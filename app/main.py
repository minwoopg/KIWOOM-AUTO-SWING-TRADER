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
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config.settings import Settings, load_settings
from domain.models import AccountBalance
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker
from infra.market_data.quote_source import KiwoomQuoteSource
from infra.storage.logger import build_app_logger
from infra.storage.process_lock import single_instance_lock
from infra.storage.run_baseline import perform_run_baseline_startup
from domain.position.position_book import ReconcileReport, reconcile
from domain.service.lot_ledger import LotMatchError, apply_events
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
    """단타 레포 app/main.py와 동일."""
    if settings.broker.use_mock:
        return MockBroker()
    return KiwoomBroker(settings.broker)


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
    balance: AccountBalance | None = None
    balance_error: str = ""
    unresolved_intent_symbols: list[str] = field(default_factory=list)
    journal_symbols: list[str] = field(default_factory=list)
    journal_error: str = ""
    ledger_error: str = ""
    reconcile: ReconcileReport | None = None

    @property
    def has_unresolved_orders(self) -> bool:
        return bool(self.unresolved_intent_symbols or self.journal_symbols or self.journal_error)


async def run_startup_checks(settings: Settings, broker, app_logger, *, sleep=asyncio.sleep) -> StartupReport:
    """인증 → 잔고 → 미해결 주문 흔적 확인. 주문은 절대 내지 않습니다."""
    is_live = not settings.broker.is_paper_trading
    report = StartupReport(mode="실전투자" if is_live else "모의투자")

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
        if is_live:
            app_logger.critical("[STARTUP_BLOCK] 실전투자 — 잔고 조회 실패로 시작하지 않습니다.")
            raise StartupBlockedError("실전투자 시작 시 잔고 조회 실패") from exc

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
        if is_live:
            raise StartupBlockedError("실전투자 — 주문 기록을 확인할 수 없어 시작하지 않습니다.")
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
        if is_live:
            raise StartupBlockedError("실전투자 — 체결 원장 손상으로 시작하지 않습니다.") from exc
        return report
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
    return report


async def async_main(check_only: bool = False):
    load_dotenv()
    settings = load_settings()
    with single_instance_lock(Path(settings.storage.state_file).with_suffix(".lock")):
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
    return SessionConfig(
        poll_interval_sec=float(s.poll_interval_sec), close_reconcile_until=_time(int(hh), int(mm)),
        watch_symbols=tuple(str(x) for x in s.watch_symbols),
    )


def run_session(settings: Settings, broker, app_logger, *, strategy=None, clock=None, sleep=None,
                should_stop=None, quote_source=None):
    """하루 수명주기 실행. 테스트에서 clock/sleep/should_stop을 주입할 수 있음."""
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
    runner = SessionRunner(
        broker=broker, executor=executor, state=state, ledger_store=ledger_store,
        recorder=FillRecorder(ledger_store, logger=app_logger,
                              scope=settings.broker.account_scope_id.strip() or "unscoped"), calendar=calendar,
        strategy=strategy or NullStrategy(), guard_config=build_guard_config(settings.guard),
        session_config=build_session_config(settings.session), logger=app_logger,
        clock=clock or now_local, sleep=sleep or _time_mod.sleep, should_stop=should_stop or (lambda: False),
        after_close=after_close,
        quote_source=quote_source if quote_source is not None else (
            KiwoomQuoteSource(broker, logger=app_logger) if isinstance(broker, KiwoomBroker) else None),
    )
    import uuid as _uuid
    try:
        summary = runner.run()
    finally:
        executor.shutdown()
    summary.run_id = _uuid.uuid4().hex[:12]
    if summary.status != "CLOSED_DAY":
        missing = ""
        if summary.final_reconcile is None:
            last_at = runner.last_balance_at
            missing = ("마감 후 잔고 조회·대조 실패"
                       + (f" (마지막 성공 잔고 {last_at:%H:%M:%S} — 마감 검증에 사용 불가)" if last_at else ""))
        try:
            from app.reports import generate_daily_report
            generate_daily_report(settings, summary.trade_date, balance=None,
                                  reconcile_report=summary.final_reconcile, session_lines=summary.lines(),
                                  calendar=calendar, logger=app_logger, today=summary.trade_date,
                                  final_only=True, reconcile_missing_reason=missing,
                                  close_check=summary.close_check, close_issues=summary.close_issues)
            summary.report = "OK"
        except Exception as exc:
            summary.report = f"FAILED: {type(exc).__name__}: {exc}"
            app_logger.error(f"[REPORT] 일일 리포트 생성 실패(매매 결과에는 영향 없음): {type(exc).__name__}: {exc}")
            summary.add_close_issue("REPORT_FAILED")
            app_logger.critical(f"[SESSION_CLOSE] {summary.close_check} — REPORT_FAILED")
        if write_session_status(settings, summary, app_logger) is None:
            summary.add_close_issue("STATUS_WRITE_FAILED")
            app_logger.critical(f"[SESSION_CLOSE] 상태 파일 저장 실패 — {summary.close_check}, "
                                f"{', '.join(summary.close_issues)} (run_id={summary.run_id})")
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
