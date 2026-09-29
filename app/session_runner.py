from __future__ import annotations

"""하루 수명주기 (스윙 분리 6라운드, 2026-09-28).

장 시작 전에 켜고 장 마감 후 끄는 운영을 전제로, 프로세스 한 번 = 거래일 하루.

    CLOSED_DAY  → 로그 남기고 바로 종료
    PRE_OPEN    → 정규장 시작까지 대기
    REGULAR / CLOSING_AUCTION
                → poll_interval_sec마다 tick():
                  잔고 조회 → 주문 대조(OrderExecutor) → 체결 원장 기록(FillRecorder)
                  → 장부 대조(원장·잔고·메타, 미해결 주문 종목만 수량 차이 보류)
                  → 전략 on_tick → (매수 의도 시 보유 종목 현재가 조회) → 안전 한도 → 주문
    POST_CLOSE  → 미해결 주문이 없어질 때까지(최대 close_reconcile_until) 대조만,
                  마지막 장부 대조·요약 → (선택) 일봉 갱신 → 종료

매매 판단은 `Strategy`에만 있고 기본값 `NullStrategy`는 아무 주문도 내지 않습니다.
이 모듈은 순서와 안전장치만 담당합니다.

테스트를 위해 clock/sleep/should_stop을 주입할 수 있습니다.
"""

import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable

from domain.models import AccountBalance
from domain.position.lifecycle import PositionLifecycle
from domain.position.position_book import META_ORPHAN, ReconcileReport, reconcile
from domain.position.swing_state import PositionMeta
from domain.risk.account_guard import GuardConfig, check_intent
from domain.service.fill_recorder import FillRecorder
from domain.service.lot_ledger import LotMatchError, apply_events
from domain.strategy.interface import OrderIntent, TickContext
from infra.market_data.quote_source import QuoteSource
from infra.storage.fill_ledger import FillLedgerCorruptError
from utils.trading_calendar import MarketPhase, TradingCalendar


@dataclass(frozen=True)
class SessionConfig:
    poll_interval_sec: float = 60.0
    close_reconcile_until: time = time(15, 45)
    max_consecutive_balance_failures_warn: int = 5
    watch_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.poll_interval_sec < 10:
            raise ValueError("poll_interval_sec는 10초 이상 (잔고·체결 조회 한도 보호)")


@dataclass
class SessionSummary:
    trade_date: date | None = None
    status: str = ""                     # CLOSED_DAY / COMPLETED / STOPPED
    ticks: int = 0
    balance_failures: int = 0
    intents: int = 0
    intents_denied: int = 0
    orders_sent: int = 0
    orders_accepted: int = 0
    fills_recorded: int = 0
    halted_reason: str = ""
    unresolved_at_end: bool = False
    final_reconcile: ReconcileReport | None = None
    denied_codes: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"거래일 {self.trade_date} | 상태 {self.status} | 폴링 {self.ticks}회 (잔고 실패 {self.balance_failures})",
            f"주문 의도 {self.intents} (한도 차단 {self.intents_denied}) | 전송 {self.orders_sent} / 접수 {self.orders_accepted}"
            f" | 원장 기록 {self.fills_recorded}건",
        ]
        if self.denied_codes:
            out.append("차단 사유: " + ", ".join(f"{k} {v}" for k, v in sorted(self.denied_codes.items())))
        if self.halted_reason:
            out.append(f"신규 주문 중단: {self.halted_reason}")
        if self.unresolved_at_end:
            out.append("장 마감 후에도 미해결 주문 남음 — 다음 기동 시 ERROR로 복원됨, HTS 확인 필요")
        if self.final_reconcile is not None:
            out.append("장부 대조: " + ("일치" if self.final_reconcile.ok else "불일치")
                       + f" (어긋남 {len(self.final_reconcile.issues)}건)")
        return out


class SessionRunner:
    def __init__(
        self,
        *,
        broker,
        executor,
        state,
        ledger_store,
        recorder: FillRecorder,
        calendar: TradingCalendar,
        strategy,
        guard_config: GuardConfig,
        session_config: SessionConfig,
        logger,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = _time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
        after_close: Callable[[date], None] | None = None,
        quote_source: QuoteSource | None = None,
    ) -> None:
        self.broker = broker
        self.executor = executor
        self.state = state
        self.ledger_store = ledger_store
        self.recorder = recorder
        self.calendar = calendar
        self.strategy = strategy
        self.guard = guard_config
        self.cfg = session_config
        self.log = logger
        self.clock = clock
        self.sleep = sleep
        self.should_stop = should_stop
        self.after_close = after_close
        self.quote_source = quote_source   # None이면 보유 종목이 있을 때 신규 매수 불가(PRICE_UNKNOWN)
        self._tick_prices: dict[str, int] | None = None
        self._unreconciled: frozenset[str] = frozenset()
        self.summary = SessionSummary()
        self._consecutive_balance_failures = 0
        self._halted_reason = ""
        self._last_report: ReconcileReport | None = None
        self.last_balance: AccountBalance | None = None

    # ── 진입점 ──────────────────────────────────────────────

    def run(self) -> SessionSummary:
        now = self.clock()
        phase = self.calendar.phase(now)
        today = now.date()
        self.summary.trade_date = today
        if phase == MarketPhase.CLOSED_DAY:
            self.log.info(f"[SESSION] {today} 휴장일 — {self.calendar.holiday_name(today)} — 종료")
            self.summary.status = "CLOSED_DAY"
            return self.summary
        self.state.last_session_date = today.isoformat()
        self.executor.save_state()

        if phase == MarketPhase.PRE_OPEN:
            self.log.info("[SESSION] 장 시작 전 — 정규장 시작까지 대기")
            if not self._wait_until(lambda: self.calendar.phase(self.clock()) != MarketPhase.PRE_OPEN):
                return self._finish("STOPPED")

        started = False
        while not self.should_stop():
            phase = self.calendar.phase(self.clock())
            if phase not in (MarketPhase.REGULAR, MarketPhase.CLOSING_AUCTION):
                break
            ctx = self._tick(allow_orders=True, call_strategy_start=not started)
            started = started or ctx is not None
            if not self._sleep_interval():
                return self._finish("STOPPED")
        if self.should_stop():
            return self._finish("STOPPED")
        self._close_routine()
        return self._finish("COMPLETED")

    # ── 장중 한 번 ──────────────────────────────────────────

    def _fetch_balance(self) -> AccountBalance | None:
        try:
            balance = self.broker.get_account_balance()
            self._consecutive_balance_failures = 0
            self.last_balance = balance
            return balance
        except Exception as exc:
            self._consecutive_balance_failures += 1
            self.summary.balance_failures += 1
            level = (self.log.critical if self._consecutive_balance_failures
                     >= self.cfg.max_consecutive_balance_failures_warn else self.log.warning)
            level(f"[SESSION] 잔고 조회 실패 {self._consecutive_balance_failures}회 연속 — 이번 폴링은 주문 없음: "
                  f"{type(exc).__name__}: {exc}")
            return None

    def _reconcile_step(self, balance: AccountBalance, now: datetime):
        """주문 대조 → 원장 기록 → 추적 종료 → 장부 대조. (ledger, report) 또는 원장 오류 시 (None, None)."""
        watch = set(self.cfg.watch_symbols) | set(self.recorder.tracked)
        self.executor.sync_with_balance(balance, watch)
        try:
            written = self.recorder.observe(balance, trade_date=now.date(), now=now)
            self.summary.fills_recorded += len(written)
        except FillLedgerCorruptError as exc:
            # 기록하지 못한 체결은 장부 대조에서 불일치로 드러남 — 사람 확인 필요
            self._halt(f"체결 원장 기록 실패 — {exc}")
        psm = self.executor.position_state_machine
        for sym, order in self.recorder.tracked.items():
            st = psm.get(sym)
            if (st.lifecycle in (PositionLifecycle.OPEN, PositionLifecycle.FLAT)
                    and not st.pending_order_id and not st.orphan_order_id):
                self.recorder.stop_tracking(sym)
                if not order.done:
                    self.log.warning(f"[SESSION] {sym} 주문 {order.order_id} 종료 확인 — 체결 "
                                     f"{order.filled_quantity}/{order.requested_quantity}주 (나머지 미체결)")
        try:
            ledger = apply_events(self.ledger_store.load())
        except (FillLedgerCorruptError, LotMatchError) as exc:
            self._halt(f"체결 원장 오류 — {type(exc).__name__}: {exc}")
            return None, None
        in_flight = self._in_flight_symbols()
        report = reconcile(ledger, balance, self.state.positions, in_flight_symbols=in_flight)
        if self._last_report is None or report.lines() != self._last_report.lines():
            for line in report.lines():
                (self.log.critical if line.startswith("[BLOCK]") else self.log.info)(f"[SESSION_RECONCILE] {line}")
        self._last_report = report
        for sym in report.symbols_with(META_ORPHAN):
            if sym not in in_flight:
                self.state.remove_position_meta(sym)
                self.log.info(f"[SESSION] {sym} 청산 완료 — 포지션 메타 정리")
        return ledger, report

    def _in_flight_symbols(self) -> frozenset[str]:
        return self.executor.unresolved_symbols() | frozenset(self.recorder.tracked)

    def _tick(self, *, allow_orders: bool, call_strategy_start: bool = False) -> TickContext | None:
        now = self.clock()
        self._tick_prices = None
        self.summary.ticks += 1
        balance = self._fetch_balance()
        if balance is None:
            return None
        ledger, report = self._reconcile_step(balance, now)
        if ledger is None:
            self.executor.save_state()
            return None
        blocked = set(report.blocking_symbols)
        blocked |= {s for s, m in self.state.positions.items() if m.needs_review}
        self._unreconciled = report.blocking_symbols
        in_flight = self._in_flight_symbols()
        ctx = TickContext(
            now=now, phase=self.calendar.phase(now), trade_date=now.date(), balance=balance,
            positions=ledger.positions(), metas=dict(self.state.positions),
            blocked_symbols=frozenset(blocked),
            orders_in_flight=bool(in_flight) or self.executor.has_unresolved_orders(),
            in_flight_symbols=in_flight,
        )
        if call_strategy_start:
            self._safe_strategy_call("on_session_start", ctx)
        if allow_orders and not self._halted_reason:
            intents = self._safe_strategy_call("on_tick", ctx) or []
            for intent in intents:
                self._handle_intent(intent, ctx)
        self.executor.save_state()
        return ctx

    def _handle_intent(self, intent: OrderIntent, ctx: TickContext) -> None:
        self.summary.intents += 1
        if not isinstance(intent, OrderIntent):
            self.log.critical(f"[SESSION] 전략이 OrderIntent가 아닌 값을 반환 — 무시: {intent!r}")
            self.summary.intents_denied += 1
            return
        prices: dict[str, int] = {}
        reserve, unknown_pending = 0, False
        if intent.side == "BUY":
            prices = self._held_prices(ctx)
            reserve, unknown_pending = self._pending_buy_reserve()
        decision = check_intent(intent, config=self.guard, now=ctx.now, phase=ctx.phase, balance=ctx.balance,
                                positions=ctx.positions, prices=prices, blocked_symbols=ctx.blocked_symbols,
                                unreconciled_symbols=self._unreconciled, pending_buy_reserve=reserve,
                                unknown_pending_orders=unknown_pending)
        if not decision.allowed:
            self.summary.intents_denied += 1
            self.summary.denied_codes[decision.code] = self.summary.denied_codes.get(decision.code, 0) + 1
            self.log.info(f"[GUARD] {intent.symbol} {intent.side} {intent.quantity}주 차단 | {decision.code} | {decision.detail}")
            return
        held = next((p for p in ctx.balance.positions if p.symbol == intent.symbol), None)
        base_qty = held.quantity if held else 0
        base_cost = held.quantity * held.average_price if held else 0
        context = {"entry_strategy": getattr(self.strategy, "strategy_id", ""), **intent.context}
        if intent.side == "BUY":
            context.setdefault("entry_reason", intent.reason)
            sub = self.executor.submit_buy(intent.symbol, intent.quantity, intent.reference_price, context=context)
        else:
            sub = self.executor.submit_sell(intent.symbol, intent.quantity, intent.reference_price,
                                            exit_reason=intent.reason,
                                            avg_buy_price=held.average_price if held else 0,
                                            forced=intent.forced, context=context)
        if sub.sent:
            self.summary.orders_sent += 1
        if not sub.accepted:
            self.log.warning(f"[SESSION] {intent.symbol} {intent.side} 미접수 | {sub.block_code or (sub.result.message if sub.result else '')}")
            return
        self.summary.orders_accepted += 1
        self.recorder.track(intent.symbol, intent.side, sub.result.order_id, intent.quantity,
                            intent.reference_price, base_quantity=base_qty, base_cost=base_cost)
        if intent.side == "BUY" and intent.symbol not in self.state.positions:
            self.state.upsert_position_meta(PositionMeta(
                intent.symbol, strategy_id=getattr(self.strategy, "strategy_id", ""), origin="ORDER"))

    def _held_prices(self, ctx: TickContext) -> dict[str, int]:
        """계좌 전체 보유(원장 ∪ 잔고) 현재가. 한 폴링에 한 번만 조회."""
        if self._tick_prices is None:
            held = {s for s, p in ctx.positions.items() if p.quantity > 0} | {
                p.symbol for p in ctx.balance.positions if p.quantity > 0}
            if not held:
                self._tick_prices = {}
            elif self.quote_source is None:
                self._tick_prices = {}
            else:
                try:
                    self._tick_prices = dict(self.quote_source.get_prices(held))
                except Exception as exc:
                    self.log.warning(f"[SESSION] 현재가 조회 실패 — 이번 폴링 신규 매수 보류: {type(exc).__name__}: {exc}")
                    self._tick_prices = {}
        return self._tick_prices

    def _pending_buy_reserve(self) -> tuple[int, bool]:
        """(미체결 매수 예상 금액, 금액을 알 수 없는 미해결 주문 존재 여부)."""
        buf = 1 + self.guard.buy_price_buffer_pct / 100
        reserve = sum(int(max(o.requested_quantity - o.filled_quantity, 0) * o.reference_price * buf)
                      for o in self.recorder.tracked.values() if o.side == "BUY")
        unknown = bool(self.executor.unresolved_symbols() - frozenset(self.recorder.tracked))
        return reserve, unknown

    def _safe_strategy_call(self, name: str, ctx: TickContext):
        try:
            return getattr(self.strategy, name)(ctx)
        except Exception as exc:
            self.log.critical(f"[STRATEGY_ERROR] {name} 예외 — 이번 폴링은 주문 없음: {type(exc).__name__}: {exc}")
            return None

    def _halt(self, reason: str) -> None:
        """신규 주문 중단 (이번 프로세스 끝까지). 대조·기록은 계속. 첫 사유를 유지."""
        if self._halted_reason:
            return
        self.log.critical(f"[SESSION_HALT] 신규 주문 중단: {reason}")
        self._halted_reason = reason
        self.summary.halted_reason = reason

    # ── 마감 후 ─────────────────────────────────────────────

    def _close_routine(self) -> None:
        self.log.info("[SESSION] 정규장 종료 — 미해결 주문 대조")
        deadline = datetime.combine(self.clock().date(), self.cfg.close_reconcile_until)
        ctx = None
        while not self.should_stop():
            ctx = self._tick(allow_orders=False) or ctx
            unresolved = self.executor.has_unresolved_orders() or bool(self.recorder.tracked)
            if not unresolved or self.clock() >= deadline:
                break
            if not self._sleep_interval():
                break
        self.summary.unresolved_at_end = self.executor.has_unresolved_orders() or bool(self.recorder.tracked)
        for sym, order in self.recorder.tracked.items():
            self.log.critical(f"[SESSION] {sym} 주문 {order.order_id} 마감 후에도 추적 중 — 체결 "
                              f"{order.filled_quantity}/{order.requested_quantity}주, 원장·HTS 확인 필요")
        self.summary.final_reconcile = self._last_report
        if ctx is not None:
            self._safe_strategy_call("on_session_end", ctx)
        self.executor.save_state()
        if self.after_close is not None:
            try:
                self.after_close(self.clock().date())
            except Exception as exc:
                self.log.error(f"[SESSION] 마감 후 작업 실패(매매 결과에는 영향 없음): {type(exc).__name__}: {exc}")

    @property
    def last_reconcile(self) -> ReconcileReport | None:
        return self._last_report

    def _finish(self, status: str) -> SessionSummary:
        self.summary.status = status
        self.executor.save_state()
        for line in self.summary.lines():
            self.log.info(f"[SESSION_SUMMARY] {line}")
        return self.summary

    # ── 대기 ────────────────────────────────────────────────

    def _sleep_interval(self) -> bool:
        """poll_interval 동안 1초 단위로 쉬며 중지 요청 확인. 중지되면 False."""
        end = self.clock() + timedelta(seconds=self.cfg.poll_interval_sec)
        while self.clock() < end:
            if self.should_stop():
                return False
            self.sleep(min(1.0, (end - self.clock()).total_seconds()))
        return not self.should_stop()

    def _wait_until(self, cond: Callable[[], bool]) -> bool:
        while not cond():
            if self.should_stop():
                return False
            self.sleep(1.0)
        return True
