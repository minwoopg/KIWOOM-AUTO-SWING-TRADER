"""일봉 수집·갱신 (조회 전용, 스윙 분리 5라운드).

사용법 (스윙 레포 루트, PowerShell)
    python tools/update_daily_bars.py 005930 000660
    python tools/update_daily_bars.py --symbols-file config\\symbols.txt

- config/settings.yaml의 broker.base_url·.env 키로 인증합니다(use_mock 설정과 무관 — 조회 전용).
- 일봉 조회(ka10081)만 호출합니다. 주문 API는 쓰지 않습니다.
- 결과는 market_data.daily_bars_dir(기본 data/daily_bars)에 종목별로 저장.
- 장중에 실행해도 당일 미완성 봉은 저장하지 않습니다(마지막 완성 거래일까지만).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import load_dotenv  # noqa: E402
from config.settings import load_settings  # noqa: E402
from infra.market_data.daily_bar_repository import FAILED, DailyBarRepository  # noqa: E402
from infra.market_data.daily_bar_source import KiwoomDailyBarSource, PacedFetcher  # noqa: E402
from infra.market_data.daily_bar_store import DailyBarStore  # noqa: E402
from infra.storage.logger import build_app_logger  # noqa: E402
from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402


def read_symbols(args) -> list[str]:
    symbols = list(args.symbols)
    if args.symbols_file:
        for line in Path(args.symbols_file).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                symbols.append(line)
    out = list(dict.fromkeys(s.strip() for s in symbols if s.strip()))
    if not out:
        raise SystemExit("종목코드를 하나 이상 지정하세요")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="일봉 수집·갱신 (조회 전용)")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--symbols-file")
    args = ap.parse_args(argv)
    symbols = read_symbols(args)

    load_dotenv(str(ROOT / ".env"))
    settings = load_settings(ROOT / "config" / "settings.yaml")
    # broker.use_mock 설정과 무관하게 실제 키움 API(settings의 base_url)로 조회합니다 —
    # 조회 전용이고, MockBroker에는 일봉 원본이 없기 때문입니다.
    if not settings.broker.app_key or not settings.broker.secret_key:
        print("[중단] .env에 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 없습니다.")
        return 2
    logger = build_app_logger(settings.storage.app_log_file, settings.app.log_level)

    from infra.broker.kiwoom_broker import KiwoomBroker
    broker = KiwoomBroker(settings.broker)
    broker.authenticate()

    md = settings.market_data
    repo = DailyBarRepository(
        DailyBarStore(str(ROOT / md.daily_bars_dir) if not Path(md.daily_bars_dir).is_absolute()
                      else md.daily_bars_dir),
        PacedFetcher(KiwoomDailyBarSource(broker), min_interval_sec=md.min_call_interval_sec,
                     retry_backoff_sec=md.retry_backoff_sec, logger=logger),
        TradingCalendar.load(),
        backfill_pages=md.backfill_pages, logger=logger,
    )
    now = now_local()
    failed = 0
    for sym in symbols:
        res = repo.update(sym, now)
        print(res.line())
        logger.info(f"[DAILY_BARS] {res.line()}")
        failed += res.action == FAILED
    print(f"\n완료: {len(symbols)}종목, 실패 {failed} | API 호출 {repo.fetcher.calls}회, 재시도 {repo.fetcher.retries}회")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
