from __future__ import annotations

"""포지션 장부 대조 (스윙 분리 4라운드, 2026-09-28).

세 출처를 맞춰봅니다.
  1. 체결 원장 → 보유 수량·원가 (`LedgerResult`)
  2. 브로커 잔고 → 실제 수량·평균단가 (`AccountBalance`)
  3. 포지션 메타 → 전략 ID·손절가 등 (`SwingState.positions`)

**보고만 하고 고치지 않습니다.** 어긋남을 자동으로 메우면(예: 잔고에 있으니
원장에 BUY를 추가) 원인을 모르는 채로 장부가 "맞아" 보이게 됩니다. 사람이
확인한 뒤 명시적으로 사건을 추가하도록 `opening_events_from_balance()` 같은
도우미만 제공합니다.

주문이 진행 중인 종목은 잔고와 원장이 잠시 다를 수 있습니다 — 호출부가 그
종목을 `in_flight_symbols`로 넘기면 **그 종목의** 수량 불일치만 참고(INFO)로
표시합니다. 다른 종목의 불일치는 계속 차단(BLOCK)입니다(8-B, F1: 이전에는
계좌 전체 bool 하나라 B종목 주문 중에 A종목 불일치도 풀렸음).
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from domain.models import AccountBalance
from domain.position.fill_event import FillEvent
from domain.position.swing_state import PositionMeta
from domain.service.lot_ledger import LedgerResult

# 어긋남 종류
QTY_MISMATCH = "QTY_MISMATCH"              # 원장 수량 ≠ 잔고 수량
UNTRACKED_HOLDING = "UNTRACKED_HOLDING"    # 잔고엔 있는데 원장엔 없음 (HTS 수동 매매 등)
LEDGER_ONLY = "LEDGER_ONLY"                # 원장엔 있는데 잔고엔 없음
META_MISSING = "META_MISSING"              # 원장 보유인데 메타 없음
META_ORPHAN = "META_ORPHAN"                # 메타는 있는데 원장·잔고 모두 보유 없음
AVG_PRICE_GAP = "AVG_PRICE_GAP"            # 원장 평균단가와 잔고 평균단가 차이 (참고)

BLOCKING_KINDS = (QTY_MISMATCH, UNTRACKED_HOLDING, LEDGER_ONLY)


@dataclass(frozen=True)
class ReconcileIssue:
    kind: str
    symbol: str
    detail: str
    blocking: bool


@dataclass
class ReconcileReport:
    issues: list[ReconcileIssue] = field(default_factory=list)
    in_flight_symbols: frozenset[str] = frozenset()

    @property
    def orders_in_flight(self) -> bool:
        return bool(self.in_flight_symbols)

    @property
    def blocking_symbols(self) -> frozenset[str]:
        return frozenset(i.symbol for i in self.issues if i.blocking)

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)

    def symbols_with(self, kind: str) -> list[str]:
        return sorted(i.symbol for i in self.issues if i.kind == kind)

    def lines(self) -> list[str]:
        return [f"[{'BLOCK' if i.blocking else 'INFO '}] {i.kind} {i.symbol} | {i.detail}"
                for i in self.issues]


def reconcile(
    ledger: LedgerResult,
    balance: AccountBalance,
    metas: dict[str, PositionMeta],
    *,
    in_flight_symbols: frozenset[str] | set[str] = frozenset(),
    avg_price_tolerance_pct: float = 0.5,
) -> ReconcileReport:
    """원장·잔고·메타를 대조한 보고서. 아무것도 수정하지 않습니다.

    blocking=True인 어긋남이 있으면 호출부는 해당 종목(또는 전체) 자동 매매를
    멈추고 사람 확인을 요청해야 합니다. 단, in_flight_symbols에 든 종목의 수량
    관련 어긋남은 blocking=False(그 종목 주문의 체결 반영 지연일 수 있음).
    """
    in_flight = frozenset(in_flight_symbols)
    report = ReconcileReport(in_flight_symbols=in_flight)
    positions = ledger.positions()
    broker = {p.symbol: p for p in balance.positions if p.quantity > 0}

    for sym in sorted(set(positions) | set(broker)):
        lp, bp = positions.get(sym), broker.get(sym)
        qty_blocking = sym not in in_flight
        if lp and not bp:
            report.issues.append(ReconcileIssue(
                LEDGER_ONLY, sym, f"원장 {lp.quantity}주, 잔고 없음", qty_blocking))
        elif bp and not lp:
            report.issues.append(ReconcileIssue(
                UNTRACKED_HOLDING, sym,
                f"잔고 {bp.quantity}주(평균 {bp.average_price:,}원), 원장 없음 — "
                f"인수하려면 사람이 확인 후 OPENING 사건 기록", qty_blocking))
        elif lp and bp:
            if lp.quantity != bp.quantity:
                report.issues.append(ReconcileIssue(
                    QTY_MISMATCH, sym, f"원장 {lp.quantity}주 ≠ 잔고 {bp.quantity}주", qty_blocking))
            elif bp.average_price > 0:
                gap = abs(lp.avg_price - bp.average_price) / bp.average_price * 100
                if gap > avg_price_tolerance_pct:
                    report.issues.append(ReconcileIssue(
                        AVG_PRICE_GAP, sym,
                        f"원장 평균 {lp.avg_price:,.0f}원 vs 잔고 평균 {bp.average_price:,}원 "
                        f"({gap:.2f}%{', 추정가 포함' if lp.includes_estimate else ''})", False))
        if lp and sym not in metas:
            report.issues.append(ReconcileIssue(META_MISSING, sym, "원장 보유인데 포지션 메타 없음", False))

    for sym in sorted(metas):
        if sym not in positions and sym not in broker:
            report.issues.append(ReconcileIssue(
                META_ORPHAN, sym, "메타만 남음(원장·잔고 보유 없음) — 청산 후 정리 누락", False))
    return report


def opening_events_from_balance(
    balance: AccountBalance,
    symbols: list[str],
    trade_date: date,
    *,
    occurred_at: datetime | None = None,
    note: str = "",
) -> list[FillEvent]:
    """사람이 확인한 기존 보유분을 원장에 인수하기 위한 OPENING 사건을 만듭니다.

    원가는 잔고 평균단가(BROKER_AVG). 호출부가 FillLedgerStore.append()로 기록합니다.
    trade_date는 실제 매수일을 모르면 인수한 날로 둡니다(보유 거래일수 기준이 됨 —
    note에 그 사실을 남기세요).
    """
    occurred_at = occurred_at or datetime.now()
    held = {p.symbol: p for p in balance.positions if p.quantity > 0}
    events = []
    for sym in symbols:
        p = held.get(sym)
        if p is None:
            raise ValueError(f"{sym}: 잔고에 없음 — 인수할 수 없음")
        if p.average_price <= 0:
            raise ValueError(f"{sym}: 잔고 평균단가가 0 — 원가를 알 수 없음")
        events.append(FillEvent(
            event_id=f"OPENING:{sym}:{trade_date.isoformat()}",
            kind="OPENING", symbol=sym, quantity=p.quantity, price=p.average_price,
            price_source="BROKER_AVG", trade_date=trade_date, occurred_at=occurred_at,
            note=note or "기존 보유분 인수(사람 확인)",
        ))
    return events
