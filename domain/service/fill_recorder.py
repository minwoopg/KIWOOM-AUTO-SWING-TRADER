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
    체결조회 증거로 실제 체결가를 붙이는 것은 이후 과제.
- event_id = "{주문번호}:{누적 체결수량}" → 재시작·재폴링해도 같은 체결이 두 번
  기록되지 않음(원장이 같은 id를 무시).
- 추적 정보는 메모리에만 있습니다. 주문 도중 재시작하면 `OrderExecutor`가 그
  종목을 ERROR로 복원하고, 원장·잔고 불일치는 기동 점검에서 보고됩니다 —
  사람이 확인한 뒤 사건을 추가하는 것이 원칙입니다.
"""

from dataclasses import dataclass
from datetime import date, datetime

from domain.models import AccountBalance
from domain.position.fill_event import FillEvent


@dataclass
class TrackedOrder:
    symbol: str
    side: str                  # "BUY" / "SELL"
    order_id: str
    requested_quantity: int
    base_quantity: int         # 접수 직전 잔고 수량
    base_cost: int             # 접수 직전 잔고 원가(평균단가 × 수량) — 매수 원가 역산용
    reference_price: int
    filled_quantity: int = 0
    recorded_cost: int = 0     # 이 주문으로 기록한 매수 원가 합계

    @property
    def done(self) -> bool:
        return self.filled_quantity >= self.requested_quantity


class FillRecorder:
    def __init__(self, ledger_store, logger=None) -> None:
        self.ledger_store = ledger_store
        self.logger = logger
        self._orders: dict[str, TrackedOrder] = {}

    @property
    def tracked(self) -> dict[str, TrackedOrder]:
        return dict(self._orders)

    def track(self, symbol: str, side: str, order_id: str, requested_quantity: int,
              reference_price: int, *, base_quantity: int, base_cost: int) -> None:
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
                                            base_quantity, base_cost, reference_price)

    def stop_tracking(self, symbol: str) -> TrackedOrder | None:
        return self._orders.pop(symbol, None)

    def observe(self, balance: AccountBalance, *, trade_date: date, now: datetime) -> list[FillEvent]:
        """잔고 스냅샷을 보고 새 체결 사건을 원장에 기록. 기록한 사건 목록 반환."""
        held = {p.symbol: p for p in balance.positions}
        written: list[FillEvent] = []
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
            ev = FillEvent(
                event_id=f"{order.order_id}:{filled_now}", kind=order.side, symbol=sym,
                quantity=delta, price=price, price_source=source, trade_date=trade_date,
                occurred_at=now, order_id=order.order_id,
                note=f"잔고 변화 기록 ({order.base_quantity}→{broker_qty}주)",
            )
            self.ledger_store.append(ev)
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
