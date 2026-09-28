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

from domain.models import AccountBalance
from domain.position.swing_state import PositionMeta
from domain.service.lot_ledger import PositionSummary
from utils.trading_calendar import MarketPhase

INTENT_SIDES = ("BUY", "SELL")


class OrderIntentError(ValueError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    """전략이 원하는 주문 하나 (시장가).

    reference_price: 판단에 쓴 가격(수량·금액 한도 계산과 거래 로그 기록용).
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

    @property
    def notional(self) -> int:
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
