# -*- coding: utf-8 -*-
"""거래일 캘린더 회귀 테스트 (스윙 분리 3라운드, 2026-09-28)."""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime, time, timezone, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from utils.trading_calendar import (
    CalendarConfigError, CalendarCoverageError, MarketPhase, TradingCalendar,
)

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def yaml_file(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "cal.yaml"
    p.write_text(text, encoding="utf-8")
    return p


cal = TradingCalendar.load()
D = date.fromisoformat

# ── 1. 실제 설정 파일 ─────────────────────────────────────────
check("1-1) config/krx_calendar.yaml 로드", 2026 in cal.covered_years)
check("1-2) 추석 9/24·25 휴장", not cal.is_trading_day(D("2026-09-24")) and not cal.is_trading_day(D("2026-09-25")))
check("1-3) 9/28(월) 정상 개장 (추석 토요일 겹침은 대체공휴일 아님)", cal.is_trading_day(D("2026-09-28")))
check("1-4) 10/5 개천절 대체공휴일 휴장", not cal.is_trading_day(D("2026-10-05")))
check("1-5) 7/17 제헌절 휴장 (2026년 재지정)", not cal.is_trading_day(D("2026-07-17")))
check("1-6) 주말 휴장", not cal.is_trading_day(D("2026-09-27")))
check("1-7) 휴장 사유 조회", cal.holiday_name(D("2026-06-03")) == "전국동시지방선거"
      and cal.holiday_name(D("2026-09-26")) == "주말" and cal.holiday_name(D("2026-09-28")) is None)

# ── 2. 거래일 계산 ────────────────────────────────────────────
check("2-1) 9/23 다음 거래일 = 9/28 (추석·주말 건너뜀)", cal.next_trading_day(D("2026-09-23")) == D("2026-09-28"))
check("2-2) 9/28 이전 거래일 = 9/23", cal.previous_trading_day(D("2026-09-28")) == D("2026-09-23"))
check("2-3) 10/2(금) + 1거래일 = 10/6 (10/5 대체공휴일)", cal.add_trading_days(D("2026-10-02"), 1) == D("2026-10-06"))
check("2-4) 10/6 - 2거래일 = 10/1", cal.add_trading_days(D("2026-10-06"), -2) == D("2026-10-01"))
check("2-5) 0거래일은 자기 자신", cal.add_trading_days(D("2026-09-28"), 0) == D("2026-09-28"))
check("2-6) 휴장일에 0거래일 → 예외", raises(lambda: cal.add_trading_days(D("2026-10-05"), 0), ValueError))
check("2-7) 보유 거래일수: 9/23 매수 → 9/28 기준 1", cal.trading_days_between(D("2026-09-23"), D("2026-09-28")) == 1)
check("2-8) 보유 거래일수: 10/1 → 10/12 = 5 (10/2·6·7·8·12, 10/5·9 휴장)",
      cal.trading_days_between(D("2026-10-01"), D("2026-10-12")) == 5)
check("2-9) 역순이면 음수", cal.trading_days_between(D("2026-09-28"), D("2026-09-23")) == -1)
check("2-10) 같은 날 0", cal.trading_days_between(D("2026-09-28"), D("2026-09-28")) == 0)
check("2-11) 범위 내 거래일 목록", cal.trading_days_in_range(D("2026-09-21"), D("2026-09-28"))
      == [D("2026-09-21"), D("2026-09-22"), D("2026-09-23"), D("2026-09-28")])

# ── 3. 장 단계 ───────────────────────────────────────────────
dt = lambda s: datetime.fromisoformat(s)
check("3-1) 08:59 PRE_OPEN", cal.phase(dt("2026-09-28T08:59:59")) == MarketPhase.PRE_OPEN)
check("3-2) 09:00 REGULAR", cal.phase(dt("2026-09-28T09:00:00")) == MarketPhase.REGULAR)
check("3-3) 15:19:59 REGULAR", cal.phase(dt("2026-09-28T15:19:59")) == MarketPhase.REGULAR)
check("3-4) 15:20 CLOSING_AUCTION", cal.phase(dt("2026-09-28T15:20:00")) == MarketPhase.CLOSING_AUCTION)
check("3-5) 15:30 POST_CLOSE", cal.phase(dt("2026-09-28T15:30:00")) == MarketPhase.POST_CLOSE)
check("3-6) 휴장일 CLOSED_DAY", cal.phase(dt("2026-10-05T10:00:00")) == MarketPhase.CLOSED_DAY)
check("3-7) 연초 개장일 1/2 09:30은 아직 PRE_OPEN(10시 개장)", cal.phase(dt("2026-01-02T09:30:00")) == MarketPhase.PRE_OPEN)
utc = timezone.utc
check("3-8) aware(UTC) 시각은 KST로 변환 — 00:30Z = 09:30 KST",
      cal.phase(datetime(2026, 9, 28, 0, 30, tzinfo=utc)) == MarketPhase.REGULAR)

# ── 4. 완성된 마지막 거래일 (일봉 판정 기준) ─────────────────
check("4-1) 장중이면 직전 거래일", cal.last_completed_session(dt("2026-09-28T10:00")) == D("2026-09-23"))
check("4-2) 장 시작 전이어도 직전 거래일", cal.last_completed_session(dt("2026-09-28T08:00")) == D("2026-09-23"))
check("4-3) 종가 단일가 중에도 아직 직전 거래일", cal.last_completed_session(dt("2026-09-28T15:25")) == D("2026-09-23"))
check("4-4) 장 마감 후면 당일", cal.last_completed_session(dt("2026-09-28T15:30")) == D("2026-09-28"))
check("4-5) 휴장일(10/5)이면 직전 거래일(10/2)", cal.last_completed_session(dt("2026-10-05T16:00")) == D("2026-10-02"))

# ── 5. fail-closed ───────────────────────────────────────────
check("5-1) 다루지 않는 연도 → CalendarCoverageError",
      raises(lambda: cal.is_trading_day(D("2027-01-04")), CalendarCoverageError))
check("5-2) 연말 경계에서 다음 해로 넘어가도 추측하지 않음",
      raises(lambda: cal.next_trading_day(D("2026-12-30")), CalendarCoverageError))
check("5-3) 파일 없음 → CalendarConfigError",
      raises(lambda: TradingCalendar.load("/nonexistent/cal.yaml"), CalendarConfigError))
check("5-4) YAML 깨짐 → CalendarConfigError",
      raises(lambda: TradingCalendar.load(yaml_file("covered_years: [[[")), CalendarConfigError))
base = "covered_years: [2026]\nregular_session: {open: '09:00', close: '15:30'}\n"
check("5-5) 날짜 형식 오류 → CalendarConfigError",
      raises(lambda: TradingCalendar.load(yaml_file(base + "holidays: {'2026-13-01': x}\n")), CalendarConfigError))
check("5-6) covered_years 밖 휴장일 → CalendarConfigError",
      raises(lambda: TradingCalendar.load(yaml_file(base + "holidays: {'2027-01-01': x}\n")), CalendarConfigError))
check("5-7) 주말을 휴장일로 적으면 → CalendarConfigError (오타 방지)",
      raises(lambda: TradingCalendar.load(yaml_file(base + "holidays: {'2026-09-26': x}\n")), CalendarConfigError))
check("5-8) 시간 형식 오류 → CalendarConfigError",
      raises(lambda: TradingCalendar.load(yaml_file(
          "covered_years: [2026]\nregular_session: {open: '9시', close: '15:30'}\n")), CalendarConfigError))
check("5-9) regular_session 누락 → CalendarConfigError",
      raises(lambda: TradingCalendar.load(yaml_file("covered_years: [2026]\n")), CalendarConfigError))

# ── 6. 일봉 대조 ─────────────────────────────────────────────
days = cal.trading_days_in_range(D("2026-09-14"), D("2026-09-28"))
r = cal.compare_with_bar_dates(days, D("2026-09-14"), D("2026-09-28"))
check("6-1) 봉 날짜와 캘린더가 같으면 불일치 없음", r == {"missing_bars": [], "unexpected_bars": []})
r = cal.compare_with_bar_dates([d for d in days if d != D("2026-09-22")] + [D("2026-09-24")],
                               D("2026-09-14"), D("2026-09-28"))
check("6-2) 누락된 거래일과 휴장일의 봉을 각각 잡아냄",
      r == {"missing_bars": [D("2026-09-22")], "unexpected_bars": [D("2026-09-24")]})

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
