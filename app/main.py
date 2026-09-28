from __future__ import annotations

"""스윙 자동매매 진입점 — 1라운드: 기반 점검 모드 (2026-09-28).

운영 방식(민우님 확정): 장 시작 전에 켜고 장 마감 후 끈다.

이 라운드에는 매매 루프가 없습니다. 단타 레포 `app/main.py`(bdde6c2)의
기동 순서 중 매매 로직과 무관한 부분만 옮겨, 아래를 한 번 실행하고
종료합니다.

1. .env / settings.yaml 로드
2. 단일 인스턴스 락 (상태 파일 기준 — 단타 레포와 경로가 달라 서로 막지 않음)
3. 실행 기준선 기록 (run_id / git sha / 설정 해시)
4. 브로커 인증 (429 재시도)
5. 잔고 조회 (실전투자에서 실패하면 시작 중단 — 단타 레포와 동일 원칙)
6. 이전 프로세스가 남긴 미해결 주문 흔적 확인
   (state.json의 unresolved_order_intents + tracked_order_journal)
   — 읽기만 하고 아무것도 자동 해소하지 않습니다.
7. 카카오 시작 알림 (백그라운드, 실패해도 무시)

주문 실행부(OrderExecutor)와 스윙 매매 루프는 다음 라운드에서 연결합니다.
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
from infra.storage.logger import build_app_logger
from infra.storage.process_lock import single_instance_lock
from infra.storage.run_baseline import perform_run_baseline_startup
from infra.storage.state_store import JsonStateStore
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
    # 단타 레포 TradingService._restore_order_recovery_blocks()가 보는 두
    # 출처와 동일. 이 라운드는 PSM이 없으므로 "있다/없다"만 보고합니다.
    try:
        state, _highest = JsonStateStore(settings.storage.state_file).load()
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
    return report


async def async_main() -> None:
    load_dotenv()
    settings = load_settings()
    with single_instance_lock(Path(settings.storage.state_file).with_suffix(".lock")):
        await _run_application(settings)


async def _run_application(settings: Settings) -> None:
    # 단타 레포와 동일: 구버전 .pyc 캐시로 인한 AttributeError 방지
    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    app_logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    print("=" * 50)
    print("  스윙 자동매매 — 기반 점검 모드 (매매 루프 없음)")
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
        watch_line="기반 점검 모드(매매 루프 없음)",
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
    app_logger.info("[STARTUP] 기반 점검 완료 — 매매 루프 없이 종료합니다(1라운드).")


def main() -> int:
    """단타 레포 app/main.py의 main()과 동일한 종료 코드 규칙."""
    exit_code = 0
    try:
        asyncio.run(async_main())
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
