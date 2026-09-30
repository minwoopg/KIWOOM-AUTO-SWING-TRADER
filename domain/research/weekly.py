from __future__ import annotations

"""완성 주봉과 주간 추세 근사 (보충안 5절, weekly_version = w1).

- 주 = Asia/Seoul 기준 월~일. 한 주의 **예정된 마지막 세션**은 거래일 달력(휴장일은
  미리 공지되므로 기준일에 알 수 있는 정보)으로 정합니다.
- 기준일 t에서 완성된 주: 그 주의 마지막 세션이 t 이하이고, 그 주의 모든 세션 봉이 있음.
  진행 중인 주는 넣지 않습니다. 봉이 빠진 주는 불완전 주 — 건너뛰어 압축하지 않습니다.
- `schedule_known_through`: 달력이 세션을 확정해 둔 마지막 날짜. 그 뒤로 넘어가는 주는
  마지막 세션을 알 수 없으므로 완성으로 보지 않습니다.
- 주봉: O=첫 세션 시가, H=최고, L=최저, C=마지막 세션 종가, V·거래대금=합계(거래대금은 하나라도
  없으면 None).
- SMA30W = 최근 30개 완성 주봉 종가 평균, slope4W = SMA30W[w]/SMA30W[w-4]-1 (34주 연속 필요).
- 상태: UP_PROXY(C>SMA30W, slope>0) / DOWN_PROXY(C<SMA30W, slope<0) / UNCLASSIFIED / UNKNOWN.
  Stage 1~4 분류가 아닙니다(근사).
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from domain.research.series import ResearchBar

WEEKLY_VERSION = "w1"
UP_PROXY, DOWN_PROXY, UNCLASSIFIED, UNKNOWN = "UP_PROXY", "DOWN_PROXY", "UNCLASSIFIED", "UNKNOWN"


@dataclass(frozen=True)
class WeeklyBar:
    week_start: date            # 월요일
    week_end_session: date      # 그 주 마지막 세션
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_value: int | None
    complete: bool
    reason: str = ""


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekly_bars(bars: Sequence[ResearchBar], sessions: Sequence[date], t: date,
                schedule_known_through: date) -> list[WeeklyBar]:
    """t 시점에 판단한 주봉 목록(오래된 순). 진행 중인 주는 제외, 불완전 주는 complete=False로 포함."""
    by_date = {b.date: b for b in bars if b.date <= t}
    weeks: dict[date, list[date]] = {}
    for s in sessions:
        weeks.setdefault(_monday(s), []).append(s)
    out: list[WeeklyBar] = []
    for monday in sorted(weeks):
        ss = sorted(weeks[monday])
        sunday = monday + timedelta(days=6)
        if sunday > schedule_known_through:
            continue                      # 그 주 마지막 세션을 확정할 수 없음
        last = ss[-1]
        if last > t:
            continue                      # 진행 중(또는 미래) 주
        got = [by_date.get(s) for s in ss]
        if any(b is None for b in got):
            first_bar = min(by_date) if by_date else None
            if first_bar is None or ss[0] < first_bar:
                continue                  # 상장 전 주는 목록에서 뺌
            out.append(WeeklyBar(monday, last, 0.0, 0.0, 0.0, 0.0, 0, None, False,
                                 f"MISSING_DAILY:{sum(b is None for b in got)}"))
            continue
        tv = None if any(b.trade_value is None for b in got) else sum(b.trade_value for b in got)
        out.append(WeeklyBar(monday, last, got[0].open, max(b.high for b in got), min(b.low for b in got),
                             got[-1].close, sum(b.volume for b in got), tv, True))
    return out


@dataclass(frozen=True)
class WeeklyTrend:
    state: str
    last_week_end: date | None
    close: float | None
    sma30w: float | None
    slope4w: float | None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "last_week_end": self.last_week_end.isoformat() if self.last_week_end else None,
                "close": self.close, "sma30w": self.sma30w, "slope4w": self.slope4w, "reason": self.reason}


def weekly_trend(weeks: Sequence[WeeklyBar]) -> WeeklyTrend:
    if len(weeks) < 34:
        return WeeklyTrend(UNKNOWN, weeks[-1].week_end_session if weeks else None, None, None, None,
                           f"INSUFFICIENT_WEEKS:{len(weeks)}/34")
    last34 = list(weeks[-34:])
    bad = [w for w in last34 if not w.complete]
    if bad:
        return WeeklyTrend(UNKNOWN, last34[-1].week_end_session, None, None, None,
                           f"INCOMPLETE_WEEK:{bad[-1].week_start.isoformat()}")
    closes = [w.close for w in last34]
    sma_now = sum(closes[-30:]) / 30
    sma_prev = sum(closes[-34:-4]) / 30
    slope = sma_now / sma_prev - 1
    c = closes[-1]
    if c > sma_now and slope > 0:
        state = UP_PROXY
    elif c < sma_now and slope < 0:
        state = DOWN_PROXY
    else:
        state = UNCLASSIFIED
    return WeeklyTrend(state, last34[-1].week_end_session, c, sma_now, slope)
