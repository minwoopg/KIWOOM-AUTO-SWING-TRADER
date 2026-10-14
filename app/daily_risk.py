from __future__ import annotations

"""일일 위험 한도 (C3 — GPT 최소판 요청: 일일 손실 한도·실패 주문 한도) — 실행기가 폴링마다 호출.

| 조건 | 결과 |
|---|---|
| 그날 거래 손익(비용 후, `domain/risk/daily_loss.day_pnl`) ≤ −daily_loss_limit | 신규 매수 중단 요청(`commands/halt.json`, by=daily_risk) |
| 그날 미접수·응답 불명 주문 수 ≥ max_failed_orders | 같은 방식 |
| 시작 기준(DAY_START)을 못 정함·현재가 없음·기록 파일 손상 | 그 폴링 신규 매수 보류(DAILY_RISK_UNKNOWN) — 중단 파일은 쓰지 않음 |

- 중단은 `commands/halt.json`이라 재시작해도 유지되고 **사람이 재개**(관리 창·`tools/buy_halt.py resume`). 매도·손절은 계속.
- enforce=False(그림자 모드): 계산·기록만 하고 중단 파일은 쓰지 않음("중단했을 것" 로그).
- 손익은 원장 사건 기준(입출금 무관). 체결 근거가 있으면 실제 체결가·실제 비용, 없으면 원장 가격·추정 비용(`trade_costs`).
"""

from datetime import date, datetime

from domain.risk.daily_loss import DayPnl, day_pnl
from domain.service.trade_costs import CostProfile, event_costs

BLOCK_UNKNOWN = "DAILY_RISK_UNKNOWN"
BLOCK_BASELINE = "BASELINE_MISSING_WITH_TRADES"
BLOCK_HALTED = "DAILY_RISK_HALTED"


class DailyRiskGuard:
    def __init__(self, *, store, ledger_store, evidence_store, commands_dir, logger, limit: int, max_failures: int,
                 profile: CostProfile, enforce: bool) -> None:
        self.store, self.ledger_store, self.evidence_store = store, ledger_store, evidence_store
        self.commands_dir, self.log = commands_dir, logger
        self.limit, self.max_failures, self.profile, self.enforce = limit, max_failures, profile, enforce
        self.last: dict = {}
        self._halted_today: str | None = None
        self._last_log = ""

    # ── 계산 ─────────────────────────────────────────────
    def _events_today(self, trade_date: date):
        events = self.ledger_store.load()
        actual: dict = {}
        if self.evidence_store is not None:
            from domain.service.fill_evidence import apply_fill_evidence
            try:
                ov = apply_fill_evidence(events, self.evidence_store.load())
                events = ov.events
                actual = {eid: (r.commission, r.tax) for eid, r in ov.applied.items()}
            except Exception as exc:                       # noqa: BLE001 — 근거 없이 원장 가격·추정 비용으로
                self._say("warning", f"체결 근거 읽기 실패 — 원장 가격·추정 비용으로 계산: {type(exc).__name__}: {exc}")
        today = [e for e in events if e.trade_date == trade_date and e.kind in ("BUY", "SELL", "OPENING")]
        return today, event_costs(today, actual, self.profile)

    def step(self, now: datetime, trade_date: date, ledger_positions: dict, balance, prices: dict[str, int]) -> str | None:
        """신규 매수를 막을 사유(없으면 None). 한도에 닿으면 중단 요청을 씀(enforce)."""
        d = trade_date.isoformat()
        try:
            start, failures, halts = self.store.day(d)
            qty_now = {s: p.quantity for s, p in ledger_positions.items() if p.quantity > 0}
            if start is None:
                # C4(GPT R1): 오늘 체결 사건이 이미 있으면 지금 보유로 시작 기준을 만들 수 없음(오늘 매수가 기준과 사건에
                # 두 번 들어감). 그날 신규 매수 보류 — 기준을 추정하지 않고, 사건을 빼서 손실을 지우지도 않음. 청산은 계속.
                today_events = [e for e in self.ledger_store.load()
                                if e.trade_date == trade_date and e.kind in ("BUY", "SELL", "OPENING")]
                if today_events:
                    self.last = {"trade_date": d, "state": BLOCK_BASELINE,
                                 "note": f"시작 기준 전에 오늘 체결 {len(today_events)}건 — 그날 신규 매수 보류(다음 거래일 새 기준)"}
                    self._say("critical", f"시작 기준 없이 오늘 체결 {len(today_events)}건이 있음 — 그날 신규 매수 보류, "
                                          "청산은 계속(기준을 추정하지 않음)")
                    return BLOCK_BASELINE
                missing = [s for s in qty_now if type(prices.get(s)) is not int or prices.get(s, 0) <= 0]
                if missing:
                    self.last = {"trade_date": d, "state": "NO_START", "note": f"시작 기준가 없음: {missing}"}
                    self._say("warning", f"하루 시작 기준을 정하지 못함(현재가 없음 {missing}) — 신규 매수 보류")
                    return BLOCK_UNKNOWN
                held_value = sum(q * prices[s] for s, q in qty_now.items())
                start = {"type": "DAY_START", "trade_date": d, "at": now.isoformat(timespec="seconds"),
                         "qty": qty_now, "marks": {s: prices[s] for s in qty_now}, "cash": balance.cash,
                         "start_equity": balance.cash + held_value,
                         "note": "손익 = 원장 기준(입출금 무관). start_equity는 표시용(현금 + 원장 보유 × 기준가)"}
                self.store.append(start)
                self._say("info", f"하루 시작 기준 기록 — 보유 {qty_now or '없음'}, 시작 평가액 {start['start_equity']:,}원")
            events, costs = self._events_today(trade_date)
            pnl: DayPnl = day_pnl(start, events, qty_now, prices, costs)
        except Exception as exc:                           # noqa: BLE001 — 손상·읽기 실패: 그날 신규 매수 없음
            self.last = {"trade_date": d, "state": "ERROR", "note": f"{type(exc).__name__}: {exc}"}
            self._say("critical", f"일일 위험 계산 실패 — 신규 매수 보류: {type(exc).__name__}: {exc}")
            return BLOCK_UNKNOWN
        n_fail = len(failures)
        self.last = {"trade_date": d, "state": "OK" if pnl.pnl is not None else "UNKNOWN", **pnl.to_dict(),
                     "label": "시작 기준 이후 거래 손익(비용 후, 입출금 제외)", "start_at": start.get("at"),
                     "limit": self.limit, "failed_orders": n_fail, "max_failed_orders": self.max_failures,
                     "start_equity": start.get("start_equity"), "halted": bool(halts), "enforce": self.enforce,
                     "cost_profile": self.profile.label}
        if halts or self._halted_today == d:
            return BLOCK_HALTED
        reason = ""
        if pnl.pnl is not None and pnl.pnl <= -self.limit:
            reason = f"일일 손실 {pnl.pnl:,}원 ≤ −{self.limit:,}원 (비용 {pnl.costs:,}원, 그중 추정 {pnl.estimated_costs:,}원)"
        elif n_fail >= self.max_failures:
            reason = f"미접수·응답 불명 주문 {n_fail}건 ≥ {self.max_failures}건"
        if reason:
            return self._halt(now, d, reason)
        if pnl.pnl is None:
            self._say("warning", f"일일 손익 계산 불가({pnl.note}) — 신규 매수 보류")
            return BLOCK_UNKNOWN
        return None

    def on_order_failure(self, now: datetime, trade_date: date, intent, status: str, detail: str) -> str | None:
        d = trade_date.isoformat()
        try:
            self.store.append({"type": "ORDER_FAILURE", "trade_date": d, "at": now.isoformat(timespec="seconds"),
                               "symbol": intent.symbol, "side": intent.side, "status": status, "detail": detail[:300]})
            _, failures, halts = self.store.day(d)
        except Exception as exc:                           # noqa: BLE001
            self._say("critical", f"주문 실패 기록 실패: {type(exc).__name__}: {exc}")
            return self._halt(now, d, f"주문 실패({intent.symbol} {intent.side} {status}) 기록 실패")
        if halts:
            return BLOCK_HALTED
        if len(failures) >= self.max_failures:
            return self._halt(now, d, f"미접수·응답 불명 주문 {len(failures)}건 ≥ {self.max_failures}건 "
                                      f"(마지막: {intent.symbol} {intent.side} {status})")
        return None

    def _halt(self, now: datetime, d: str, reason: str) -> str:
        if not self.enforce:
            self._say("warning", f"[그림자] 신규 매수 중단 조건 — {reason} (그림자 모드라 중단 파일은 쓰지 않음)")
            self.last["would_halt"] = reason
            return BLOCK_HALTED
        if self._halted_today != d:
            from infra.storage.runner_status import request_buy_halt
            created, req = request_buy_halt(self.commands_dir, reason=f"[일일 위험] {reason}", by="daily_risk", now=now)
            try:
                self.store.append({"type": "HALT", "trade_date": d, "at": now.isoformat(timespec="seconds"),
                                   "reason": reason, "request_id": req.get("request_id"), "created": created})
            except Exception:                              # noqa: BLE001 — 중단 파일이 원천
                pass
            self._halted_today = d
            self.log.critical(f"[DAILY_RISK] 신규 매수 중단 요청 — {reason} · 요청 {req.get('request_id')} "
                              "(매도·손절은 계속, 사람이 재개)")
        self.last["halt_reason"] = reason
        return BLOCK_HALTED

    def _say(self, level: str, msg: str) -> None:
        if msg != self._last_log:
            self._last_log = msg
            getattr(self.log, level)(f"[DAILY_RISK] {msg}")
