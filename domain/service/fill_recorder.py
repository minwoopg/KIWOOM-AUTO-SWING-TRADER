from __future__ import annotations

"""체결 → 원장 자동 기록 (스윙 분리 6라운드, 2026-09-28).

`OrderExecutor`가 접수한 주문을 추적하다가, 폴링마다 받은 잔고에서 수량이
변한 만큼 체결 사건(`FillEvent`)을 원장에 추가합니다.

원칙
- **이 프로그램이 접수한 주문의 몫만** 기록합니다. 추적 중인 주문이 없는
  종목의 수량 변화(HTS 수동 매매 등)는 기록하지 않고, 장부 대조
  (`position_book.reconcile`)에서 불일치로 드러나게 둡니다.
- 방향이 맞는 변화만 기록합니다(매수 주문인데 수량이 줄면 기록하지 않음).
- 요청 수량을 넘는 변화는 넘는 만큼 기록하지 않습니다.
- 가격
  - 매수: 잔고 평균단가로 역산한 실제 원가
    `(새 평균단가 × 새 수량 − 직전 잔고 원가) ÷ 증가 수량` → BROKER_AVG.
    (직전 원가도 잔고 평균단가 기준이라 원장의 추정가와 섞이지 않음.)
    역산 값이 0 이하이거나 요청 수량을 넘는 변화가 섞이면 주문가 추정(ORDER_ESTIMATE).
  - 매도: 잔고로는 체결가를 알 수 없어 주문 직전 시세(ORDER_ESTIMATE).
  - (M3 A2) `fill_lookup`이 있으면 **주문 전량이 한 사건으로 끝날 때** 주문별 체결조회(ka10076)를
    먼저 봅니다. 주문번호·방향·수량이 맞는 전량 체결 행이 있으면 그 체결가(BROKER_FILL)로 기록하고
    수수료·세금은 체결 근거 기록(`fill_evidence.jsonl`)에 남깁니다. 조회 실패·행 없음이면 위 규칙대로
    기록하고, 다음 폴링부터 한 폴링에 한 건씩(최대 RETRY_LIMIT회, 간격을 늘리며) 다시 확인해 근거 기록만 추가합니다
    (원장 사건은 다시 쓰지 않음 — 중복 BUY/SELL 없음). 재시작하면 재확인 목록은 사라지고
    `tools/paper_order.py confirm-fills`로 확인합니다. 지정가 매수의 추정가는 지정가(체결가는 그 이하).
- event_id = "{계좌범위}|{주문 거래일}|{매수/매도}|{종목}|{주문번호}|{누적 체결수량}"
  → 같은 주문의 재폴링은 같은 id라 두 번 기록되지 않고(원장이 같은 id를 무시),
  다른 날·다른 계좌에서 같은 주문번호가 다시 나와도 다른 사건으로 구분됨
  (8-C, F4 — 이전 형식 "{주문번호}:{누적수량}"은 거래일·계좌 범위가 없었음.
  이미 기록된 이전 형식 사건은 그대로 유효하며 새 형식과 겹치지 않음).
  계좌범위는 설정의 `broker.account_scope_id`(계좌번호 아님).
- 추적 정보는 메모리에만 있습니다. 주문 도중 재시작하면 `OrderExecutor`가 그
  종목을 ERROR로 복원하고, 원장·잔고 불일치는 기동 점검에서 보고됩니다 —
  사람이 확인한 뒤 사건을 추가하는 것이 원칙입니다.
"""

from dataclasses import dataclass
from datetime import date, datetime

from domain.models import AccountBalance
from domain.position.fill_event import FillEvent
from domain.service.fill_evidence import record_for_event


@dataclass
class TrackedOrder:
    symbol: str
    side: str                  # "BUY" / "SELL"
    order_id: str
    requested_quantity: int
    base_quantity: int         # 접수 직전 잔고 수량
    base_cost: int             # 접수 직전 잔고 원가(평균단가 × 수량) — 매수 원가 역산용
    reference_price: int
    order_date: date | None = None   # 주문 접수 거래일 (event_id 범위)
    filled_quantity: int = 0
    recorded_cost: int = 0     # 이 주문으로 기록한 매수 원가 합계

    @property
    def done(self) -> bool:
        return self.filled_quantity >= self.requested_quantity


def _norm_oid(order_id) -> str:
    return str(order_id or "").strip().lstrip("0")


class FillRecorder:
    RETRY_LIMIT = 5            # 체결조회 재확인 상한(사건당)
    SAVE_RETRY_LIMIT = 10      # 근거 파일 저장 재시도 상한(사건당, API 호출 없음)
    BACKOFF_BASE_SEC = 60      # 재시도 간격: 60·120·240… 초, 최대 BACKOFF_MAX_SEC
    BACKOFF_MAX_SEC = 900

    def __init__(self, ledger_store, logger=None, *, scope: str = "default",
                 fill_lookup=None, evidence_store=None) -> None:
        scope = str(scope or "").strip()
        if not scope or any(c in scope for c in "|\r\n"):
            raise ValueError(f"체결 원장 계좌 범위(scope)가 비었거나 '|'·줄바꿈 포함: {scope!r}")
        self.ledger_store = ledger_store
        self.logger = logger
        self.scope = scope
        self._orders: dict[str, TrackedOrder] = {}
        # M3 A2: fill_lookup(order_id, symbol) -> FillDetail | None (예외 가능), evidence_store.append(record)
        self.fill_lookup = fill_lookup
        self.evidence_store = evidence_store
        self._unconfirmed: dict[str, dict] = {}     # event_id → {event, attempts, next_at} — 체결조회 다시
        self._pending_saves: dict[str, dict] = {}   # event_id → {record, attempts, next_at, error} — 저장만 다시
        self.last_evidence_error = ""

    @property
    def unconfirmed(self) -> dict[str, dict]:
        return dict(self._unconfirmed)

    @property
    def pending_saves(self) -> dict[str, dict]:
        return dict(self._pending_saves)

    def evidence_status(self) -> dict:
        """실행기 상태 파일·관리 창용 — 실제 체결가·비용이 아직 확정되지 않은 사건."""
        iso = lambda v: v.isoformat(timespec="seconds") if v else None      # noqa: E731
        return {
            "lookup_pending": [{"event_id": k, "symbol": v["event"].symbol, "side": v["event"].kind,
                                "order_id": v["event"].order_id, "attempts": v["attempts"],
                                "next_at": iso(v.get("next_at"))} for k, v in self._unconfirmed.items()],
            "save_pending": [{"event_id": k, "symbol": v["record"].symbol, "side": v["record"].side,
                              "order_id": v["record"].order_id, "attempts": v["attempts"],
                              "next_at": iso(v.get("next_at")), "error": v.get("error", "")}
                             for k, v in self._pending_saves.items()],
            "last_error": self.last_evidence_error,
        }

    def _backoff(self, attempts: int, now: datetime):
        from datetime import timedelta
        return now + timedelta(seconds=min(self.BACKOFF_BASE_SEC * 2 ** max(attempts - 1, 0), self.BACKOFF_MAX_SEC))

    @property
    def tracked(self) -> dict[str, TrackedOrder]:
        return dict(self._orders)

    def track(self, symbol: str, side: str, order_id: str, requested_quantity: int,
              reference_price: int, *, base_quantity: int, base_cost: int,
              order_date: date | None = None) -> None:
        """접수(accepted)된 주문을 추적 대상으로 등록. 주문번호가 없으면 등록하지 않음
        (event_id를 만들 수 없음 — 사람 확인 대상)."""
        if not str(order_id or "").strip():
            if self.logger is not None:
                self.logger.critical(f"[FILL_RECORDER] {symbol} 주문번호 없음 — 원장 자동 기록 불가, 수동 확인 필요")
            return
        if symbol in self._orders and not self._orders[symbol].done:
            if self.logger is not None:
                self.logger.critical(f"[FILL_RECORDER] {symbol} 이미 추적 중인 주문 위에 새 주문 — 이전 추적 종료")
        self._orders[symbol] = TrackedOrder(symbol, side, str(order_id).strip(), requested_quantity,
                                            base_quantity, base_cost, reference_price, order_date=order_date)

    def event_id(self, order: TrackedOrder, filled_now: int, trade_date: date) -> str:
        order_date = order.order_date or trade_date
        return (f"{self.scope}|{order_date:%Y%m%d}|{order.side}|{order.symbol}|"
                f"{order.order_id}|{filled_now}")

    def stop_tracking(self, symbol: str) -> TrackedOrder | None:
        return self._orders.pop(symbol, None)

    def observe(self, balance: AccountBalance, *, trade_date: date, now: datetime) -> list[FillEvent]:
        """잔고 스냅샷을 보고 새 체결 사건을 원장에 기록. 기록한 사건 목록 반환."""
        held = {p.symbol: p for p in balance.positions}
        written: list[FillEvent] = []
        self._retry_one_save(now)                      # 저장만 다시(API 호출 없음)
        retried = self._retry_one_confirmation(now)
        for sym, order in list(self._orders.items()):
            pos = held.get(sym)
            broker_qty = pos.quantity if pos else 0
            if order.side == "BUY":
                filled_now = broker_qty - order.base_quantity
            else:
                filled_now = order.base_quantity - broker_qty
            filled_now = max(0, min(filled_now, order.requested_quantity))
            delta = filled_now - order.filled_quantity
            if delta <= 0:
                continue
            price, source = order.reference_price, "ORDER_ESTIMATE"
            if order.side == "BUY" and pos is not None and pos.average_price > 0:
                prev_qty = order.base_quantity + order.filled_quantity
                prev_cost = order.base_cost + order.recorded_cost
                derived = (pos.average_price * broker_qty - prev_cost) / delta
                if broker_qty == prev_qty + delta and derived > 0:
                    price, source = int(round(derived)), "BROKER_AVG"
            whole_order = delta == order.requested_quantity     # 주문 전량이 이 사건 하나
            detail, lookup_note = None, ""
            if whole_order and self.fill_lookup is not None and not retried:
                detail, lookup_note = self._lookup(order.order_id, sym, order.side, delta)
                retried = True     # 한 폴링에 체결조회 한 번(조회 한도 보호)
            if detail is not None:
                price, source = detail.price, "BROKER_FILL"
            ev = FillEvent(
                event_id=self.event_id(order, filled_now, trade_date), kind=order.side, symbol=sym,
                quantity=delta, price=price, price_source=source, trade_date=trade_date,
                occurred_at=now, order_id=order.order_id,
                note=f"잔고 변화 기록 ({order.base_quantity}→{broker_qty}주)"
                     + (" · 체결조회 ka10076" if detail is not None else ""),
            )
            self.ledger_store.append(ev)
            if whole_order and self.fill_lookup is not None:
                if detail is not None:
                    self._save_evidence(ev, detail, "AUTO", now)
                else:
                    tries = 1 if lookup_note else 0
                    self._unconfirmed[ev.event_id] = {"event": ev, "attempts": tries,
                                                      "next_at": self._backoff(tries, now) if tries else now}
                    if self.logger is not None:
                        self.logger.info(f"[FILL_RECORDER] {sym} {order.side} 체결가 확인 보류"
                                         f"({lookup_note or '이번 폴링 조회 한도'}) — 다음 폴링에 다시 확인")
            order.filled_quantity = filled_now
            if order.side == "BUY":
                order.recorded_cost += price * delta
            written.append(ev)
            if self.logger is not None:
                self.logger.info(f"[FILL_RECORDER] {sym} {order.side} {delta}주 @{price:,} ({source}) "
                                 f"누적 {filled_now}/{order.requested_quantity}")
            if order.done:
                self._orders.pop(sym, None)
        return written

    # ── M3 A2: 주문별 체결 근거 ─────────────────────────────

    def _lookup(self, order_id: str, symbol: str, side: str, quantity: int):
        """(FillDetail 또는 None, 실패 사유). 주문번호·방향·수량이 맞지 않으면 None."""
        try:
            detail = self.fill_lookup(order_id, symbol)
        except Exception as exc:
            return None, f"체결조회 실패 {type(exc).__name__}: {exc}"
        if detail is None:
            return None, "체결조회에 전량 체결 행 없음"
        side_v = getattr(detail.side, "value", detail.side)
        # 원장에 쓰기 전에 주문 식별을 끝냄(GPT 검토 R1): 주문번호(앞자리 0 무시)·종목·방향·수량 모두 일치
        if _norm_oid(getattr(detail, "order_id", "")) != _norm_oid(order_id) or not _norm_oid(order_id):
            return None, f"체결조회 행의 주문번호 {getattr(detail, 'order_id', '')!r} ≠ {order_id}"
        if side_v != side or detail.quantity != quantity or detail.symbol != symbol:
            return None, f"체결조회 행 불일치(방향 {side_v}·수량 {detail.quantity})"
        if type(detail.price) is not int or detail.price <= 0:
            return None, f"체결조회 가격 이상 {detail.price!r}"
        return detail, ""

    def _save_evidence(self, ev: FillEvent, detail, origin: str, now: datetime) -> bool:
        """근거 기록. 저장 실패면 확인한 근거를 저장 재시도 목록에 남김(다시 조회하지 않음). 성공·대기 등록이면 True."""
        rec = record_for_event(ev, detail, origin=origin, now=now)
        if rec is None:
            self.last_evidence_error = f"{ev.event_id}: 근거가 원장 사건과 맞지 않음"
            return False
        if self.evidence_store is None:
            return True
        return self._append_or_queue(rec, now, attempts=0)

    def _append_or_queue(self, rec, now: datetime, *, attempts: int) -> bool:
        """근거 저장. 일시 실패(디스크·잠금 등)는 저장 재시도 목록에(원장 사건은 다시 쓰지 않음).
        같은 사건에 다른 근거가 이미 있으면(충돌) 재시도하지 않고 사람 확인."""
        try:
            self.evidence_store.append(rec)
            self._pending_saves.pop(rec.event_id, None)
            return True
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            self.last_evidence_error = f"{rec.event_id}: 근거 저장 실패 — {err}"
            if "Conflict" in type(exc).__name__:
                self._pending_saves.pop(rec.event_id, None)
                if self.logger is not None:
                    self.logger.critical(f"[FILL_RECORDER] {rec.symbol} 체결 근거 충돌 — 덮어쓰지 않음, 사람 확인: {err}")
                return False
            attempts += 1
            if attempts >= self.SAVE_RETRY_LIMIT:
                self._pending_saves.pop(rec.event_id, None)
                if self.logger is not None:
                    self.logger.critical(f"[FILL_RECORDER] {rec.symbol} {rec.side} 체결 근거 저장 {attempts}회 실패 — "
                                         f"포기(원장은 기록됨, 실제 비용 미확정). `paper_order.py confirm-fills`로 다시: {err}")
                return False
            self._pending_saves[rec.event_id] = {"record": rec, "attempts": attempts,
                                                 "next_at": self._backoff(attempts, now), "error": err}
            if self.logger is not None:
                self.logger.critical(f"[FILL_RECORDER] {rec.symbol} 체결 근거 저장 실패(원장은 기록됨) — "
                                     f"{attempts}/{self.SAVE_RETRY_LIMIT}, 다음 폴링 이후 다시: {err}")
            return False

    def _retry_one_save(self, now: datetime) -> None:
        """저장 재시도 시각이 된 근거 하나를 다시 저장(체결조회 없음)."""
        if self.evidence_store is None or not self._pending_saves:
            return
        due = next(((k, v) for k, v in self._pending_saves.items() if v["next_at"] <= now), None)
        if due is None:
            return
        eid, item = due
        if self._append_or_queue(item["record"], now, attempts=item["attempts"]) and self.logger is not None:
            self.logger.info(f"[FILL_RECORDER] {item['record'].symbol} 체결 근거 저장 재시도 성공")
        elif eid in self._pending_saves:      # 뒤로 보내 다른 사건이 막히지 않게
            self._pending_saves[eid] = self._pending_saves.pop(eid)

    def _retry_one_confirmation(self, now: datetime) -> bool:
        """추정가로 기록된 사건 하나(재시도 시각이 된 것)의 체결가를 다시 확인. 조회했으면 True."""
        if self.fill_lookup is None or not self._unconfirmed:
            return False
        due = next(((k, v) for k, v in self._unconfirmed.items() if (v.get("next_at") or now) <= now), None)
        if due is None:
            return False
        eid, item = due
        ev: FillEvent = item["event"]
        item["attempts"] += 1
        detail, why = self._lookup(ev.order_id, ev.symbol, ev.kind, ev.quantity)
        if detail is not None:
            self._unconfirmed.pop(eid, None)          # 조회는 끝 — 저장 실패면 저장 재시도 목록으로
            if self._save_evidence(ev, detail, "AUTO_RETRY", now) and self.logger is not None:
                self.logger.info(f"[FILL_RECORDER] {ev.symbol} {ev.kind} 체결가 확인 {detail.price:,}원 "
                                 f"(원장 {ev.price_source} {ev.price:,}원 — 근거 기록)")
        elif item["attempts"] >= self.RETRY_LIMIT:
            self._unconfirmed.pop(eid, None)
            if self.logger is not None:
                self.logger.warning(f"[FILL_RECORDER] {ev.symbol} {ev.kind} 체결가 확인 {self.RETRY_LIMIT}회 실패"
                                    f"({why}) — 추정가 유지, `paper_order.py confirm-fills`로 확인")
        else:
            # 간격을 늘리고 뒤로 보냄 — 다른 사건 확인이 막히지 않게
            self.last_evidence_error = f"{ev.symbol} {ev.kind} {ev.order_id}: {why}"
            item["next_at"] = self._backoff(item["attempts"], now)
            self._unconfirmed.pop(eid)
            self._unconfirmed[eid] = item
        return True
