from __future__ import annotations

"""계좌 안전 한도 (스윙 분리 6라운드, 2026-09-28).

전략과 무관한 **절대 한도**만 둡니다. 전략이 어떤 주문 의도를 내더라도 여기서
막히면 브로커로 가지 않습니다. 판단 규칙(언제 사고팔지)은 없습니다.

| 한도 | 매수 | 매도 |
|---|---|---|
| 신규 주문 가능 시간대 | 적용 | 적용 (단, forced 매도는 장중 전체 허용) |
| 장부 불일치·검토 필요 종목 | 차단 | 차단 (사람 확인 전 자동 매매 금지) |
| 계좌 어디든 장부 불일치가 있으면 (8-B) | 전 종목 차단 | — (불일치 없는 종목 매도는 허용) |
| 1회 주문 금액 상한 | 적용 | — |
| 최대 보유 종목 수 | 신규 종목이면 적용 | — |
| 총 노출 상한 (8-B, 아래) | 적용 | — |
| 현금 버퍼 | 적용 | — |
| 매도 수량 ≤ 원장 보유 수량 **그리고** ≤ 이번 잔고 수량 (8-B) | — | 적용 |
| 허용 종목 목록(설정 시) | 적용 | — |

총 노출 (8-B, F3):
  Σ(계좌 전체 보유 종목: max(원장 수량, 잔고 수량) × 현재가)
  + 미체결 매수 예약금(`pending_buy_reserve`)
  + 이번 주문 금액 × (1 + `buy_price_buffer_pct`%)  ← 시장가 체결가 변동 여유
보유 종목 중 하나라도 현재가를 모르면(`prices`에 없음/0) 신규 매수를 보류합니다
(원가로 대신 평가하지 않음 — 상승 시 노출을 과소평가하므로).

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
    buy_price_buffer_pct: float = 1.0       # 시장가 매수 체결가 변동 여유 (금액 한도·현금 버퍼에 적용)

    def __post_init__(self) -> None:
        for name in ("max_positions", "max_order_amount", "max_total_exposure"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name}는 양수")
        if not 0 <= self.buy_price_buffer_pct <= 30:
            raise ValueError("buy_price_buffer_pct는 0~30")
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
    unreconciled_symbols: frozenset[str] | set[str] = frozenset(),
    pending_buy_reserve: int = 0,
    unknown_pending_orders: bool = False,
) -> GuardDecision:
    """주문 의도 하나를 한도에 비춰 허용/차단.

    prices: 계좌 전체 보유 종목(원장 ∪ 잔고)의 현재가. 매수 판단 시 하나라도
      빠지면 PRICE_UNKNOWN으로 보류.
    unreconciled_symbols: 장부 대조에서 차단(BLOCK) 판정된 종목. 하나라도 있으면
      계좌 노출 계산을 믿을 수 없으므로 모든 신규 매수를 막음.
    pending_buy_reserve: 아직 체결되지 않은 매수 주문의 예상 금액 합계.
    unknown_pending_orders: 금액을 알 수 없는 미해결 주문(재시작 전 주문 등)이 있음 —
      예약금을 계산할 수 없으므로 신규 매수 보류.
    """
    if phase not in (MarketPhase.REGULAR,):
        if not (intent.forced and phase == MarketPhase.CLOSING_AUCTION):
            return _deny("OUTSIDE_SESSION", f"장 단계 {phase.value}")
    t = now.time()
    in_window = config.new_orders_start <= t < config.new_orders_end
    if not in_window and not (intent.side == "SELL" and intent.forced):
        return _deny("OUTSIDE_ORDER_WINDOW",
                     f"{t.strftime('%H:%M')} (허용 {config.new_orders_start:%H:%M}~{config.new_orders_end:%H:%M})")
    if intent.symbol in blocked_symbols or intent.symbol in unreconciled_symbols:
        return _deny("SYMBOL_BLOCKED", "장부 불일치 또는 검토 필요 종목 — 사람 확인 전 자동 매매 금지")

    broker_qty = {p.symbol: p.quantity for p in balance.positions if p.quantity > 0}
    if intent.side == "SELL":
        held = positions.get(intent.symbol)
        held_qty = held.quantity if held else 0
        if intent.quantity > held_qty:
            return _deny("SELL_EXCEEDS_LEDGER", f"매도 {intent.quantity}주 > 원장 보유 {held_qty}주")
        if intent.quantity > broker_qty.get(intent.symbol, 0):
            return _deny("SELL_EXCEEDS_BROKER",
                         f"매도 {intent.quantity}주 > 잔고 {broker_qty.get(intent.symbol, 0)}주")
        return GuardDecision(True)

    # ── BUY ──
    if unreconciled_symbols:
        return _deny("ACCOUNT_RECONCILE_BLOCKED",
                     f"장부 불일치 종목 {sorted(unreconciled_symbols)} — 계좌 노출을 확정할 수 없어 신규 매수 보류")
    if unknown_pending_orders:
        return _deny("PENDING_AMOUNT_UNKNOWN", "금액을 알 수 없는 미해결 주문 있음 — 신규 매수 보류")
    if config.allowed_symbols and intent.symbol not in config.allowed_symbols:
        return _deny("SYMBOL_NOT_ALLOWED", "허용 종목 목록에 없음")
    if pending_buy_reserve < 0:
        return _deny("INVALID_RESERVE", f"미체결 매수 예약금이 음수: {pending_buy_reserve}")
    buffered = int(intent.notional * (1 + config.buy_price_buffer_pct / 100))
    if buffered > config.max_order_amount:
        return _deny("ORDER_AMOUNT_LIMIT",
                     f"{intent.notional:,}원(+여유 {config.buy_price_buffer_pct}% = {buffered:,}) > 상한 {config.max_order_amount:,}원")
    held_symbols = {s for s, p in positions.items() if p.quantity > 0} | set(broker_qty)
    if intent.symbol not in held_symbols and len(held_symbols) >= config.max_positions:
        return _deny("MAX_POSITIONS", f"보유 {len(held_symbols)}종목 / 상한 {config.max_positions}")
    missing = sorted(s for s in held_symbols if prices.get(s, 0) <= 0)
    if missing:
        return _deny("PRICE_UNKNOWN", f"보유 종목 현재가 없음 {missing} — 노출 계산 불가, 신규 매수 보류")
    exposure = 0
    for sym in held_symbols:
        ledger_q = positions[sym].quantity if sym in positions else 0
        exposure += max(ledger_q, broker_qty.get(sym, 0)) * prices[sym]
    total = exposure + pending_buy_reserve + buffered
    if total > config.max_total_exposure:
        return _deny("TOTAL_EXPOSURE_LIMIT",
                     f"보유 평가 {exposure:,} + 미체결 매수 {pending_buy_reserve:,} + 주문 {buffered:,} "
                     f"> 상한 {config.max_total_exposure:,}")
    if balance.cash - buffered < config.min_cash_buffer:
        return _deny("CASH_BUFFER", f"현금 {balance.cash:,} - 주문 {buffered:,} < 버퍼 {config.min_cash_buffer:,}")
    return GuardDecision(True)
