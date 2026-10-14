from __future__ import annotations

"""스윙 자동매매 진입점 (2026-09-28, 6라운드: 하루 수명주기 연결).

운영 방식(민우님 확정): 장 시작 전에 켜고 장 마감 후 끈다.

    python -m app.main                 # 기동 점검 → 하루 수명주기(app/session_runner.py) → 마감 후 종료
    python -m app.main --check-only    # 기동 점검만 하고 종료 (이전 라운드 동작)

전략 자리는 비어 있습니다(NullStrategy — 주문을 내지 않음).

기동 점검 순서 (단타 레포 `app/main.py`(bdde6c2)에서 매매 로직과 무관한 부분):

1. .env / settings.yaml 로드
2. 단일 인스턴스 락 (상태 파일 기준 — 단타 레포와 경로가 달라 서로 막지 않음)
3. 실행 기준선 기록 (run_id / git sha / 설정 해시)
4. 브로커 인증 (429 재시도)
5. 잔고 조회 (실전투자에서 실패하면 시작 중단 — 단타 레포와 동일 원칙)
6. 이전 프로세스가 남긴 미해결 주문 흔적 확인
   (state.json의 unresolved_order_intents + tracked_order_journal)
   — 읽기만 하고 아무것도 자동 해소하지 않습니다.
6-1. 체결 원장 ↔ 잔고 ↔ 포지션 메타 대조 보고 (4라운드, 읽기 전용)
7. 카카오 시작 알림 (백그라운드, 실패해도 무시)
8. (--check-only가 아니면) OrderExecutor·체결 기록기·안전 한도를 묶어 하루 수명주기 실행
"""

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import Settings, load_settings
from domain.models import AccountBalance
from infra.broker.execution_mode import (
    KIWOOM_PAPER, PaperKiwoomBroker, build_execution_broker, mode_label, paper_identity_problems,
    resolve_execution_mode,
)
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.market_data.quote_source import KiwoomQuoteSource
from infra.storage.logger import build_app_logger
from infra.storage.execution_guard import DataModeMismatchError, commit_data_mode, decide_data_mode, order_path_locks
from infra.storage.run_baseline import perform_run_baseline_startup
from domain.position.position_book import ReconcileReport, reconcile
from domain.service.lot_ledger import LotMatchError, apply_events
from infra.storage.fill_evidence import FillEvidenceStore, evidence_path
from infra.storage.fill_ledger import FillLedgerCorruptError, FillLedgerStore
from infra.storage.swing_state_store import SwingStateStore
from infra.storage.tracked_order_journal import (
    TrackedOrderJournalCorruptError,
    TrackedOrderJournalStore,
)
from utils.time_utils import now_local

STARTUP_TITLE = "🟦 스윙 자동매매 시작"


def load_dotenv(path: str = ".env") -> None:
    """단타 레포 app/main.py와 동일 (외부 의존성 없이 .env를 읽음)."""
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_broker(settings: Settings):
    """실행 모드별 브로커 (M0 커밋 1): MOCK_LOCAL → MockBroker, KIWOOM_PAPER → PaperKiwoomBroker,
    실전·알 수 없는 주소 → ExecutionModeError(브로커를 만들지 않음). 단타 원본은 KiwoomBroker를 그대로 만들었음."""
    return build_execution_broker(settings)


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc)
    return "http=429" in msg or "허용된 요청 개수를 초과" in msg


async def retry_on_429(fn, desc: str, app_logger, *, max_retries: int = 10, sleep=asyncio.sleep):
    """시작 시 429 재시도 래퍼 — 단타 레포 app/main.py `_retry_on_429`와 동일한 대기 규칙.

    테스트에서 실제로 기다리지 않도록 sleep을 주입할 수 있게만 바꿨습니다.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_rate_limit_error(exc):
                raise
            wait = min(30 * attempt, 180)  # 30초 → 60초 → ... → 최대 180초
            app_logger.warning(
                f"[STARTUP] {desc} 429 에러 ({attempt}/{max_retries}회) — {wait}초 후 재시도"
            )
            await sleep(wait)
    raise RuntimeError(f"{desc} 최대 재시도 초과")


class StartupBlockedError(RuntimeError):
    """실전투자에서 안전하게 시작할 수 없는 상태."""


@dataclass
class StartupReport:
    mode: str
    execution_mode: str = ""
    balance: AccountBalance | None = None
    balance_error: str = ""
    unresolved_intent_symbols: list[str] = field(default_factory=list)
    journal_symbols: list[str] = field(default_factory=list)
    journal_error: str = ""
    ledger_error: str = ""
    reconcile: ReconcileReport | None = None
    data_mode_action: str = ""          # M0 커밋 2: CREATE / SAME / SWITCH (execution_mode.json)

    @property
    def has_unresolved_orders(self) -> bool:
        return bool(self.unresolved_intent_symbols or self.journal_symbols or self.journal_error)


async def run_startup_checks(settings: Settings, broker, app_logger, *, sleep=asyncio.sleep) -> StartupReport:
    """인증 → 잔고 → 미해결 주문 흔적 확인. 주문은 절대 내지 않습니다."""
    paper_strategy = getattr(settings, "swing_strategy", None) is not None and settings.swing_strategy.mode == "paper"
    if paper_strategy:
        problems = paper_config_problems(settings, broker)
        if problems:
            raise StartupBlockedError("swing_strategy.mode=paper 기동 거부 — " + "; ".join(problems))
    is_live = not settings.broker.is_paper_trading
    # M0 커밋 1: 키움 모의투자 서버 모드도 실전과 같은 엄격 기준(잔고·주문 기록·원장을 모르면 시작 안 함).
    # 로컬 가짜 브로커(MOCK_LOCAL)만 예전처럼 경고 후 계속. 실전 조합은 build_broker에서 이미 거부됨.
    exec_mode = resolve_execution_mode(settings.broker) if not is_live else ""
    strict = is_live or exec_mode == KIWOOM_PAPER
    if exec_mode == KIWOOM_PAPER and not isinstance(broker, PaperKiwoomBroker):
        raise StartupBlockedError("KIWOOM_PAPER 설정인데 브로커가 모의투자 전용(PaperKiwoomBroker)이 아님")
    report = StartupReport(mode="실전투자" if is_live else f"모의투자 · {mode_label(exec_mode)}",
                           execution_mode=exec_mode)

    await retry_on_429(broker.authenticate, "인증", app_logger, sleep=sleep)

    # ── 잔고 조회 ────────────────────────────────────────────
    # 단타 레포와 동일 원칙: 실전투자는 계좌 상태를 모르는 채로 시작하지 않음.
    try:
        report.balance = await retry_on_429(
            broker.get_account_balance, "잔고조회", app_logger, sleep=sleep
        )
        app_logger.info(
            f"[STARTUP] 잔고 확인 | 현금 {report.balance.cash:,}원 | "
            f"보유 {len(report.balance.positions)}종목"
        )
    except Exception as exc:
        report.balance_error = f"{type(exc).__name__}: {exc}"
        app_logger.warning(f"[STARTUP] 잔고 조회 실패: {report.balance_error}")
        if strict:
            app_logger.critical(f"[STARTUP_BLOCK] {report.mode} — 잔고 조회 실패로 시작하지 않습니다.")
            raise StartupBlockedError(f"{report.mode} 시작 시 잔고 조회 실패") from exc

    # ── 이전 프로세스의 미해결 주문 흔적 (읽기 전용) ───────────
    # OrderExecutor.restore_order_recovery_blocks()가 보는 두 출처와 동일.
    # 여기서는 "있다/없다"만 보고합니다.
    state = None
    try:
        state, _ = SwingStateStore(settings.storage.state_file).load()
        report.unresolved_intent_symbols = sorted(state.unresolved_order_intents)
    except Exception as exc:
        # state.json 손상도 "미해결 주문 여부를 알 수 없음"으로 취급
        report.journal_error = f"state.json 읽기 실패 — {type(exc).__name__}: {exc}"
    try:
        records = TrackedOrderJournalStore(settings.storage.tracked_order_journal_file).load_all()
        report.journal_symbols = sorted(records)
    except TrackedOrderJournalCorruptError as exc:
        report.journal_error = (report.journal_error + " / " if report.journal_error else "") + str(exc)

    if report.journal_error:
        app_logger.critical(f"[STARTUP_ORDER_RECOVERY] 주문 기록 확인 실패: {report.journal_error}")
        if strict:
            raise StartupBlockedError(f"{report.mode} — 주문 기록을 확인할 수 없어 시작하지 않습니다.")
    elif report.has_unresolved_orders:
        app_logger.critical(
            f"[STARTUP_ORDER_RECOVERY] 이전 프로세스의 미해결 주문 흔적 | "
            f"intents={report.unresolved_intent_symbols} journal={report.journal_symbols} "
            f"— HTS에서 주문·잔고를 직접 확인하세요(자동 해소하지 않음)"
        )
    else:
        app_logger.info("[STARTUP_ORDER_RECOVERY] 미해결 주문 흔적 없음")

    # ── 체결 원장 ↔ 잔고 ↔ 포지션 메타 대조 (4라운드, 읽기 전용) ──
    ledger_store = FillLedgerStore(settings.storage.fill_ledger_file)
    try:
        ledger = apply_events(ledger_store.load())
    except (FillLedgerCorruptError, LotMatchError) as exc:
        report.ledger_error = f"{type(exc).__name__}: {exc}"
        app_logger.critical(f"[STARTUP_LEDGER] 체결 원장을 신뢰할 수 없음: {report.ledger_error}")
        if strict:
            raise StartupBlockedError(f"{report.mode} — 체결 원장 손상으로 시작하지 않습니다.") from exc
        return report
    # M0 커밋 2: 데이터 폴더의 실행 모드 표식 — 다른 모드·계좌 라벨의 원장·미해결 주문과 섞지 않음
    if exec_mode:
        try:
            report.data_mode_action = decide_data_mode(
                settings, exec_mode, ledger_empty=not ledger_store.load(), unresolved=report.has_unresolved_orders)
        except DataModeMismatchError as exc:
            app_logger.critical(f"[STARTUP_BLOCK] 데이터 폴더 실행 모드 불일치: {exc}")
            raise StartupBlockedError(str(exc)) from exc
        if report.data_mode_action == "SWITCH":
            app_logger.warning(f"[STARTUP] 데이터 폴더 실행 모드 전환 → {exec_mode} (원장 비어 있음·미해결 주문 없음)")
    for torn in ledger_store.torn_tail_paths:
        app_logger.critical(f"[STARTUP_LEDGER] 끊긴 마지막 줄 격리: {torn} — 잔고와 대조 필요")
    if report.balance is not None and state is not None:
        report.reconcile = reconcile(
            ledger, report.balance, state.positions,
            in_flight_symbols=set(report.unresolved_intent_symbols) | set(report.journal_symbols),
        )
        for line in report.reconcile.lines():
            (app_logger.critical if line.startswith("[BLOCK]") else app_logger.info)(
                f"[STARTUP_RECONCILE] {line}")
        if report.reconcile.issues == []:
            app_logger.info("[STARTUP_RECONCILE] 원장·잔고·메타 일치")
    if paper_strategy:
        problems = paper_account_problems(report, state)
        if problems:
            from infra.storage.runner_status import request_buy_halt
            _, req = request_buy_halt(settings.storage.commands_dir, reason="[paper 사전 점검] " + "; ".join(problems),
                                      by="paper_precheck", now=now_local())
            app_logger.critical(f"[STARTUP_PAPER] 사전 점검 문제 — 신규 매수 중단({req.get('request_id')}), 매도·손절은 계속: "
                                + "; ".join(problems))
        else:
            app_logger.info(f"[STARTUP_PAPER] 사전 점검 통과 — 허용 종목 {list(settings.swing_strategy.allowed_symbols)}, "
                            f"1주·최대 보유 1, 일일 손실 한도 {settings.swing_strategy.daily_loss_limit:,}원")
    return report


def paper_config_problems(settings: Settings, broker) -> list[str]:
    """S1 모의 전송(paper) 설정 점검(C3) — 하나라도 있으면 기동 거부. 실전은 build_broker·실행 모드에서 이미 거부."""
    out: list[str] = []
    st = settings.swing_strategy
    try:
        mode = resolve_execution_mode(settings.broker)
    except Exception as exc:                              # noqa: BLE001
        return [f"실행 모드 오류: {exc}"]
    if mode != KIWOOM_PAPER:
        out.append(f"실행 모드 {mode} — 키움 모의 서버(KIWOOM_PAPER: mockapi·use_mock false)만")
    elif not isinstance(broker, PaperKiwoomBroker):
        out.append("브로커가 모의투자 전용(PaperKiwoomBroker)이 아님")
    out += [f"계좌 설정: {p}" for p in paper_identity_problems(settings.broker)]
    if not st.allowed_symbols:
        out.append("swing_strategy.allowed_symbols 비어 있음 — 허용 종목을 1개 이상 지정")
    if st.quantity != 1:
        out.append("수량 1주만")
    if settings.session.entry_cancel_at in (None, ""):
        out.append("session.entry_cancel_at(진입 마감·미체결 취소) 없음")
    return out


def paper_account_problems(report, state) -> list[str]:
    """계좌 사전 점검(C3) — 문제가 있으면 기동은 하되 신규 매수 중단(보유 청산은 계속)."""
    out: list[str] = []
    if report.has_unresolved_orders:
        out.append(f"미해결 주문 흔적 intents={report.unresolved_intent_symbols} journal={report.journal_symbols}")
    if report.reconcile is not None and any(str(x).startswith("[BLOCK]") for x in report.reconcile.lines()):
        out.append("원장·잔고·메타 불일치")
    if report.balance is not None:
        from app.s1_strategy import STRATEGY_ID
        metas = state.positions if state is not None else {}
        foreign = [p.symbol for p in report.balance.positions if p.quantity > 0
                   and not (p.symbol in metas and metas[p.symbol].strategy_id == STRATEGY_ID
                            and metas[p.symbol].origin == "ORDER" and metas[p.symbol].stop_price)]
        if foreign:
            out.append(f"S1 진입 메타 없는 보유 {foreign} — 사람 확인(자동 청산 안 함)")
    return out


async def async_main(check_only: bool = False):
    load_dotenv()
    settings = load_settings()
    # M0 커밋 2: 상태 파일 잠금 + 계좌 라벨 잠금(M1 수동 주문 도구·원장 정정 도구와 동시 실행 차단)
    with order_path_locks(settings):
        return await _run_application(settings, check_only=check_only)


async def _run_application(settings: Settings, check_only: bool = False):
    # 단타 레포와 동일: 구버전 .pyc 캐시로 인한 AttributeError 방지
    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    print("=" * 50)
    print("  스윙 자동매매 — " + ("기반 점검 모드" if check_only else "하루 수명주기 (전략: NullStrategy)"))
    print(f"  app.log: {settings.storage.app_log_file}")
    print("=" * 50)

    perform_run_baseline_startup(settings, app_logger)

    broker = build_broker(settings)
    report = await run_startup_checks(settings, broker, app_logger)

    from infra.notify.kakao_notifier import build_notifier, send_startup_notification_async
    notifier = build_notifier(settings)
    send_startup_notification_async(
        notifier, app_logger, report.mode, now_local().strftime("%H:%M"), None,
        title=STARTUP_TITLE,
        watch_line="기반 점검 모드" if check_only else "하루 수명주기 시작 (전략 없음)",
    )

    # 매매 루프가 없어 곧바로 종료되므로, 백그라운드 알림 스레드(daemon)가
    # 전송 전에 프로세스와 함께 끊기지 않도록 최대 20초만 기다립니다.
    import threading
    for _t in threading.enumerate():
        if _t.name == "kakao-startup-notify":
            _t.join(timeout=20)

    print(f"  모드: {report.mode}")
    if report.balance is not None:
        print(f"  현금: {report.balance.cash:,}원 | 보유: {len(report.balance.positions)}종목")
    else:
        print(f"  잔고 조회 실패: {report.balance_error}")
    print(f"  미해결 주문 흔적: {'있음 — app.log 확인' if report.has_unresolved_orders else '없음'}")
    if report.ledger_error:
        print(f"  체결 원장: 오류 — {report.ledger_error}")
    elif report.reconcile is not None:
        print(f"  장부 대조: {'일치' if report.reconcile.ok else '불일치 — app.log [STARTUP_RECONCILE] 확인'}"
              f" (어긋남 {len(report.reconcile.issues)}건)")
    if check_only:
        app_logger.info("[STARTUP] 기반 점검 완료 — --check-only로 종료합니다.")
        return
    if report.data_mode_action:
        commit_data_mode(settings, report.execution_mode, report.data_mode_action)
    summary = run_session(settings, broker, app_logger)
    print("-" * 50)
    for line in summary.lines():
        print(f"  {line}")
    return summary


def build_guard_config(g) -> "GuardConfig":
    from datetime import time as _time
    from domain.risk.account_guard import GuardConfig

    def hm(v: str) -> _time:
        hh, mm = str(v).split(":")
        return _time(int(hh), int(mm))

    return GuardConfig(
        max_positions=int(g.max_positions), max_order_amount=int(g.max_order_amount),
        max_total_exposure=int(g.max_total_exposure), min_cash_buffer=int(g.min_cash_buffer),
        new_orders_start=hm(g.new_orders_start), new_orders_end=hm(g.new_orders_end),
        allowed_symbols=tuple(str(x) for x in g.allowed_symbols),
        buy_price_buffer_pct=float(g.buy_price_buffer_pct),
    )


def build_session_config(s) -> "SessionConfig":
    from datetime import time as _time
    from app.session_runner import SessionConfig

    hh, mm = str(s.close_reconcile_until).split(":")
    cancel_at = str(getattr(s, "entry_cancel_at", "") or "").strip()
    entry_cancel_at = None
    if cancel_at:
        ch, cm = cancel_at.split(":")
        entry_cancel_at = _time(int(ch), int(cm))
    return SessionConfig(
        poll_interval_sec=float(s.poll_interval_sec), close_reconcile_until=_time(int(hh), int(mm)),
        watch_symbols=tuple(str(x) for x in s.watch_symbols), entry_cancel_at=entry_cancel_at,
    )


def code_version_cached() -> str:
    """시작 당시 체크아웃 정보(관리자와 같은 방식) — 한 번만 계산."""
    global _CODE_VERSION
    if _CODE_VERSION is None:
        try:
            from utils.code_version import code_version
            _CODE_VERSION = code_version()
        except Exception as exc:          # noqa: BLE001
            _CODE_VERSION = f"확인 불가({type(exc).__name__})"
    return _CODE_VERSION


_CODE_VERSION: str | None = None


def build_session_runner(settings: Settings, broker, app_logger, *, strategy=None, clock=None, sleep=None,
                         should_stop=None, quote_source=None, status_file=None, process: str = "app.main",
                         use_settings_strategy: bool = False, candidates=None):
    """실행기 조립 — 하루 수명주기와 M1 수동 주문 도구가 같은 OrderExecutor·FillRecorder·안전 한도를 씀 (M0 커밋 3).

    (runner, executor). 상태 파일 손상이면 예외(주문 경로를 만들지 않음)."""
    import time as _time_mod

    from app.session_runner import SessionRunner
    from domain.service.fill_recorder import FillRecorder
    from domain.service.order_executor import OrderExecutor
    from domain.strategy.interface import NullStrategy
    from infra.storage.logger import PositionLifecycleLogger, TradeCsvLogger
    from utils.trading_calendar import TradingCalendar

    calendar = TradingCalendar.load()
    state_store = SwingStateStore(settings.storage.state_file)
    state, hp = state_store.load()      # 손상 시 예외 → 주문 경로를 만들지 않음
    ledger_store = FillLedgerStore(settings.storage.fill_ledger_file)
    executor = OrderExecutor(
        settings=settings, broker=broker, state=state, highest_price=hp, state_store=state_store,
        app_logger=app_logger, trade_logger=TradeCsvLogger(settings.storage.trade_log_file),
        position_lifecycle_logger=PositionLifecycleLogger(settings.storage.position_lifecycle_log_file),
        clock=clock,   # 테스트의 가짜 시계 (운영은 None → datetime.now)
        commands_dir=settings.storage.commands_dir,   # 8-G: 설정 경로 (테스트는 임시 폴더)
    )
    after_close = None
    if settings.session.update_daily_bars_after_close and isinstance(broker, KiwoomBroker):
        after_close = _make_daily_bar_updater(settings, broker, calendar, state, ledger_store, app_logger)
    if quote_source is None and isinstance(broker, KiwoomBroker):
        quote_source = KiwoomQuoteSource(broker, logger=app_logger)
    session_cfg = build_session_config(settings.session)
    mode = settings.swing_strategy.mode if use_settings_strategy else "off"
    send_orders, shadow_sink, risk_guard = True, None, None
    if strategy is None and mode != "off":
        if mode == "paper":
            problems = paper_config_problems(settings, broker)
            if problems:
                raise StartupBlockedError("swing_strategy.mode=paper 기동 거부 — " + "; ".join(problems))
        strategy = build_s1_strategy(settings, broker, calendar, quote_source, session_cfg, candidates=candidates)
        if mode == "shadow":
            send_orders, shadow_sink = False, _jsonl_sink(Path(settings.storage.state_file).with_name("shadow_intents.jsonl"))
        import dataclasses as _dc
        session_cfg = _dc.replace(session_cfg, max_buy_quantity=settings.swing_strategy.quantity)
        risk_guard = build_daily_risk_guard(settings, ledger_store, app_logger, enforce=(mode == "paper"))
    runner = SessionRunner(
        broker=broker, executor=executor, state=state, ledger_store=ledger_store,
        recorder=FillRecorder(ledger_store, logger=app_logger,
                              scope=settings.broker.account_scope_id.strip() or "unscoped",
                              # M3 A2: 주문별 체결조회(키움만 — MockBroker는 없음 → 기존 기록 방식)
                              fill_lookup=getattr(broker, "get_fill_detail", None),
                              evidence_store=FillEvidenceStore(evidence_path(settings.storage.fill_ledger_file))),
        calendar=calendar,
        strategy=strategy or NullStrategy(), guard_config=build_guard_config(settings.guard),
        session_config=session_cfg, logger=app_logger,
        clock=clock or now_local, sleep=sleep or _time_mod.sleep, should_stop=should_stop or (lambda: False),
        after_close=after_close,
        quote_source=quote_source,
        status_file=status_file, commands_dir=settings.storage.commands_dir,
        meta={"execution_mode": resolve_execution_mode(settings.broker),
              "account_scope": settings.broker.account_scope_id.strip() or "unscoped",
              "code_version": code_version_cached(), "process": process, "strategy_mode": mode},
        send_orders=send_orders, shadow_sink=shadow_sink, risk_guard=risk_guard,
    )
    runner.summary.strategy_mode = mode
    return runner, executor


def build_daily_risk_guard(settings: Settings, ledger_store, app_logger, *, enforce: bool):
    """C3 일일 위험 한도 — paper는 중단 파일을 씀, shadow는 계산·기록만."""
    from app.daily_risk import DailyRiskGuard
    from domain.service.trade_costs import CostProfile
    from infra.storage.daily_risk import DailyRiskStore, daily_risk_path
    st = settings.swing_strategy
    return DailyRiskGuard(
        store=DailyRiskStore(daily_risk_path(settings.storage.state_file)), ledger_store=ledger_store,
        evidence_store=FillEvidenceStore(evidence_path(settings.storage.fill_ledger_file), read_only=True),
        commands_dir=settings.storage.commands_dir, logger=app_logger, limit=st.daily_loss_limit,
        max_failures=st.max_failed_orders, profile=CostProfile(st.commission_rate, st.sell_tax_rate), enforce=enforce)


def _jsonl_sink(path: Path):
    """append-only JSON Lines 기록 함수(그림자 의도·전략 판단)."""
    def sink(rec: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return sink


def build_s1_strategy(settings: Settings, broker, calendar, quote_source, session_cfg, *, candidates=None):
    """S1 전략(M3 B 진입·C2 E1 청산) — 후보 승인(읽기 전용)·주문 직전 가격(현재가·최우선 매도호가)·신호 키·손절/시간 청산."""
    from datetime import time as _time
    from app.candidates import CandidateReader
    from app.s1_strategy import S1EntryStrategy
    from infra.storage.signal_keys import SignalKeyStore, keys_path

    st = settings.swing_strategy
    reader = CandidateReader(st.watch_dir, calendar, min_after_close_min=st.min_after_close_min)
    if quote_source is not None and hasattr(quote_source, "get_quote"):
        quote = quote_source.get_quote
    else:                       # MOCK_LOCAL 등 — 호가가 없으면 현재가를 호가로(그림자 확인용, 실제 전송 아님)
        def quote(sym):
            px = broker.get_market_price(sym).current_price
            return px, px
    if quote_source is not None and hasattr(quote_source, "get_prices"):
        def price(sym):                 # E1 손절 판단용 현재가 — 실패·0은 None(매도 안 함)
            v = quote_source.get_prices([sym]).get(sym)
            return v if type(v) is int and v > 0 else None
    else:
        def price(sym):
            v = broker.get_market_price(sym).current_price
            return v if type(v) is int and v > 0 else None
    hh, mm = st.entry_start.split(":")
    entry_end = session_cfg.entry_cancel_at or _time(9, 30)
    return S1EntryStrategy(
        mode=st.mode, scope=settings.broker.account_scope_id.strip() or "unscoped",
        candidates=candidates or reader.read, quote=quote,
        keys=SignalKeyStore(keys_path(settings.storage.state_file, st.mode)),
        decisions=_jsonl_sink(Path(settings.storage.state_file).with_name("strategy_decisions.jsonl")),
        entry_start=_time(int(hh), int(mm)), entry_end=entry_end, quantity=st.quantity,
        calendar=calendar, price=price, allowed_symbols=st.allowed_symbols)


def run_session(settings: Settings, broker, app_logger, *, strategy=None, clock=None, sleep=None,
                should_stop=None, quote_source=None, candidates=None):
    """하루 수명주기 실행. 테스트에서 clock/sleep/should_stop을 주입할 수 있음."""
    from infra.storage.runner_status import status_path
    runner, executor = build_session_runner(settings, broker, app_logger, strategy=strategy, clock=clock, sleep=sleep,
                                            should_stop=should_stop, quote_source=quote_source,
                                            status_file=status_path(settings), use_settings_strategy=True,
                                            candidates=candidates)
    try:
        summary = runner.run()
    finally:
        executor.shutdown()
    summary.run_id = runner.run_id
    summary.execution_mode = resolve_execution_mode(settings.broker)
    summary.account_scope = settings.broker.account_scope_id.strip() or "unscoped"
    if summary.status != "CLOSED_DAY":
        missing = ""
        if summary.final_reconcile is None:
            last_at = runner.last_balance_at
            missing = ("마감 후 잔고 조회·대조 실패"
                       + (f" (마지막 성공 잔고 {last_at:%H:%M:%S} — 마감 검증에 사용 불가)" if last_at else ""))
        def make_report() -> bool:
            try:
                from app.reports import generate_daily_report
                generate_daily_report(settings, summary.trade_date, balance=None,
                                      reconcile_report=summary.final_reconcile, session_lines=summary.lines(),
                                      calendar=runner.calendar, logger=app_logger, today=summary.trade_date,
                                      final_only=True, reconcile_missing_reason=missing,
                                      close_check=summary.close_check, close_issues=summary.close_issues)
                summary.report = "OK"
                return True
            except Exception as exc:
                summary.report = f"FAILED: {type(exc).__name__}: {exc}"
                app_logger.error(f"[REPORT] 일일 리포트 생성 실패(매매 결과에는 영향 없음): {type(exc).__name__}: {exc}")
                summary.add_close_issue("REPORT_FAILED")
                app_logger.critical(f"[SESSION_CLOSE] {summary.close_check} — REPORT_FAILED")
                return False

        report_ok = make_report()
        if write_session_status(settings, summary, app_logger) is None:
            summary.add_close_issue("STATUS_WRITE_FAILED")
            app_logger.critical(f"[SESSION_CLOSE] 상태 파일 저장 실패 — {summary.close_check}, "
                                f"{', '.join(summary.close_issues)} (run_id={summary.run_id})")
            # 9-A: 보고서가 먼저 만들어졌으므로 최종 판정(STATUS_WRITE_FAILED 포함)으로 다시 씀
            if report_ok:
                make_report()
    return summary


def write_session_status(settings, summary, app_logger) -> Path | None:
    """reports/session_status_<날짜>.json — 종료 상태와 마감 검증 결과 (8-D, F5).

    리포트 파일이 있다는 것만으로 하루가 정상이었다고 판단하지 않도록, 판정
    결과를 기계가 읽을 수 있는 형태로 따로 남깁니다(원자적 쓰기)."""
    import json as _json
    import os as _os
    import tempfile as _tempfile
    tmp = path = None
    try:
        d = Path(settings.storage.reports_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"session_status_{summary.trade_date.isoformat()}.json"
        with _tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=d, prefix=path.name + ".",
                                          suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            _json.dump(summary.to_status_dict(), fh, ensure_ascii=False, indent=2)
            fh.flush()
            _os.fsync(fh.fileno())
        _os.replace(tmp, path)
        tmp = None
        return path
    except Exception as exc:
        app_logger.error(f"[SESSION_STATUS] 상태 파일 저장 실패: {type(exc).__name__}: {exc}")
        # 8-E (R3): 같은 날짜의 이전 실행 파일이 이번 결과처럼 보이지 않게 치움
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as rm_exc:
                app_logger.critical(f"[SESSION_STATUS] 이전 상태 파일이 남아 있음(이번 실행 결과 아님): "
                                    f"{path} — {rm_exc}")
        return None
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _make_daily_bar_updater(settings, broker, calendar, state, ledger_store, app_logger):
    def update(_trade_date):
        from infra.market_data.daily_bar_repository import DailyBarRepository
        from infra.market_data.daily_bar_source import KiwoomDailyBarSource, PacedFetcher
        from infra.market_data.daily_bar_store import DailyBarStore

        md = settings.market_data
        repo = DailyBarRepository(
            DailyBarStore(md.daily_bars_dir),
            PacedFetcher(KiwoomDailyBarSource(broker), min_interval_sec=md.min_call_interval_sec,
                         retry_backoff_sec=md.retry_backoff_sec, logger=app_logger),
            calendar, backfill_pages=md.backfill_pages, logger=app_logger)
        held = set(apply_events(ledger_store.load()).positions())
        symbols = sorted(set(settings.session.watch_symbols) | held | set(state.positions))
        from infra.market_data.daily_bar_repository import FAILED
        now = now_local()
        failed = []
        for sym in symbols:
            res = repo.update(sym, now)
            app_logger.info(f"[DAILY_BARS] {res.line()}")
            if res.action == FAILED:
                failed.append(sym)
        return f"{len(failed)}/{len(symbols)}종목 실패 {failed}" if failed else ""
    return update


def main() -> int:
    """종료 코드: 0 정상 / 1 비정상 종료(예외) / 2 끝까지 돌았지만 마감 검증 NEEDS_REVIEW (8-D)."""
    exit_code = 0
    check_only = "--check-only" in sys.argv[1:]
    try:
        summary = asyncio.run(async_main(check_only=check_only))
        if getattr(summary, "close_check", "") == "NEEDS_REVIEW" or getattr(summary, "close_issues", None):
            print(f"\n[주의] 마감 검증 {summary.close_check} — {', '.join(summary.close_issues)} "
                  f"(app.log [SESSION_CLOSE])")
            exit_code = 2
    except KeyboardInterrupt:
        print("\n[종료] Ctrl+C 감지 — 정상 종료 처리 중...")
    except Exception as exc:
        logging.getLogger(__name__).exception("스윙 자동매매 프로그램 치명적 오류")
        print(f"\n[오류] 프로그램이 비정상 종료됐습니다: {exc}")
        exit_code = 1
    finally:
        logging.shutdown()
        print("[종료] 로그 정리 완료. 프로그램을 종료합니다.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
