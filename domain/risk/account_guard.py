from __future__ import annotations

"""계좌 안전 한도 (스윙 분리 6라운드, 2026-09-28).

전략과 무관한 **절대 한도**만 둡니다. 전략이 어떤 주문 의도를 내더라도 여기서
막히면 브로커로 가지 않습니다. 판단 규칙(언제 사고팔지)은 없습니다.

| 한도 | 매수 | 매도 |
|---|---|---|
| 신규 주문 가능 시간대 | 적용 | 적용 (단, forced 매도는 장중 전체 허용) |
| 장부 불일치·검토 필요 종목 | 차단 | 차단 (사람 확인 전 자동 매매 금지) |
| 1회 주문 금액 상한 | 적용 | — |
| 최대 보유 종목 수 | 신규 종목이면 적용 | — |
| 총 노출(보유 평가액 + 이번 주문) 상한 | 적용 | — |
| 현금 버퍼 | 적용 | — |
| 매도 수량 ≤ 원장 보유 수량 | — | 적용 |
| 허용 종목 목록(설정 시) | 적용 | — |

순수 함수입니다(파일·네트워크 없음).
"""

from dataclasses import dataclass
from datetime import datetime, time

from domain.models import AccountBalance
from domain.service.lot_ledger import PositionSummary
from domain.strategy.interface import OrderIntent
from utils.trading_calendar import MarketPhase


@dataclass(frozen=True)
class GuardConfig:
    max_positions: int = 5
    max_order_amount: int = 2_000_000
    max_total_exposure: int = 10_000_000
    min_cash_buffer: int = 100_000
    new_orders_start: time = time(9, 5)     # 시가 단일가 직후 변동성 회피
    new_orders_end: time = time(15, 15)     # 종가 단일가(15:20) 전 마감
    allowed_symbols: tuple[str, ...] = ()   # 비어 있으면 제한 없음

    def __post_init__(self) -> None:
        for name in ("max_positions", "max_order_amount", "max_total_exposure"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name}는 양수")
        if self.min_cash_buffer < 0:
            raise ValueError("min_cash_buffer는 0 이상")
        if self.new_orders_start >= self.new_orders_end:
            raise ValueError("new_orders_start < new_orders_end 여야 함")
        if self.max_order_amount > self.max_total_exposure:
            raise ValueError("max_order_amount가 max_total_exposure보다 클 수 없음")


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    code: str = "OK"
    detail: str = ""


def _deny(code: str, detail: str) -> GuardDecision:
    return GuardDecision(False, code, detail)


def check_intent(
    intent: OrderIntent,
    *,
    config: GuardConfig,
    now: datetime,
    phase: MarketPhase,
    balance: AccountBalance,
    positions: dict[str, PositionSummary],
    prices: dict[str, int],
    blocked_symbols: frozenset[str] | set[str],
) -> GuardDecision:
    """주문 의도 하나를 한도에 비춰 허용/차단.

    prices: 보유 종목 평가용 현재가(없으면 원장 원가로 평가 — 보수적이지 않을 수
      있으므로 호출부가 가능한 한 채워서 넘김).
    """
    if phase not in (MarketPhase.REGULAR,):
        if not (intent.forced and phase == MarketPhase.CLOSING_AUCTION):
            return _deny("OUTSIDE_SESSION", f"장 단계 {phase.value}")
    t = now.time()
    in_window = config.new_orders_start <= t < config.new_orders_end
    if not in_window and not (intent.side == "SELL" and intent.forced):
        return _deny("OUTSIDE_ORDER_WINDOW",
                     f"{t.strftime('%H:%M')} (허용 {config.new_orders_start:%H:%M}~{config.new_orders_end:%H:%M})")
    if intent.symbol in blocked_symbols:
        return _deny("SYMBOL_BLOCKED", "장부 불일치 또는 검토 필요 종목 — 사람 확인 전 자동 매매 금지")

    if intent.side == "SELL":
        held = positions.get(intent.symbol)
        held_qty = held.quantity if held else 0
        if intent.quantity > held_qty:
            return _deny("SELL_EXCEEDS_LEDGER", f"매도 {intent.quantity}주 > 원장 보유 {held_qty}주")
        return GuardDecision(True)

    # ── BUY ──
    if config.allowed_symbols and intent.symbol not in config.allowed_symbols:
        return _deny("SYMBOL_NOT_ALLOWED", "허용 종목 목록에 없음")
    if intent.notional > config.max_order_amount:
        return _deny("ORDER_AMOUNT_LIMIT", f"{intent.notional:,}원 > 상한 {config.max_order_amount:,}원")
    held_symbols = {s for s, p in positions.items() if p.quantity > 0} | {
        p.symbol for p in balance.positions if p.quantity > 0}
    if intent.symbol not in held_symbols and len(held_symbols) >= config.max_positions:
        return _deny("MAX_POSITIONS", f"보유 {len(held_symbols)}종목 / 상한 {config.max_positions}")
    exposure = 0
    for sym, p in positions.items():
        exposure += p.quantity * prices.get(sym, 0) if prices.get(sym, 0) > 0 else p.cost_basis
    if exposure + intent.notional > config.max_total_exposure:
        return _deny("TOTAL_EXPOSURE_LIMIT",
                     f"보유 {exposure:,} + 주문 {intent.notional:,} > 상한 {config.max_total_exposure:,}")
    if balance.cash - intent.notional < config.min_cash_buffer:
        return _deny("CASH_BUFFER", f"현금 {balance.cash:,} - 주문 {intent.notional:,} < 버퍼 {config.min_cash_buffer:,}")
    return GuardDecision(True)
