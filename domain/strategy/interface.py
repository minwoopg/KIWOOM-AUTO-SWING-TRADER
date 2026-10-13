from __future__ import annotations

"""전략 자리 — 인터페이스만 (스윙 분리 6라운드, 2026-09-28).

매매 로직은 이 라운드에 없습니다. 하루 수명주기(`app/session_runner.py`)가
전략에게 무엇을 넘기고 무엇을 받는지만 정합니다. 기본값은 아무 주문도 내지
않는 `NullStrategy`입니다.

전략이 돌려주는 것은 **주문 의도**(`OrderIntent`)뿐이고, 실제 전송 여부는
계좌 안전 한도(`AccountGuard`)와 주문 실행기(`OrderExecutor`)가 결정합니다.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from domain.market_data.tick_size import is_valid_tick
from domain.models import AccountBalance
from domain.position.swing_state import PositionMeta
from domain.service.lot_ledger import PositionSummary
from utils.trading_calendar import MarketPhase

INTENT_SIDES = ("BUY", "SELL")
ORDER_TYPES = ("market", "limit")


class OrderIntentError(ValueError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    """전략이 원하는 주문 하나 (기본 시장가, M3 A2부터 매수 지정가 가능).

    reference_price: 판단에 쓴 가격(수량·금액 한도 계산과 거래 로그 기록용).
    order_type / limit_price: "limit"은 **매수만**, limit_price는 KRX 호가 단위에 맞는 양의 정수.
      지정가 매수의 금액 한도는 limit_price × 수량(체결가는 그 이하) — `notional` 참고.
    forced: 매도 전용 — 손절 등. PSM의 재시도 백오프(SOFT)를 넘을 수 있음.
    context: 거래 로그·체결 훅에 함께 실릴 값 (entry_strategy, entry_reason 등).
    """

    symbol: str
    side: str
    quantity: int
    reference_price: int
    reason: str = ""
    forced: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    order_type: str = "market"
    limit_price: int | None = None

    def __post_init__(self) -> None:
        if self.side not in INTENT_SIDES:
            raise OrderIntentError(f"side={self.side!r}")
        if not str(self.symbol).strip():
            raise OrderIntentError("symbol이 비어 있음")
        for name in ("quantity", "reference_price"):
            v = getattr(self, name)
            if type(v) is not int or v <= 0:
                raise OrderIntentError(f"{name}는 양의 정수 — {v!r}")
        if self.forced and self.side != "SELL":
            raise OrderIntentError("forced는 매도에만")
        if self.order_type not in ORDER_TYPES:
            raise OrderIntentError(f"order_type={self.order_type!r} (허용: {ORDER_TYPES})")
        if self.order_type == "limit":
            if self.side != "BUY":
                raise OrderIntentError("지정가는 매수에만 (매도·손절은 시장가)")
            if type(self.limit_price) is not int or self.limit_price <= 0:
                raise OrderIntentError(f"limit_price는 양의 정수 — {self.limit_price!r}")
            if not is_valid_tick(self.limit_price):
                raise OrderIntentError(f"limit_price {self.limit_price:,}는 KRX 호가 단위가 아님")
        elif self.limit_price is not None:
            raise OrderIntentError("시장가 주문에 limit_price를 넣지 않음")

    @property
    def notional(self) -> int:
        if self.order_type == "limit":
            return self.quantity * int(self.limit_price)
        return self.quantity * self.reference_price


@dataclass(frozen=True)
class TickContext:
    """장중 한 번의 폴링에서 전략이 볼 수 있는 것 (읽기 전용 스냅샷)."""

    now: datetime
    phase: MarketPhase
    trade_date: Any                       # date — 오늘 거래일
    balance: AccountBalance
    positions: dict[str, PositionSummary]  # 체결 원장 기준 보유
    metas: dict[str, PositionMeta]
    blocked_symbols: frozenset[str]        # 장부 불일치·검토 필요 등으로 자동 매매 금지
    orders_in_flight: bool
    in_flight_symbols: frozenset[str] = frozenset()  # 미해결 주문이 걸린 종목 (8-B)


class Strategy(Protocol):
    strategy_id: str

    def on_session_start(self, ctx: TickContext) -> None: ...

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]: ...

    def on_session_end(self, ctx: TickContext) -> None: ...


class NullStrategy:
    """아무 주문도 내지 않는 기본 전략 — 뼈대 검증·운영 점검용."""

    strategy_id = "null"

    def on_session_start(self, ctx: TickContext) -> None:
        return None

    def on_tick(self, ctx: TickContext) -> list[OrderIntent]:
        return []

    def on_session_end(self, ctx: TickContext) -> None:
        return None
