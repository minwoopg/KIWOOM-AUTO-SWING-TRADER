from __future__ import annotations

"""완성 주봉과 주간 추세 근사 (보충안 5절, weekly_version = w2).

A13-R2 수정 (w1 → w2): 일봉용으로 기준일 뒤를 잘라낸 세션 목록을 주봉 판정에 쓰면 수요일을
"그 주 마지막 세션"으로 오인합니다. 그래서 주봉은 세션 목록이 아니라 **주 단위 예정 일정
(WeekSchedule)** 으로만 판정합니다.

- 주 = Asia/Seoul 기준 월~일.
- 그 주의 예정 세션·마지막 세션은 WeekSchedule이 알려줍니다.
  * `CalendarWeekSchedule(TradingCalendar)`: 휴장일은 미리 공지되므로 기준일에 알 수 있는 정보.
    달력이 그 해를 다루지 않으면 None(일정 불명).
  * `ExplicitWeekSchedule(sessions, holidays, known_through)`: known_through까지의 **모든 평일이
    세션 또는 휴장일로 명시**돼 있어야 합니다(잘린 목록이면 ScheduleCoverageError).
- 시각 (모두 Asia/Seoul 기준 naive datetime — 프로젝트의 now_local()과 같은 규약)
  * `session_closed_at` : 마지막 세션의 정규장 종료 시각(특수 운영일 반영).
  * `available_at`      : 주봉을 계산에 쓸 수 있는 시각.
      - 기본(ASSUMED_DELAY): session_closed_at + data_delay(기본 30분 — 장 마감 후 일봉 확정·수집 여유).
        **백필 해석**: 과거 주는 실제 수집 시각을 알 수 없으므로 이 가정 시각을 씁니다.
      - 관측(OBSERVED): data_ready_at[마지막 세션]이 주어지면 max(session_closed_at, 그 시각).
        앞으로 쌓는 기록은 수집기가 마지막 봉의 실제 확보 시각을 넘겨야 합니다.
  * 평가 시각 as_of보다 available_at이 늦은 주는 결과에 넣지 않습니다(진행 중인 주 포함).
- 봉이 빠진 주·일정을 모르는 지난 주는 complete=False로 남깁니다(건너뛰어 압축하지 않음).
- 주봉: O=첫 세션 시가, H=최고, L=최저, C=마지막 세션 종가, V·거래대금=합계(거래대금은 하나라도
  없으면 None).
- SMA30W = 최근 30개 완성 주봉 종가 평균, slope4W = SMA30W[w]/SMA30W[w-4]-1 (34주 연속 필요).
- 상태: UP_PROXY / DOWN_PROXY / UNCLASSIFIED / UNKNOWN. Stage 1~4 분류가 아닙니다(근사).
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol, Sequence

from domain.research.series import ResearchBar, ResearchBarError, validate_sessions

WEEKLY_VERSION = "w2"
UP_PROXY, DOWN_PROXY, UNCLASSIFIED, UNKNOWN = "UP_PROXY", "DOWN_PROXY", "UNCLASSIFIED", "UNKNOWN"
DEFAULT_DATA_DELAY = timedelta(minutes=30)
ASSUMED_DELAY, OBSERVED = "ASSUMED_DELAY", "OBSERVED"


class ScheduleCoverageError(ValueError):
    """일정 목록이 주장한 범위(known_through)를 실제로 다 채우지 않음."""


class WeekSchedule(Protocol):
    def sessions_in_week(self, monday: date) -> list[date] | None: ...   # None = 일정 불명
    def close_at(self, d: date) -> datetime: ...


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


class CalendarWeekSchedule:
    """utils.trading_calendar.TradingCalendar 어댑터."""

    def __init__(self, calendar) -> None:
        self.cal = calendar

    def sessions_in_week(self, monday: date) -> list[date] | None:
        days = [monday + timedelta(days=i) for i in range(7)]
        try:
            return [d for d in days if self.cal.is_trading_day(d)]
        except Exception:          # CalendarCoverageError 등 — 그 해를 모름
            return None

    def close_at(self, d: date) -> datetime:
        st = self.cal.session_times(d)
        if st is None:
            raise ValueError(f"{d}는 거래일이 아님")
        return datetime.combine(d, st.close)


class ExplicitWeekSchedule:
    """명시적 세션·휴장일 목록. known_through까지의 모든 평일이 둘 중 하나에 있어야 함."""

    def __init__(self, sessions: Sequence[date], holidays: Sequence[date], known_through: date,
                 close: time = time(15, 30), special_close: dict[date, time] | None = None) -> None:
        validate_sessions(sessions, "schedule.sessions")
        self.sessions = set(sessions)
        self.holidays = set(holidays)
        if self.sessions & self.holidays:
            raise ScheduleCoverageError("세션이면서 휴장일인 날짜가 있음")
        self.known_through = known_through
        self.close = close
        self.special_close = dict(special_close or {})
        start = min(self.sessions | self.holidays) if (self.sessions or self.holidays) else known_through
        d = _monday(start)
        while d <= known_through:
            if d.weekday() < 5 and d not in self.sessions and d not in self.holidays and d >= start:
                raise ScheduleCoverageError(
                    f"{d}: known_through({known_through}) 이전 평일인데 세션도 휴장일도 아님 — 잘린 목록?")
            d += timedelta(days=1)

    def sessions_in_week(self, monday: date) -> list[date] | None:
        if monday + timedelta(days=6) > self.known_through:
            return None
        return sorted(d for d in self.sessions if _monday(d) == monday)

    def close_at(self, d: date) -> datetime:
        return datetime.combine(d, self.special_close.get(d, self.close))


@dataclass(frozen=True)
class WeeklyBar:
    week_start: date                    # 월요일
    week_end_session: date | None       # 그 주 마지막 예정 세션
    session_closed_at: datetime | None
    available_at: datetime | None
    availability_basis: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    trade_value: int | None
    complete: bool
    reason: str = ""


def weekly_bars(bars: Sequence[ResearchBar], schedule: WeekSchedule, as_of: datetime, *,
                data_ready_at: dict[date, datetime] | None = None,
                data_delay: timedelta = DEFAULT_DATA_DELAY) -> list[WeeklyBar]:
    """as_of(Asia/Seoul naive) 시점에 사용 가능한 주봉 목록(오래된 순)."""
    if not isinstance(as_of, datetime):
        raise ResearchBarError("as_of는 datetime(Asia/Seoul naive)이어야 함")
    dates = [b.date for b in bars]
    if any(b <= a for a, b in zip(dates, dates[1:])):
        raise ResearchBarError("일봉 날짜가 오름차순·중복 없음이 아님")
    by_date = {b.date: b for b in bars if b.date <= as_of.date()}
    if not by_date:
        return []
    first_bar = min(by_date)
    ready = data_ready_at or {}
    out: list[WeeklyBar] = []
    monday = _monday(first_bar)
    last_monday = _monday(as_of.date())
    while monday <= last_monday:
        ss = schedule.sessions_in_week(monday)
        sunday = monday + timedelta(days=6)
        if ss is None:
            if sunday < as_of.date():           # 지난 주인데 일정 불명 → 불완전으로 남김
                out.append(WeeklyBar(monday, None, None, None, "", None, None, None, None, None, None,
                                     False, "SCHEDULE_UNKNOWN"))
            monday += timedelta(days=7)
            continue
        if not ss:                              # 한 주 전체 휴장
            monday += timedelta(days=7)
            continue
        last = ss[-1]
        closed_at = schedule.close_at(last)
        if last in ready:
            available_at, basis = max(closed_at, ready[last]), OBSERVED
        else:
            available_at, basis = closed_at + data_delay, ASSUMED_DELAY
        if available_at > as_of:                # 진행 중이거나 아직 확보 전
            monday += timedelta(days=7)
            continue
        if ss[0] < first_bar:                   # 상장 전 주
            monday += timedelta(days=7)
            continue
        got = [by_date.get(s) for s in ss]
        if any(b is None for b in got):
            out.append(WeeklyBar(monday, last, closed_at, available_at, basis, None, None, None, None, None,
                                 None, False, f"MISSING_DAILY:{sum(b is None for b in got)}"))
        else:
            tv = None if any(b.trade_value is None for b in got) else sum(b.trade_value for b in got)
            out.append(WeeklyBar(monday, last, closed_at, available_at, basis, got[0].open,
                                 max(b.high for b in got), min(b.low for b in got), got[-1].close,
                                 sum(b.volume for b in got), tv, True))
        monday += timedelta(days=7)
    return out


@dataclass(frozen=True)
class WeeklyTrend:
    state: str
    last_week_end: date | None
    available_at: datetime | None
    close: float | None
    sma30w: float | None
    slope4w: float | None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "last_week_end": self.last_week_end.isoformat() if self.last_week_end else None,
                "available_at": self.available_at.isoformat() if self.available_at else None,
                "close": self.close, "sma30w": self.sma30w, "slope4w": self.slope4w, "reason": self.reason}


def weekly_trend(weeks: Sequence[WeeklyBar]) -> WeeklyTrend:
    if len(weeks) < 34:
        last = weeks[-1] if weeks else None
        return WeeklyTrend(UNKNOWN, last.week_end_session if last else None, last.available_at if last else None,
                           None, None, None, f"INSUFFICIENT_WEEKS:{len(weeks)}/34")
    last34 = list(weeks[-34:])
    end, avail = last34[-1].week_end_session, last34[-1].available_at
    bad = [w for w in last34 if not w.complete]
    if bad:
        return WeeklyTrend(UNKNOWN, end, avail, None, None, None, f"INCOMPLETE_WEEK:{bad[-1].week_start.isoformat()}")
    closes = [w.close for w in last34]
    sma_now = sum(closes[-30:]) / 30
    sma_prev = sum(closes[-34:-4]) / 30
    slope = sma_now / sma_prev - 1 if sma_prev else float("nan")
    c = closes[-1]
    if not all(math.isfinite(x) for x in (c, sma_now, sma_prev, slope)):      # A13-R5
        return WeeklyTrend(UNKNOWN, end, avail, None, None, None, "NON_FINITE")
    if c > sma_now and slope > 0:
        state = UP_PROXY
    elif c < sma_now and slope < 0:
        state = DOWN_PROXY
    else:
        state = UNCLASSIFIED
    return WeeklyTrend(state, end, avail, c, sma_now, slope)
