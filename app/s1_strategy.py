from __future__ import annotations

"""S1 진입 전략 (M3 B) — 관리자 후보(`app/candidates.py`) → 주문 직전 가격 재확인 → 1주 상한 지정가 매수 의도.

규칙 (GPT 최소판 요청 — S1 조건·기준값은 바꾸지 않음)
- 진입 시간: entry_start(09:05) ≤ 지금 < entry_end(session.entry_cancel_at, 09:30), 정규장.
- 최대 보유 1: 원장·잔고에 보유가 있거나 미해결 주문이 있으면 신규 의도 없음. 추가 매수 없음.
- 후보는 승인된 것만(CandidateReader — 관리자 기록 교차 검증). **폴링마다 다시 읽고**, 그 폴링의 결과가
  ERROR·WAITING·거부면 신규 의도 없음(C1 — 한 번 받은 승인을 붙들고 있지 않음).
- 주문 직전 재확인: 현재가 > 참고 손절가 ∧ 현재가 ≤ 진입 상한 ∧ 최우선 매도호가 ≤ 지정가. 하나라도 아니면 그 폴링은 보류
  (진입 시간 안에서 다음 폴링에 다시 확인). 호가를 못 받으면 보류.
- 지정가 = 진입 상한을 KRX 호가 단위로 내림(후보 승인에서 계산), 수량 1.
- 같은 신호는 한 번만: 의도를 넘기기 전에 신호 키를 기록(SignalKeyStore). 취소로 FLAT이 돼도 다시 진입하지 않음.
- 한 폴링에 의도 하나.
- 판단 기록: 후보 상태·보류 사유·의도를 decisions(콜백)로 남김(같은 사유 반복은 한 번).
"""

from datetime import date, datetime, time
from typing import Callable

from domain.strategy.interface import OrderIntent, TickContext
from utils.trading_calendar import MarketPhase

STRATEGY_ID = "s1_pullback_v0.1"


class S1EntryStrategy:
    strategy_id = STRATEGY_ID

    def __init__(self, *, mode: str, scope: str, candidates: Callable[[date, datetime], object],
                 quote: Callable[[str], tuple[int | None, int | None]], keys, decisions: Callable[[dict], None],
                 entry_start: time, entry_end: time, quantity: int = 1) -> None:
        if quantity != 1:
            raise ValueError("최소판: 수량 1 고정")
        self.mode, self.scope = mode, scope
        self.candidates, self.quote, self.keys, self.decisions = candidates, quote, keys, decisions
        self.entry_start, self.entry_end, self.quantity = entry_start, entry_end, quantity
        self.book = None
        self._last_note: dict[str, str] = {}

    # ── 기록 ────────────────────────────────────────────────
    def _note(self, ctx: TickContext, topic: str, event: str, detail: dict | None = None) -> None:
        sig = f"{event}|{sorted((detail or {}).items())}"
        if self._last_note.get(topic) == sig:
            return
        self._last_note[topic] = sig
        self.decisions({"at": ctx.now.isoformat(timespec="seconds"), "mode": self.mode, "strategy": STRATEGY_ID,
                        "topic": topic, "event": event, **(detail or {})})

    def key(self, signal_id: str) -> str:
        return f"{self.mode}|{self.scope}|{signal_id}"

    # ── Strategy ────────────────────────────────────────────
    def on_session_start(self, ctx: TickContext) -> None:
        return None

    def on_session_end(self, ctx: TickContext) -> None:
        if self.book is not None:
            self._note(ctx, "session", "SESSION_END", {"book_state": self.book.state,
                                                       "approved": [a.symbol for a in self.book.approved]})

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        now = ctx.now
        if ctx.phase != MarketPhase.REGULAR or not (self.entry_start <= now.time() < self.entry_end):
            return []
        # C1(GPT 검토 R1): 신규 진입을 검토하는 폴링마다 승인을 새로 읽음 — 이전 READY를 계속 쓰지 않음.
        # 최신 결과가 ERROR·WAITING·거부면 그 폴링은 의도 없음. 이미 쓴 신호 키는 다시 살아나지 않음.
        self.book = self.candidates(ctx.trade_date, now)
        self._note(ctx, "book", f"CANDIDATES_{self.book.state}", {
            "signal_date": self.book.signal_date, "contract": self.book.contract_hash,
            "approved": [a.symbol for a in self.book.approved], "rejected": self.book.rejected,
            "note": self.book.note})
        if self.book.state != "READY" or not self.book.approved:
            return []
        held = ({s for s, p in ctx.positions.items() if p.quantity > 0}
                | {p.symbol for p in ctx.balance.positions if p.quantity > 0})
        if held or ctx.orders_in_flight:
            self._note(ctx, "capacity", "MAX_POSITION", {"held": sorted(held), "in_flight": ctx.orders_in_flight})
            return []
        used = self.keys.keys()
        for c in sorted(self.book.approved, key=lambda a: a.symbol):
            k = self.key(c.signal_id)
            if k in used:
                continue
            if c.symbol in ctx.blocked_symbols or c.symbol in ctx.in_flight_symbols:
                self._note(ctx, c.symbol, "SKIP_BLOCKED")
                continue
            try:
                price, ask = self.quote(c.symbol)
            except Exception as exc:                       # noqa: BLE001
                price, ask = None, None
                self._note(ctx, c.symbol, "QUOTE_FAILED", {"error": f"{type(exc).__name__}: {exc}"})
            why = []
            if not price:
                why.append("NO_PRICE")
            else:
                if price <= c.stop_ref:
                    why.append("PRICE_AT_OR_BELOW_STOP")
                if price > c.entry_cap:
                    why.append("PRICE_ABOVE_CAP")
            if not ask:
                why.append("NO_ASK")
            elif ask > c.limit_price:
                why.append("ASK_ABOVE_LIMIT")
            if why:
                self._note(ctx, c.symbol, "WAIT_PRICE", {"reasons": why, "price": price, "ask": ask,
                                                         "limit": c.limit_price, "stop_ref": c.stop_ref,
                                                         "entry_cap": c.entry_cap})
                continue
            rec = {"at": now.isoformat(timespec="seconds"), "symbol": c.symbol, "signal_id": c.signal_id,
                   "signal_date": c.signal_date, "limit_price": c.limit_price, "price": price, "ask": ask}
            if not self.keys.claim(k, rec):
                continue
            intent = OrderIntent(c.symbol, "BUY", self.quantity, price, reason=f"S1 신호 {c.signal_date}",
                                 order_type="limit", limit_price=c.limit_price,
                                 context={"signal_id": c.signal_id, "signal_date": c.signal_date,
                                          "entry_cap": c.entry_cap, "stop_ref": c.stop_ref,
                                          "contract_hash": c.contract_hash, "scan_run_id": c.scan_run_id,
                                          "strategy_mode": self.mode, "pre_order_price": price, "pre_order_ask": ask})
            self._note(ctx, c.symbol, "INTENT", {**rec, "stop_ref": c.stop_ref, "entry_cap": c.entry_cap})
            return [intent]
        return []
