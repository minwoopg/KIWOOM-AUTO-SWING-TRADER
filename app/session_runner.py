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

마감 검증 (8-D, F5): 프로세스가 끝까지 돈 것(status=COMPLETED)과 마감 검증이
성공한 것(close_check=VERIFIED)을 구분합니다. 마감 이후 잔고 조회가 한 번도
성공하지 못하면 제한 횟수만큼 재시도하고, 그래도 실패하거나 최종 대조가 마감
이후의 것이 아니거나, 불일치·미해결 주문·신규 주문 중단·일봉 갱신 실패가 있으면
NEEDS_REVIEW입니다. 이전 폴링의 대조 결과를 최종 결과로 쓰지 않습니다.

매매 판단은 `Strategy`에만 있고 기본값 `NullStrategy`는 아무 주문도 내지 않습니다.
이 모듈은 순서와 안전장치만 담당합니다.

테스트를 위해 clock/sleep/should_stop을 주입할 수 있습니다.
"""

import math
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
    close_balance_retries: int = 3          # 마감 후 잔고 조회 실패 시 추가 시도 횟수
    close_balance_retry_sec: float = 15.0
    # M3 A2: 지정가 진입 마감 — 이 시각 이후 신규 지정가 매수 거부, 이 실행이 낸 미체결 지정가 매수 취소.
    # None이면 아무것도 하지 않음(기본 — 기존 동작 그대로).
    entry_cancel_at: time | None = None
    # M3 B: 매수 의도 수량 상한(최소판 1). None이면 검사 안 함(기존 동작).
    max_buy_quantity: int | None = None

    def __post_init__(self) -> None:
        if self.poll_interval_sec < 10:
            raise ValueError("poll_interval_sec는 10초 이상 (잔고·체결 조회 한도 보호)")
        if self.close_balance_retries < 0 or self.close_balance_retry_sec < 0:
            raise ValueError("close_balance_retries / close_balance_retry_sec는 0 이상")


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
    # 8-D (F5): 마감 검증
    run_id: str = ""                     # 8-E (R3): 이번 실행 식별자
    close_check: str = ""                # "" (휴장일) / VERIFIED / NEEDS_REVIEW / NOT_RUN (마감 전 중지)
    close_issues: list[str] = field(default_factory=list)
    final_balance_at: datetime | None = None
    final_reconcile_at: datetime | None = None
    daily_bars: str = "SKIPPED"          # SKIPPED / OK / FAILED: ...
    report: str = ""                     # OK / FAILED: ... (run_session이 채움)
    execution_mode: str = ""             # M0 커밋 1: MOCK_LOCAL / KIWOOM_PAPER (run_session이 채움)
    account_scope: str = ""              # 비민감 계좌 라벨(계좌번호 아님)
    cancels_requested: int = 0           # M3 A2: 지정가 매수 취소 요청(접수·거절·불명 포함)
    shadow_intents: int = 0              # M3 B: 그림자 모드 — 전송하지 않고 기록한 주문 의도
    strategy_mode: str = ""              # off / shadow / paper

    def add_close_issue(self, issue: str) -> None:
        if issue not in self.close_issues:
            self.close_issues.append(issue)
        if self.close_check in ("VERIFIED", ""):
            self.close_check = "NEEDS_REVIEW"

    @property
    def needs_attention(self) -> bool:
        """종료 코드 2 대상: 마감 검증 실패 또는 (마감 전 중지 등에서도) 기록된 문제."""
        return self.close_check == "NEEDS_REVIEW" or bool(self.close_issues)

    def to_status_dict(self) -> dict:
        iso = lambda v: v.isoformat(timespec="seconds") if v else None
        return {
            "run_id": self.run_id, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "execution_mode": self.execution_mode, "account_scope": self.account_scope,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "status": self.status, "close_check": self.close_check, "close_issues": list(self.close_issues),
            "final_balance_at": iso(self.final_balance_at), "final_reconcile_at": iso(self.final_reconcile_at),
            "final_reconcile_ok": None if self.final_reconcile is None else self.final_reconcile.ok,
            "unresolved_at_end": self.unresolved_at_end, "halted_reason": self.halted_reason,
            "daily_bars": self.daily_bars, "report": self.report,
            "ticks": self.ticks, "balance_failures": self.balance_failures,
            "orders_sent": self.orders_sent, "orders_accepted": self.orders_accepted,
            "fills_recorded": self.fills_recorded, "cancels_requested": self.cancels_requested,
            "strategy_mode": self.strategy_mode, "shadow_intents": self.shadow_intents,
        }

    def lines(self) -> list[str]:
        out = []
        if self.execution_mode:
            out.append(f"실행 모드 {self.execution_mode} | 계좌 라벨 {self.account_scope or '-'}")
        out += [
            f"거래일 {self.trade_date} | 상태 {self.status} | 폴링 {self.ticks}회 (잔고 실패 {self.balance_failures})",
            f"주문 의도 {self.intents} (한도 차단 {self.intents_denied}) | 전송 {self.orders_sent} / 접수 {self.orders_accepted}"
            f" | 원장 기록 {self.fills_recorded}건"
            + (f" | 지정가 취소 요청 {self.cancels_requested}" if self.cancels_requested else ""),
        ]
        if self.strategy_mode and self.strategy_mode != "off":
            out.append(f"전략 모드 {self.strategy_mode}"
                       + (f" | 그림자 의도 {self.shadow_intents}건(전송 안 함)" if self.strategy_mode == "shadow" else ""))
        if self.denied_codes:
            out.append("차단 사유: " + ", ".join(f"{k} {v}" for k, v in sorted(self.denied_codes.items())))
        if self.halted_reason:
            out.append(f"신규 주문 중단: {self.halted_reason}")
        if self.unresolved_at_end:
            out.append("장 마감 후에도 미해결 주문 남음 — 다음 기동 시 ERROR로 복원됨, HTS 확인 필요")
        if self.final_reconcile is not None:
            out.append("장부 대조: " + ("일치" if self.final_reconcile.ok else "불일치")
                       + f" (어긋남 {len(self.final_reconcile.issues)}건)")
        if self.close_check:
            out.append(f"마감 검증: {self.close_check}"
                       + (f" — {', '.join(self.close_issues)}" if self.close_issues else "")
                       + f" | 일봉 갱신 {self.daily_bars}")
        return out


class SessionRunner:
    MAX_BUY_DECISION_AGE_SEC = 30      # H2: 폴링 시작 → 매수 전송 직전까지 허용 시간(넘으면 STALE_DECISION)
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
        status_file=None,
        commands_dir: str | None = None,
        meta: dict | None = None,
        send_orders: bool = True,
        shadow_sink: Callable[[dict], None] | None = None,
        risk_guard=None,
    ) -> None:
        self.risk_guard = risk_guard          # C3: 일일 손실·실패 주문 한도(app/daily_risk.DailyRiskGuard), None이면 없음
        self._risk_block: str | None = None
        self._evidence_recovered = False
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
        self._last_balance_ok_at: datetime | None = None
        self._last_reconcile_at: datetime | None = None
        self._unreconciled: frozenset[str] = frozenset()
        self.summary = SessionSummary()
        self._consecutive_balance_failures = 0
        self._halted_reason = ""
        self._last_report: ReconcileReport | None = None
        self.last_balance: AccountBalance | None = None
        self.last_balance_error = ""        # 마지막 잔고 조회 실패 사유 (M1 결과 표시용)
        # M3: 상태 파일(대시보드)·신규 매수 중단 명령
        import os as _os
        import uuid as _uuid
        self.run_id = _uuid.uuid4().hex[:12]
        self.pid = _os.getpid()
        self.started_at = self.clock()
        self.status_file = status_file
        self.commands_dir = commands_dir
        self.meta = dict(meta or {})
        self.stage = "STARTING"
        self._last_status_at: datetime | None = None
        self.buy_halt: dict | None = None
        self._limit_buy_orders: dict[str, str] = {}     # M3 A2: 이 실행이 낸 지정가 매수 (주문번호 → 종목)
        self._cancel_results: dict[str, str] = {}       # 종목 → 마지막 취소 요청 결과 코드
        # M3 B: 그림자 모드 — 의도·안전 한도 판정까지 하고 전송하지 않음(기록만)
        self.send_orders = send_orders
        self.shadow_sink = shadow_sink

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

        self._refresh_buy_halt(now)
        self._set_stage("WAITING_OPEN" if phase == MarketPhase.PRE_OPEN else "RUNNING")
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
        self._set_stage("CLOSE_RECONCILE")
        self._close_routine()
        return self._finish("COMPLETED")

    # ── M1 수동 주문 실측 (M0 커밋 3) ───────────────────────

    def run_probe(self, intent: OrderIntent, *, max_wait_sec: float = 180.0, poll_sec: float = 3.0) -> dict:
        """주문 의도 하나를 하루 수명주기와 **같은 경로**(잔고 → 대조 → 원장 기록 → 안전 한도 → 주문 실행부)로 한 번 보내고,
        체결·원장 기록·주문 종료가 확인될 때까지(최대 max_wait_sec) 대조만 반복합니다.

        전략 게이트만 없음(수동 시험) — 계좌·복구·금액·시간대 안전 한도는 그대로. 재전송·재주문 없음.
        반환 status: REFUSED(장 단계·잔고·원장 문제로 시도 안 함) / DENIED(안전 한도·실행부 차단, 전송 없음) /
        NOT_ACCEPTED(전송했지만 미접수 — 응답 불명 포함) / FILLED(체결 기록·주문 종료) / CLOSED_NO_FILL(체결 없이 종료) /
        UNRESOLVED(시간 안에 종료 확인 못함 — 재시작하면 ERROR로 복원, 사람 확인).
        """
        now = self.clock()
        self.summary.trade_date = now.date()
        phase = self.calendar.phase(now)
        if phase != MarketPhase.REGULAR:
            return {"status": "REFUSED", "reason": f"장 단계 {phase.value} — 정규장에서만"}
        original, sent = self.strategy, []

        class _Once:
            strategy_id = "m1_probe"

            def on_session_start(self, ctx):
                return None

            def on_session_end(self, ctx):
                return None

            def on_tick(self, ctx):
                if sent:
                    return []
                sent.append(intent)
                return [intent]

        self.strategy = _Once()
        ctx = None
        try:
            # 잔고 조회 실패(예: 직전 호출과 붙어 429)면 주문 의도가 만들어지기 전이라 다시 해도 중복 전송이 없음 —
            # poll 간격(최소 2초)을 두고 최대 3번. 원장 오류(중단)면 다시 하지 않음.
            for attempt in range(3):
                if attempt and not self._sleep_seconds(max(poll_sec, 2.0)):
                    break
                ctx = self._tick(allow_orders=True)
                if ctx is not None or sent or self._halted_reason:
                    break
        finally:
            self.strategy = original
        s = self.summary
        base = {"denied": dict(s.denied_codes), "orders_sent": s.orders_sent, "halted": s.halted_reason}
        if ctx is None or not sent:
            why = s.halted_reason or (f"잔고 조회 실패 {s.balance_failures}회 — {self.last_balance_error}"
                                      if self.last_balance_error else "잔고 조회·원장 확인 실패")
            return {"status": "REFUSED", "reason": why, **base}
        if s.intents_denied:
            return {"status": "DENIED", "reason": ", ".join(s.denied_codes), **base}
        if not s.orders_accepted:
            last = self.executor.last_order_attempt(intent.symbol)
            return {"status": "NOT_ACCEPTED" if s.orders_sent else "DENIED",
                    "reason": (last.message if last else "주문 실행부 차단"),
                    "ambiguous": bool(last and last.is_ambiguous), **base}
        order_id = next((o.order_id for o in self.recorder.tracked.values()), "")
        deadline = self.clock() + timedelta(seconds=max_wait_sec)
        done = False
        while self.clock() < deadline and not self.should_stop():
            if not self._sleep_seconds(poll_sec):
                break
            self._tick(allow_orders=False)
            if not self.recorder.tracked and not self.executor.has_unresolved_orders():
                done = True
                break
        self.executor.save_state()
        rep = self._last_report
        status = ("FILLED" if s.fills_recorded else "CLOSED_NO_FILL") if done else "UNRESOLVED"
        return {"status": status,
                "order_id": order_id, "fills_recorded": s.fills_recorded,
                "reconcile_ok": None if rep is None else rep.ok, "unresolved": not done, **base}

    # ── 장중 한 번 ──────────────────────────────────────────

    def _fetch_balance(self) -> AccountBalance | None:
        try:
            balance = self.broker.get_account_balance()
            if self._consecutive_balance_failures:
                # E1: 연속 실패 뒤 복구 시점을 남김(10/21 10:25~10:37 장애는 복구 시각이 로그에 없었음)
                since = getattr(self, "_balance_fail_since", None)
                self.log.warning(f"[SESSION] 잔고 조회 복구 — 연속 실패 {self._consecutive_balance_failures}회"
                                 + (f" ({since:%H:%M:%S}부터)" if since else ""))
            self._balance_fail_since = None
            self._consecutive_balance_failures = 0
            self.last_balance = balance
            self._last_balance_ok_at = self.clock()
            return balance
        except Exception as exc:
            if not self._consecutive_balance_failures:
                self._balance_fail_since = self.clock()
            self._consecutive_balance_failures += 1
            self.summary.balance_failures += 1
            self.last_balance_error = f"{type(exc).__name__}: {exc}"
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
        self._last_reconcile_at = now
        for sym in report.symbols_with(META_ORPHAN):
            if sym not in in_flight:
                self.state.remove_position_meta(sym)
                self.log.info(f"[SESSION] {sym} 청산 완료 — 포지션 메타 정리")
        return ledger, report

    def _in_flight_symbols(self) -> frozenset[str]:
        return self.executor.unresolved_symbols() | frozenset(self.recorder.tracked)

    def _tick(self, *, allow_orders: bool, call_strategy_start: bool = False) -> TickContext | None:
        try:
            return self._tick_inner(allow_orders=allow_orders, call_strategy_start=call_strategy_start)
        finally:
            self._beat(force=True)

    def _tick_inner(self, *, allow_orders: bool, call_strategy_start: bool = False) -> TickContext | None:
        now = self.clock()
        self._tick_prices = None
        self.summary.ticks += 1
        self._refresh_buy_halt(now)
        balance = self._fetch_balance()
        if balance is None:
            return None
        if not self._evidence_recovered and hasattr(self.recorder, "recover_unconfirmed"):
            self._evidence_recovered = True
            try:
                if hasattr(self.recorder, "restore_spool"):
                    self.recorder.restore_spool(now)          # C4: 저장 대기 근거 먼저(재조회 없음)
                self.recorder.recover_unconfirmed(self.ledger_store.load(), now.date(), now)
            except Exception as exc:                     # noqa: BLE001 — 근거 복구 실패가 실행을 멈추지 않음
                self.log.warning(f"[FILL_RECORDER] 재시작 복구 실패: {type(exc).__name__}: {exc}")
        ledger, report = self._reconcile_step(balance, now)
        if ledger is None:
            self.executor.save_state()
            return None
        blocked = set(report.blocking_symbols)
        blocked |= {s for s, m in self.state.positions.items() if m.needs_review}
        blocked |= self.executor.recovery_blocked_symbols()      # M0 커밋 2: ERROR·orphan 이중 차단
        self._unreconciled = report.blocking_symbols
        in_flight = self._in_flight_symbols()
        self._sync_entry_dates(ledger.positions())
        ctx = TickContext(
            now=now, phase=self.calendar.phase(now), trade_date=now.date(), balance=balance,
            positions=ledger.positions(), metas=dict(self.state.positions),
            blocked_symbols=frozenset(blocked),
            orders_in_flight=bool(in_flight) or self.executor.has_unresolved_orders(),
            in_flight_symbols=in_flight,
        )
        if self.risk_guard is not None:
            try:
                self._risk_block = self.risk_guard.step(now, ctx.trade_date, ctx.positions, balance,
                                                        self._held_prices(ctx))
            except Exception as exc:                     # noqa: BLE001 — 모르면 신규 매수 없음
                self._risk_block = "DAILY_RISK_UNKNOWN"
                self.log.critical(f"[DAILY_RISK] 확인 실패 — 신규 매수 보류: {type(exc).__name__}: {exc}")
        if allow_orders:
            self._cancel_expired_entries(now)
        if call_strategy_start:
            self._safe_strategy_call("on_session_start", ctx)
        if allow_orders and not self._halted_reason:
            intents = self._safe_strategy_call("on_tick", ctx) or []
            for intent in intents:
                self._handle_intent(intent, ctx)
            self._after_tick(ctx)
        self.executor.save_state()
        return ctx

    def _after_tick(self, ctx: TickContext) -> None:
        """H2(GPT 10/27): 그 폴링의 주문 의도를 모두 처리(전송)한 뒤 전략의 부가 작업(주문 미허용 후보 가격 관찰).
        주문 경로와 분리 — 예외는 기록만 하고 계속."""
        hook = getattr(self.strategy, "after_tick", None)
        if hook is None:
            return
        try:
            hook(ctx, self.clock)
        except Exception as exc:                         # noqa: BLE001
            self.log.warning(f"[STRATEGY] after_tick 예외(매매 영향 없음): {type(exc).__name__}: {exc}")

    def _shadow(self, intent: OrderIntent, ctx: TickContext, *, allowed: bool, code: str = "", detail: str = "") -> None:
        """그림자 모드 기록 — 전송하지 않음. 판정(허용/차단 사유)과 의도 전체를 남김."""
        if self.send_orders:
            return
        if allowed:
            self.summary.shadow_intents += 1
        rec = {"at": ctx.now.isoformat(timespec="seconds"), "run_id": self.run_id, "symbol": intent.symbol,
               "side": intent.side, "quantity": intent.quantity, "order_type": intent.order_type,
               "limit_price": intent.limit_price, "reference_price": intent.reference_price, "reason": intent.reason,
               "context": intent.context, "would_send": allowed, "deny_code": code, "deny_detail": detail}
        self.log.info(f"[SHADOW] {intent.symbol} {intent.side} {intent.quantity}주 "
                      f"{intent.order_type} {intent.limit_price or ''} → "
                      + ("전송했을 것(그림자 — 전송 안 함)" if allowed else f"차단 {code}"))
        if self.shadow_sink is not None:
            try:
                self.shadow_sink(rec)
            except Exception as exc:                     # noqa: BLE001 — 기록 실패가 실행을 멈추지 않음
                self.log.warning(f"[SHADOW] 기록 실패: {type(exc).__name__}: {exc}")

    def _handle_intent(self, intent: OrderIntent, ctx: TickContext) -> None:
        """의도 하나 처리 + 결과를 전략에 알림(C2 — 신호 키 상태 줄).

        SHADOW / GUARD_REJECTED:<사유> / SUBMITTED / NOT_ACCEPTED[:<차단>] / UNKNOWN(응답 불명·예외)."""
        self.summary.intents += 1
        if not isinstance(intent, OrderIntent):
            self.log.critical(f"[SESSION] 전략이 OrderIntent가 아닌 값을 반환 — 무시: {intent!r}")
            self.summary.intents_denied += 1
            return
        try:
            status, detail = self._handle_intent_inner(intent, ctx)
        except Exception as exc:
            self._report_intent(intent, "UNKNOWN", f"{type(exc).__name__}: {exc}")
            self._order_failed(intent, ctx, "UNKNOWN", f"{type(exc).__name__}: {exc}")
            raise
        self._report_intent(intent, status, detail)
        if status == "UNKNOWN" or status.startswith("NOT_ACCEPTED"):
            self._order_failed(intent, ctx, status, detail)

    def _order_failed(self, intent: OrderIntent, ctx: TickContext, status: str, detail: str) -> None:
        if self.risk_guard is None:
            return
        try:
            block = self.risk_guard.on_order_failure(ctx.now, ctx.trade_date, intent, status, detail)
        except Exception as exc:                          # noqa: BLE001
            block = "DAILY_RISK_UNKNOWN"
            self.log.critical(f"[DAILY_RISK] 주문 실패 반영 실패 — 신규 매수 보류: {type(exc).__name__}: {exc}")
        if block:
            self._risk_block = block

    def _report_intent(self, intent: OrderIntent, status: str, detail: str) -> None:
        hook = getattr(self.strategy, "on_intent_result", None)
        if hook is None:
            return
        try:
            hook(intent, status, detail)
        except Exception as exc:                          # noqa: BLE001 — 기록 실패가 실행을 멈추지 않음
            self.log.warning(f"[SESSION] 전략 결과 통지 실패: {type(exc).__name__}: {exc}")

    def _entry_meta(self, intent: OrderIntent) -> PositionMeta | None:
        """전략 의도의 진입 정보 → 포지션 메타(C2). 손절 기준이 없으면 None(일반 전략 — 기존 동작)."""
        c = intent.context or {}
        stop_ref = c.get("stop_ref")
        if stop_ref is None:
            return None
        try:
            stop = int(math.floor(float(stop_ref)))
        except (TypeError, ValueError):
            return None
        if stop <= 0:
            return None
        keep = ("signal_id", "signal_key", "signal_date", "contract_hash", "scan_run_id", "entry_cap", "stop_ref",
                "strategy_mode", "pre_order_price", "pre_order_ask")
        info = {k: c[k] for k in keep if k in c}
        info.update({"limit_price": intent.limit_price, "order_type": intent.order_type, "entry_status": "SUBMITTING",
                     "run_id": self.run_id})
        return PositionMeta(intent.symbol, strategy_id=getattr(self.strategy, "strategy_id", ""), stop_price=stop,
                            origin="ORDER", meta=info)

    def _handle_intent_inner(self, intent: OrderIntent, ctx: TickContext) -> tuple[str, str]:
        if intent.side == "BUY" and self.buy_halt:
            # M3: 신규 매수 중단 — 매도·손절·미해결 주문 확인은 그대로
            self.summary.intents_denied += 1
            self.summary.denied_codes["BUY_HALTED"] = self.summary.denied_codes.get("BUY_HALTED", 0) + 1
            self.log.info(f"[GUARD] {intent.symbol} BUY {intent.quantity}주 차단 | BUY_HALTED | "
                          f"요청 {self.buy_halt.get('request_id')}")
            self._shadow(intent, ctx, allowed=False, code="BUY_HALTED")
            return "GUARD_REJECTED:BUY_HALTED", ""
        if intent.side == "BUY" and self._risk_block:
            # C3: 일일 손실·실패 주문 한도 — 매도·손절은 그대로
            self.summary.intents_denied += 1
            self.summary.denied_codes[self._risk_block] = self.summary.denied_codes.get(self._risk_block, 0) + 1
            self.log.info(f"[GUARD] {intent.symbol} BUY {intent.quantity}주 차단 | {self._risk_block}")
            self._shadow(intent, ctx, allowed=False, code=self._risk_block)
            return f"GUARD_REJECTED:{self._risk_block}", ""
        if (intent.side == "BUY" and self.cfg.max_buy_quantity is not None
                and intent.quantity > self.cfg.max_buy_quantity):
            self.summary.intents_denied += 1
            self.summary.denied_codes["QTY_LIMIT"] = self.summary.denied_codes.get("QTY_LIMIT", 0) + 1
            self.log.critical(f"[GUARD] {intent.symbol} BUY {intent.quantity}주 차단 | QTY_LIMIT | "
                              f"최소판 상한 {self.cfg.max_buy_quantity}주")
            self._shadow(intent, ctx, allowed=False, code="QTY_LIMIT")
            return "GUARD_REJECTED:QTY_LIMIT", ""
        # H2(GPT 10/27): 진입 마감은 폴링 시작 시각(ctx.now)이 아니라 **전송 직전 실제 시각**으로 검사
        fresh = self.clock() if intent.side == "BUY" else ctx.now
        if (intent.side == "BUY" and intent.order_type == "limit" and self.cfg.entry_cancel_at is not None
                and max(ctx.now, fresh).time() >= self.cfg.entry_cancel_at):
            self.summary.intents_denied += 1
            self.summary.denied_codes["ENTRY_WINDOW_CLOSED"] = self.summary.denied_codes.get("ENTRY_WINDOW_CLOSED", 0) + 1
            self.log.info(f"[GUARD] {intent.symbol} 지정가 BUY 차단 | ENTRY_WINDOW_CLOSED | "
                          f"진입 마감 {self.cfg.entry_cancel_at:%H:%M}")
            self._shadow(intent, ctx, allowed=False, code="ENTRY_WINDOW_CLOSED")
            return "GUARD_REJECTED:ENTRY_WINDOW_CLOSED", ""
        age = (fresh - ctx.now).total_seconds()
        if intent.side == "BUY" and age > self.MAX_BUY_DECISION_AGE_SEC:
            # H2: 폴링 시작 뒤 너무 오래 지난 매수 판단(가격 재확인 시점이 낡음) — 보내지 않음. 신호 키는 이미 기록됐으므로
            # 그 신호는 다시 쓰지 않음(뒤늦은 추격 매수 없음, 보수적).
            self.summary.intents_denied += 1
            self.summary.denied_codes["STALE_DECISION"] = self.summary.denied_codes.get("STALE_DECISION", 0) + 1
            self.log.warning(f"[GUARD] {intent.symbol} BUY 차단 | STALE_DECISION | 판단 뒤 {age:.0f}초 "
                             f"(한도 {self.MAX_BUY_DECISION_AGE_SEC}초)")
            self._shadow(intent, ctx, allowed=False, code="STALE_DECISION")
            return "GUARD_REJECTED:STALE_DECISION", f"{age:.0f}s"
        prices: dict[str, int] = {}
        reserve, unknown_pending = 0, False
        if intent.side == "BUY":
            prices = self._held_prices(ctx)
            reserve, unknown_pending = self._pending_buy_reserve()
        decision = check_intent(intent, config=self.guard, now=ctx.now, phase=ctx.phase, balance=ctx.balance,
                                positions=ctx.positions, prices=prices, blocked_symbols=ctx.blocked_symbols,
                                unreconciled_symbols=self._unreconciled, pending_buy_reserve=reserve,
                                unknown_pending_orders=unknown_pending,
                                recovery_symbols=self.executor.recovery_blocked_symbols())
        if not decision.allowed:
            self.summary.intents_denied += 1
            self.summary.denied_codes[decision.code] = self.summary.denied_codes.get(decision.code, 0) + 1
            self.log.info(f"[GUARD] {intent.symbol} {intent.side} {intent.quantity}주 차단 | {decision.code} | {decision.detail}")
            self._shadow(intent, ctx, allowed=False, code=decision.code, detail=decision.detail)
            return f"GUARD_REJECTED:{decision.code}", decision.detail or ""
        if not self.send_orders:
            self._shadow(intent, ctx, allowed=True)
            return "SHADOW", ""
        held = next((p for p in ctx.balance.positions if p.symbol == intent.symbol), None)
        base_qty = held.quantity if held else 0
        base_cost = held.quantity * held.average_price if held else 0
        context = {"entry_strategy": getattr(self.strategy, "strategy_id", ""), **intent.context}
        new_meta = None
        if intent.side == "BUY":
            context.setdefault("entry_reason", intent.reason)
            # C2: 진입 정보(신호·계약·손절가)를 전송 **전에** 메타로 — 실행기의 주문 의도 기록과 같은 저장에 실림.
            #     전송 직후 꺼져도 재시작 때 손절가가 남아 있음. 기존 보유 메타는 덮지 않음(최대 보유 1이라 정상은 없음).
            if intent.symbol not in self.state.positions:
                new_meta = self._entry_meta(intent)
                if new_meta is not None:
                    self.state.upsert_position_meta(new_meta)
            if intent.order_type == "limit":
                sub = self.executor.submit_buy(intent.symbol, intent.quantity, intent.reference_price,
                                               context=context, order_type="limit", limit_price=intent.limit_price)
            else:
                sub = self.executor.submit_buy(intent.symbol, intent.quantity, intent.reference_price, context=context)
        else:
            sub = self.executor.submit_sell(intent.symbol, intent.quantity, intent.reference_price,
                                            exit_reason=intent.reason,
                                            avg_buy_price=held.average_price if held else 0,
                                            forced=intent.forced, context=context)
        if sub.sent:
            self.summary.orders_sent += 1
        if not sub.accepted:
            why = sub.block_code or (sub.result.message if sub.result else '')
            self.log.warning(f"[SESSION] {intent.symbol} {intent.side} 미접수 | {why}")
            if sub.block_code == "ORDER_PLACEMENT_AMBIGUOUS":
                if new_meta is not None:              # 접수됐을 수 있음 — 메타 유지(체결되면 손절가 필요)
                    new_meta.meta["entry_status"] = "UNKNOWN"
                    self.executor.save_state()
                return "UNKNOWN", why
            if new_meta is not None and not self._holds(intent.symbol, ctx):
                self.state.remove_position_meta(intent.symbol)
                self.executor.save_state()
            return ("NOT_ACCEPTED:" + sub.block_code) if sub.block_code else "NOT_ACCEPTED", why
        self.summary.orders_accepted += 1
        if new_meta is not None:
            new_meta.meta["entry_status"] = "ACCEPTED"
            new_meta.meta["order_id"] = str(sub.result.order_id or "")
        # 지정가 매수의 추정가는 지정가(체결가는 그 이하) — 미체결 예약금·추정 원가 모두 보수적
        est_price = intent.limit_price if intent.order_type == "limit" else intent.reference_price
        self.recorder.track(intent.symbol, intent.side, sub.result.order_id, intent.quantity,
                            est_price, base_quantity=base_qty, base_cost=base_cost,
                            order_date=ctx.trade_date)
        if intent.order_type == "limit" and str(sub.result.order_id or "").strip():
            self._limit_buy_orders[str(sub.result.order_id).strip().lstrip("0")] = intent.symbol
        if intent.side == "BUY" and intent.symbol not in self.state.positions:
            self.state.upsert_position_meta(PositionMeta(
                intent.symbol, strategy_id=getattr(self.strategy, "strategy_id", ""), origin="ORDER"))
        return "SUBMITTED", str(sub.result.order_id or "")

    @staticmethod
    def _holds(symbol: str, ctx: TickContext) -> bool:
        p = ctx.positions.get(symbol)
        return bool((p and p.quantity > 0) or any(b.symbol == symbol and b.quantity > 0 for b in ctx.balance.positions))

    def _sync_entry_dates(self, positions: dict) -> None:
        """C2: 메타의 진입 거래일 = 원장 첫 체결일(재시작 후에도 원장에서 다시 맞춤). 바뀔 때만 갱신."""
        for sym, pos in positions.items():
            m = self.state.positions.get(sym)
            if m is None or pos.quantity <= 0:
                continue
            d = pos.first_entry_date.isoformat()
            if m.meta.get("entry_trade_date") != d or m.meta.get("entry_status", "FILLED") != "FILLED":
                m.meta["entry_trade_date"] = d
                if "entry_status" in m.meta:
                    m.meta["entry_status"] = "FILLED"
                self.state.upsert_position_meta(m)

    def _cancel_expired_entries(self, now: datetime) -> None:
        """진입 마감(entry_cancel_at) 이후, 이 실행이 낸 지정가 매수 중 끝나지 않은 주문의 남은 수량 취소.

        신규 매수 중단·주문 중단(_halt) 중에도 취소는 함 — 노출을 줄이는 쪽. 확정은 다음 폴링의 체결조회."""
        if self.cfg.entry_cancel_at is None or now.time() < self.cfg.entry_cancel_at or not self._limit_buy_orders:
            return
        for sym, order_id in self.executor.open_buy_orders().items():
            if self._limit_buy_orders.get(str(order_id).strip().lstrip("0")) != sym:
                continue      # 이 실행이 낸 지정가 매수가 아님(시장가·재시작 전 주문) — 건드리지 않음
            code = self.executor.cancel_open_buy(
                sym, reason=f"지정가 진입 마감 {self.cfg.entry_cancel_at:%H:%M} — 미체결 잔량 취소")
            if code in ("CANCEL_SENT", "CANCEL_REJECTED", "CANCEL_AMBIGUOUS"):
                self.summary.cancels_requested += 1
            if self._cancel_results.get(sym) != code:
                self._cancel_results[sym] = code
                self.log.info(f"[SESSION] {sym} 지정가 매수 {order_id} 취소 요청 결과 {code}")

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

    # ── 상태 파일·신규 매수 중단 (M3) ───────────────────────

    def _refresh_buy_halt(self, now: datetime) -> None:
        if not self.commands_dir:
            return
        from infra.storage.runner_status import read_buy_halt
        st, req = read_buy_halt(self.commands_dir)
        if st == "NONE":
            if self.buy_halt:
                self.log.warning(f"[BUY_HALT] 요청 파일 없음 — 사람이 재개함(요청 {self.buy_halt.get('request_id')})")
                self.buy_halt = None
            return
        if not self.buy_halt or self.buy_halt.get("request_id") != req.get("request_id"):
            self.buy_halt = {**req, "applied_at": now.isoformat(timespec="seconds"),
                             "applied_run_id": self.run_id}
            self.log.critical(f"[BUY_HALT] 신규 매수 중단 반영 — 요청 {req.get('request_id')} "
                              f"({req.get('requested_at', '?')}, {req.get('reason', req.get('error', ''))}) · 매도·손절은 계속")

    def _set_stage(self, stage: str) -> None:
        self.stage = stage
        self._beat(force=True)

    def _beat(self, force: bool = False) -> None:
        if self.status_file is None:
            return
        now = self.clock()
        if not force and self._last_status_at is not None and (now - self._last_status_at).total_seconds() < 30:
            return
        self._last_status_at = now
        try:
            from infra.storage.runner_status import write_status
            write_status(self.status_file, self._status_dict(now))
        except Exception as exc:          # 상태 파일은 정보용 — 매매를 멈추지 않음
            self.log.warning(f"[RUNNER_STATUS] 상태 파일 쓰기 실패: {type(exc).__name__}: {exc}")

    def _status_dict(self, now: datetime) -> dict:
        iso = lambda v: v.isoformat(timespec="seconds") if v else None      # noqa: E731
        bal = self.last_balance
        psm = self.executor.position_state_machine
        s = self.summary
        return {
            "schema": "rs1", "run_id": self.run_id, "pid": self.pid, "started_at": iso(self.started_at),
            "heartbeat_at": iso(now), "stage": self.stage, "trade_date": s.trade_date.isoformat() if s.trade_date else None,
            **self.meta,
            "strategy_id": getattr(self.strategy, "strategy_id", ""),
            "strategy_connected": getattr(self.strategy, "strategy_id", "null") not in ("null", ""),
            "strategy_mode": "shadow" if not self.send_orders else self.meta.get("strategy_mode", "off"),
            "strategy_book": (self.strategy.book.to_dict() if getattr(self.strategy, "book", None) is not None
                              and hasattr(self.strategy.book, "to_dict") else None),
            "shadow_intents": self.summary.shadow_intents,
            "buy_halt": self.buy_halt, "halted_reason": self._halted_reason,
            "last_balance_at": iso(self._last_balance_ok_at), "last_balance_error": self.last_balance_error,
            "last_reconcile_at": iso(self._last_reconcile_at),
            "reconcile_ok": None if self._last_report is None else self._last_report.ok,
            "cash": bal.cash if bal else None,
            "positions": [{"symbol": p.symbol, "quantity": p.quantity, "average_price": p.average_price}
                          for p in (bal.positions if bal else [])],
            "tracked_orders": [{"symbol": o.symbol, "side": o.side, "order_id": o.order_id,
                                "requested": o.requested_quantity, "filled": o.filled_quantity,
                                "reference_price": o.reference_price,
                                "order_type": "limit" if str(o.order_id).strip().lstrip("0") in self._limit_buy_orders
                                else "market"}
                               for o in self.recorder.tracked.values()],
            "entry_cancel_at": self.cfg.entry_cancel_at.strftime("%H:%M") if self.cfg.entry_cancel_at else None,
            "daily_risk": (dict(self.risk_guard.last, block=self._risk_block) if self.risk_guard is not None else None),
            "fill_evidence": self.recorder.evidence_status() if hasattr(self.recorder, "evidence_status") else None,
            "unresolved_symbols": sorted(self.executor.unresolved_symbols()),
            "buy_cancels": [r.to_dict() for r in self.executor.buy_cancels.values()]
            if hasattr(self.executor, "buy_cancels") else [],
            "recovery_symbols": sorted(self.executor.recovery_blocked_symbols()),
            "lifecycle": {sym: st.lifecycle.value for sym, st in psm._states.items()},
            "counters": {"ticks": s.ticks, "balance_failures": s.balance_failures, "intents": s.intents,
                         "intents_denied": s.intents_denied, "orders_sent": s.orders_sent,
                         "orders_accepted": s.orders_accepted, "fills_recorded": s.fills_recorded,
                         "cancels_requested": s.cancels_requested},
            "denied_codes": dict(s.denied_codes),
        }

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
        close_started = self.clock()
        deadline = datetime.combine(close_started.date(), self.cfg.close_reconcile_until)
        ctx = None
        while not self.should_stop():
            ctx = self._tick(allow_orders=False) or ctx
            unresolved = self.executor.has_unresolved_orders() or bool(self.recorder.tracked)
            if not unresolved or self.clock() >= deadline:
                break
            if not self._sleep_interval():
                break
        # 8-D (F5): 마감 이후 잔고·대조가 한 번도 성공하지 못했으면 제한 횟수 재시도
        attempt = 0
        while (not self._fresh(self._last_reconcile_at, close_started)
               and attempt < self.cfg.close_balance_retries and not self.should_stop()):
            attempt += 1
            self.log.warning(f"[SESSION_CLOSE] 마감 후 잔고·대조 미확보 — 재시도 {attempt}/{self.cfg.close_balance_retries}")
            if not self._sleep_seconds(self.cfg.close_balance_retry_sec):
                break
            ctx = self._tick(allow_orders=False) or ctx
        self.summary.unresolved_at_end = self.executor.has_unresolved_orders() or bool(self.recorder.tracked)
        for sym, order in self.recorder.tracked.items():
            self.log.critical(f"[SESSION] {sym} 주문 {order.order_id} 마감 후에도 추적 중 — 체결 "
                              f"{order.filled_quantity}/{order.requested_quantity}주, 원장·HTS 확인 필요")
        fresh_reconcile = self._fresh(self._last_reconcile_at, close_started)
        self.summary.final_reconcile = self._last_report if fresh_reconcile else None
        self.summary.final_reconcile_at = self._last_reconcile_at if fresh_reconcile else None
        self.summary.final_balance_at = (self._last_balance_ok_at
                                         if self._fresh(self._last_balance_ok_at, close_started) else None)
        if ctx is not None:
            self._safe_strategy_call("on_session_end", ctx)
        self.executor.save_state()
        if self.after_close is not None:
            try:
                result = self.after_close(self.clock().date())
                self.summary.daily_bars = f"FAILED: {result}" if isinstance(result, str) and result else "OK"
            except Exception as exc:
                self.summary.daily_bars = f"FAILED: {type(exc).__name__}: {exc}"
                self.log.error(f"[SESSION] 마감 후 작업 실패(매매 결과에는 영향 없음): {type(exc).__name__}: {exc}")
        self._judge_close()

    @staticmethod
    def _fresh(at: datetime | None, since: datetime) -> bool:
        return at is not None and at >= since

    def _judge_close(self) -> None:
        s = self.summary
        s.close_check = "VERIFIED"
        if s.final_balance_at is None:
            s.add_close_issue("FINAL_BALANCE_FAILED")
        if s.final_reconcile is None:
            s.add_close_issue("FINAL_RECONCILE_MISSING")
        elif not s.final_reconcile.ok:
            s.add_close_issue("RECONCILE_MISMATCH")
        if s.unresolved_at_end:
            s.add_close_issue("UNRESOLVED_ORDERS")
        if s.halted_reason:
            s.add_close_issue("HALTED")
        if s.daily_bars.startswith("FAILED"):
            s.add_close_issue("DAILY_BARS_FAILED")
        if s.close_check == "VERIFIED":
            self.log.info("[SESSION_CLOSE] 마감 검증 VERIFIED — 마감 후 잔고·장부 대조 일치, 미해결 주문 없음")
        else:
            self.log.critical(f"[SESSION_CLOSE] 마감 검증 NEEDS_REVIEW — {', '.join(s.close_issues)}")

    @property
    def last_balance_at(self) -> datetime | None:
        return self._last_balance_ok_at

    @property
    def last_reconcile(self) -> ReconcileReport | None:
        return self._last_report

    def _finish(self, status: str) -> SessionSummary:
        self.summary.status = status
        self._set_stage(f"FINISHED:{status}")
        if status == "STOPPED" and not self.summary.close_check:
            self.summary.close_check = "NOT_RUN"
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

    def _sleep_seconds(self, seconds: float) -> bool:
        end = self.clock() + timedelta(seconds=seconds)
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
            self._beat()
        return True
