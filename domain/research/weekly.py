from __future__ import annotations

"""완성 주봉과 주간 추세 근사 (보충안 5절, weekly_version = w4).

Q-R2 (w3 → w4): 가장 최근에 끝난 주의 입력이 다음 거래일이 시작된 뒤에도 준비되지 않았으면 잘라내지 않고
DATA_NOT_READY:OVERDUE 자리로 남깁니다(이전 주로 정상 추세를 내지 않음). 정상 대기(다음 거래일 전)만 잘라냄.

A13-Q2·Q3 (w2 → w3): 관측 모드는 주 전체 봉의 확보 시각, 끝난 주의 준비 안 된 입력은 불완전 자리로.

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
      - 관측(OBSERVED, mode="OBSERVED"): max(session_closed_at, 그 주 **모든 세션 봉**의 확보 시각).
        하나라도 확보 시각이 없으면 불완전(READY_TIME_UNKNOWN) — 30분 가정으로 메우지 않습니다.
        앞으로 쌓는 기록은 수집기가 각 봉의 실제 확보 시각을 넘겨야 합니다.
      - 두 모드는 섞지 않습니다(가정 모드에 data_ready_at을 넘기면 ValueError). data_delay 음수 거부.
  * 아직 끝나지 않은 주는 결과에 넣지 않습니다. 끝났지만 available_at > as_of인 과거 주는
    DATA_NOT_READY 불완전 자리로 남깁니다. 가장 최근에 끝난 주 하나만, 다음 거래일이 시작되기 전이면
    "아직 도착 전"으로 잘라냅니다 — 그 뒤에도 미확보면 OVERDUE 자리로 남아 추세 UNKNOWN.
- 봉이 빠진 주·일정을 모르는 지난 주는 complete=False로 남깁니다(건너뛰어 압축하지 않음).
- 한 주 전체가 예정 휴장이면 항목을 만들지 않고 다음 항목의 gap_weeks_before로 기록합니다.
  weekly_trend는 34주 창 안의 주 시작 간격이 7×(1+gap_weeks_before)일인지 확인합니다.
- 주봉: O=첫 세션 시가, H=최고, L=최저, C=마지막 세션 종가, V·거래대금=합계(거래대금은 하나라도
  없으면 None). 거래 없는 봉(거래량 0)은 합산에 포함하고 no_trade_days로 표시만 합니다(일봉 창 정책과 별개 —
  주간 추세는 종가만 씀).
- SMA30W = 최근 30개 완성 주봉 종가 평균, slope4W = SMA30W[w]/SMA30W[w-4]-1 (34주 연속 필요).
- 상태: UP_PROXY / DOWN_PROXY / UNCLASSIFIED / UNKNOWN. Stage 1~4 분류가 아닙니다(근사).
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol, Sequence

from domain.research.series import ResearchBar, ResearchBarError, validate_sessions

WEEKLY_VERSION = "w4"
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
    available_at: datetime | None       # 주 전체 필수 입력이 확보된 시각(관측) 또는 가정 시각
    availability_basis: str             # ASSUMED_DELAY / OBSERVED / ""
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | None
    trade_value: int | None
    complete: bool
    reason: str = ""
    gap_weeks_before: int = 0           # 직전 항목과의 사이에 있던 '한 주 전체 휴장' 주 수 (A13-Q3)
    no_trade_days: int = 0              # 거래 없는 봉(거래량 0) 수 — 표시만. 주봉은 종가 기반 관찰값이라 합산에 포함(A2)


def _placeholder(monday, last, closed_at, available_at, basis, reason, gap) -> WeeklyBar:
    return WeeklyBar(monday, last, closed_at, available_at, basis, None, None, None, None, None, None,
                     False, reason, gap)


def weekly_bars(bars: Sequence[ResearchBar], schedule: WeekSchedule, as_of: datetime, *,
                mode: str = ASSUMED_DELAY,
                data_ready_at: dict[date, datetime] | None = None,
                data_delay: timedelta = DEFAULT_DATA_DELAY) -> list[WeeklyBar]:
    """as_of(Asia/Seoul naive) 시점의 주봉 목록(오래된 순).

    mode (A13-Q2)
      ASSUMED_DELAY : 백필용. 확보 시각을 모르므로 마지막 세션 종료 + data_delay를 사용 가능 시각으로 가정.
                      data_ready_at을 함께 주면 오류(모드 혼용 금지).
      OBSERVED      : 앞으로 쌓는 기록. **그 주 모든 세션 봉의 실제 확보 시각**이 data_ready_at에 있어야 하고,
                      available_at = max(마지막 세션 종료, 각 봉 확보 시각). 하나라도 없으면 불완전
                      (READY_TIME_UNKNOWN) — 가정 시각으로 메우지 않음. 봉 확보 시각이 그 세션 종료보다
                      이르면 장중 미완성 봉으로 보고 불완전(READY_BEFORE_SESSION_CLOSE).
    포함 규칙 (A13-Q3)
      - 마지막 세션이 아직 끝나지 않은 주(진행 중)는 넣지 않습니다.
      - 끝났지만 입력이 아직 준비 안 된 주(DATA_NOT_READY)는 **불완전 자리로 남깁니다**. 단, 가장 최근에
        끝난 주 하나만은 다음 거래일 0시 전이면 "아직 도착 전"으로 보고 잘라냅니다(직전 주까지가 그 시점의
        최신 정보). 다음 거래일이 시작됐으면 장애 지연(OVERDUE)으로 자리를 남깁니다(Q-R2).
        그보다 앞선 주·중간에 낀 주는 남아서 추세를 UNKNOWN으로 만들고 창을 압축하지 못하게 합니다.
      - 한 주 전체가 예정 휴장인 주는 항목을 만들지 않고, 다음 항목의 gap_weeks_before로 기록합니다.
    """
    if not isinstance(as_of, datetime):
        raise ResearchBarError("as_of는 datetime(Asia/Seoul naive)이어야 함")
    if mode not in (ASSUMED_DELAY, OBSERVED):
        raise ValueError(f"mode는 {ASSUMED_DELAY}/{OBSERVED} — {mode!r}")
    if not isinstance(data_delay, timedelta) or data_delay < timedelta(0):
        raise ValueError("data_delay는 0 이상 timedelta")
    if mode == ASSUMED_DELAY and data_ready_at:
        raise ValueError("ASSUMED_DELAY 모드에는 data_ready_at을 넘기지 않음(모드 혼용 금지)")
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
    holiday_weeks = 0
    while monday <= last_monday:
        ss = schedule.sessions_in_week(monday)
        sunday = monday + timedelta(days=6)
        if ss is None:
            if sunday < as_of.date():           # 지난 주인데 일정 불명 → 불완전으로 남김
                out.append(_placeholder(monday, None, None, None, "", "SCHEDULE_UNKNOWN", holiday_weeks))
                holiday_weeks = 0
            monday += timedelta(days=7)
            continue
        if not ss:                              # 한 주 전체 예정 휴장
            if out:
                holiday_weeks += 1
            monday += timedelta(days=7)
            continue
        last = ss[-1]
        closed_at = schedule.close_at(last)
        if closed_at > as_of:                   # 진행 중인 주
            monday += timedelta(days=7)
            continue
        if ss[0] < first_bar:                   # 상장 전 주
            monday += timedelta(days=7)
            continue
        gap, holiday_weeks = holiday_weeks, 0
        got = [by_date.get(s) for s in ss]
        # ── 사용 가능 시각 ──
        reason = ""
        if mode == ASSUMED_DELAY:
            available_at, basis = closed_at + data_delay, ASSUMED_DELAY
        else:
            basis = OBSERVED
            missing = [s for s in ss if s not in ready]
            early = [s for s in ss if s in ready and ready[s] < schedule.close_at(s)]
            if missing:
                available_at, reason = None, f"READY_TIME_UNKNOWN:{missing[0].isoformat()}"
            elif early:
                available_at, reason = None, f"READY_BEFORE_SESSION_CLOSE:{early[0].isoformat()}"
            else:
                available_at = max([closed_at] + [ready[s] for s in ss])
        if reason:
            out.append(_placeholder(monday, last, closed_at, available_at, basis, reason, gap))
        elif available_at > as_of:
            out.append(_placeholder(monday, last, closed_at, available_at, basis, "DATA_NOT_READY", gap))
        elif any(b is None for b in got):
            out.append(_placeholder(monday, last, closed_at, available_at, basis,
                                    f"MISSING_DAILY:{sum(b is None for b in got)}", gap))
        else:
            tv = None if any(b.trade_value is None for b in got) else sum(b.trade_value for b in got)
            out.append(WeeklyBar(monday, last, closed_at, available_at, basis, got[0].open,
                                 max(b.high for b in got), min(b.low for b in got), got[-1].close,
                                 sum(b.volume for b in got), tv, True, "", gap,
                                 sum(1 for b in got if b.no_trades)))
        monday += timedelta(days=7)
    if out and out[-1].reason == "DATA_NOT_READY":
        # 가장 최근에 끝난 주 하나만, **정상 대기 기간**(다음 거래일이 시작되기 전)이면 '아직 도착 전'으로 잘라냄.
        # 다음 거래일이 시작됐는데도 미확보면 장애 지연 → 자리를 남겨 추세 UNKNOWN (Q-R2).
        # 다음 거래일을 모르면(일정 불명) 대기 기간을 판단할 수 없으므로 자리를 남김(fail-closed).
        nxt = _next_session(schedule, out[-1].week_end_session)
        if nxt is not None and as_of < datetime.combine(nxt, time(0, 0)):
            out.pop()
        else:
            out[-1] = _placeholder(out[-1].week_start, out[-1].week_end_session, out[-1].session_closed_at,
                                   out[-1].available_at, out[-1].availability_basis,
                                   f"DATA_NOT_READY:OVERDUE(next_session={nxt})", out[-1].gap_weeks_before)
    return out


def _next_session(schedule: WeekSchedule, last: date, max_weeks: int = 4) -> date | None:
    """last 다음 거래일 (일정 불명이면 None)."""
    monday = _monday(last) + timedelta(days=7)
    for _ in range(max_weeks):
        ss = schedule.sessions_in_week(monday)
        if ss is None:
            return None
        if ss:
            return ss[0]
        monday += timedelta(days=7)
    return None


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
    # A13-Q3: 주 시작일 간격은 7일 × (1 + 사이의 한 주 전체 휴장 수)여야 함 — 빠진 주가 있으면 압축된 창
    for prev, cur in zip(last34, last34[1:]):
        if (cur.week_start - prev.week_start).days != 7 * (1 + cur.gap_weeks_before):
            return WeeklyTrend(UNKNOWN, end, avail, None, None, None,
                               f"WEEK_SEQUENCE_GAP:{prev.week_start.isoformat()}→{cur.week_start.isoformat()}")
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
