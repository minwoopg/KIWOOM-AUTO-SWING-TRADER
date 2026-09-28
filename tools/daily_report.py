"""일일 리포트 다시 만들기 (파일만 읽음 — 브로커 호출 없음).

    python tools/daily_report.py                 # 오늘(KST)
    python tools/daily_report.py --date 2026-09-28

하루 수명주기(`python -m app.main`)가 끝날 때 자동으로 만들어집니다. 이 도구는
다시 만들거나 지난 날짜를 볼 때 씁니다. 잔고 없이 만들므로 장부 대조는 생략됩니다.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports import generate_daily_report  # noqa: E402
from config.settings import load_settings  # noqa: E402
from utils.time_utils import now_local  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="스윙 일일 리포트 생성")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    args = ap.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else now_local().date()
    settings = load_settings(ROOT / "config" / "settings.yaml")
    path = generate_daily_report(settings, day)
    print(path)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
