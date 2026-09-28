from __future__ import annotations

"""여러 날 보유 로트 계산 (스윙 분리 4라운드, 2026-09-28).

체결 원장(`FillEvent` 목록)을 시간 순서대로 적용해 종목별 미청산 로트와
실현 손익을 계산하는 **순수 함수** 모음입니다(파일·네트워크 없음).

단타 `pnl_calculator.calculate_realized_pnl_by_events()`와의 차이
- 하루 범위가 아니라 원장 전체를 적용합니다. 이월분은 OPENING 사건(잔고
  평균단가)으로 원장에 들어가므로 "이월 매도는 종목당 1회만 인정" 같은
  제한이 필요 없습니다 → 분할 청산이 막히지 않습니다.
- 매도 수량이 보유 로트보다 많으면 여전히 예외(`LotMatchError`) — 원장이
  잔고와 어긋났다는 뜻이므로 조용히 계산하지 않습니다(fail-close 원칙 유지).
- 가격 출처를 매칭 결과까지 가져가 "추정 포함" 여부를 표시합니다.

손익은 비용 전(gross) 금액입니다. 비용 반영은 `net_pnl()`에서
`domain/cost_model.py`의 시나리오로 따로 계산합니다(실제 체결가에 이미 반영된
슬리피지를 이중 차감하지 않도록 호출부가 시나리오를 고름).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from domain.position.fill_event import FillEvent


class LotMatchError(ValueError):
    """매도 수량이 그 시점 보유 로트보다 많음 — 원장이 잔고와 어긋남."""


@dataclass
class Lot:
    symbol: str
    quantity: int            # 남은 수량
    price: int
    trade_date: date
    event_id: str
    price_source: str

    @property
    def is_estimate(self) -> bool:
        return self.price_source == "ORDER_ESTIMATE"


@dataclass(frozen=True)
class RealizedMatch:
    """매도 1건이 로트 1개와 맞물린 결과 (매도 하나가 여러 로트에 걸치면 여러 개)."""

    symbol: str
    quantity: int
    buy_price: int
    buy_date: date
    buy_event_id: str
    buy_price_source: str
    sell_price: int
    sell_date: date
    sell_event_id: str
    sell_price_source: str

    @property
    def gross_pnl(self) -> int:
        return (self.sell_price - self.buy_price) * self.quantity

    @property
    def buy_notional(self) -> int:
        return self.buy_price * self.quantity

    @property
    def is_estimate(self) -> bool:
        return "ORDER_ESTIMATE" in (self.buy_price_source, self.sell_price_source)


@dataclass(frozen=True)
class PositionSummary:
    symbol: str
    quantity: int
    cost_basis: int               # 남은 로트 원가 합계
    first_entry_date: date        # 남은 로트 중 가장 이른 체결일
    last_entry_date: date
    lot_count: int
    includes_estimate: bool

    @property
    def avg_price(self) -> float:
        return self.cost_basis / self.quantity


@dataclass
class LedgerResult:
    open_lots: dict[str, list[Lot]] = field(default_factory=dict)
    realized: list[RealizedMatch] = field(default_factory=list)

    def position(self, symbol: str) -> PositionSummary | None:
        lots = self.open_lots.get(symbol) or []
        if not lots:
            return None
        return PositionSummary(
            symbol=symbol,
            quantity=sum(l.quantity for l in lots),
            cost_basis=sum(l.quantity * l.price for l in lots),
            first_entry_date=min(l.trade_date for l in lots),
            last_entry_date=max(l.trade_date for l in lots),
            lot_count=len(lots),
            includes_estimate=any(l.is_estimate for l in lots),
        )

    def positions(self) -> dict[str, PositionSummary]:
        out = {}
        for sym in sorted(self.open_lots):
            p = self.position(sym)
            if p is not None:
                out[sym] = p
        return out

    def realized_between(self, start: date, end: date) -> list[RealizedMatch]:
        """매도 거래일 기준 start 이상 end 이하."""
        return [r for r in self.realized if start <= r.sell_date <= end]

    def realized_gross_pnl(self, start: date, end: date) -> int:
        return sum(r.gross_pnl for r in self.realized_between(start, end))

    def unrealized_gross_pnl(self, prices: dict[str, int]) -> dict[str, int]:
        """보유 종목별 평가손익(비용 전). 가격이 없는 종목은 결과에서 빠짐."""
        out = {}
        for sym, p in self.positions().items():
            if sym in prices and prices[sym] > 0:
                out[sym] = prices[sym] * p.quantity - p.cost_basis
        return out


def apply_events(events: Iterable[FillEvent]) -> LedgerResult:
    """원장 사건을 (occurred_at, 기록 순서) 순으로 적용해 FIFO 로트를 계산합니다."""
    indexed = list(enumerate(events))
    indexed.sort(key=lambda p: (p[1].occurred_at, p[0]))
    result = LedgerResult()
    for _, ev in indexed:
        lots = result.open_lots.setdefault(ev.symbol, [])
        if ev.kind in ("BUY", "OPENING"):
            lots.append(Lot(ev.symbol, ev.quantity, ev.price, ev.trade_date,
                            ev.event_id, ev.price_source))
            continue
        remaining = ev.quantity
        held = sum(l.quantity for l in lots)
        if remaining > held:
            raise LotMatchError(
                f"{ev.symbol}: 매도 {ev.quantity}주 > 보유 로트 {held}주 "
                f"(event_id={ev.event_id}, {ev.trade_date}) — 원장이 잔고와 어긋남"
            )
        while remaining > 0:
            lot = lots[0]
            take = min(lot.quantity, remaining)
            result.realized.append(RealizedMatch(
                symbol=ev.symbol, quantity=take,
                buy_price=lot.price, buy_date=lot.trade_date, buy_event_id=lot.event_id,
                buy_price_source=lot.price_source,
                sell_price=ev.price, sell_date=ev.trade_date, sell_event_id=ev.event_id,
                sell_price_source=ev.price_source,
            ))
            lot.quantity -= take
            remaining -= take
            if lot.quantity == 0:
                lots.pop(0)
    result.open_lots = {s: l for s, l in result.open_lots.items() if l}
    return result


def net_pnl(matches: Iterable[RealizedMatch], cost_model, scenario: str) -> float:
    """비용 차감 후 손익. cost_model = domain.cost_model.CostModel (진입 원금 기준 왕복 비용률)."""
    total = 0.0
    for m in matches:
        total += m.gross_pnl - cost_model.cost_amount(m.buy_notional, scenario)
    return total
