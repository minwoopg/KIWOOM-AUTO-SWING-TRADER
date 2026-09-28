"""하루 운영 기록 번들 (민감정보 가림, 브로커 호출 없음).

    python tools/export_bundle.py                  # 오늘(KST)
    python tools/export_bundle.py --date 2026-09-28

결과: exports/swing_bundle_<날짜>.zip — GPT 검토·사후 분석 공유용.
앱키·토큰·계좌번호 형태 값은 가려집니다(단타 레포 번들과 같은 규칙).
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.reports import export_bundle  # noqa: E402
from config.settings import load_settings  # noqa: E402
from utils.time_utils import now_local  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="하루 운영 기록 번들")
    ap.add_argument("--date", help="YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--out-dir", default=str(ROOT / "exports"))
    args = ap.parse_args(argv)
    day = date.fromisoformat(args.date) if args.date else now_local().date()
    settings = load_settings(ROOT / "config" / "settings.yaml")
    path = export_bundle(settings, day, root=ROOT, out_dir=args.out_dir)
    print(path)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
