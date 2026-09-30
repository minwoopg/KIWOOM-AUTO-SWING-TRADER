from __future__ import annotations

"""연구용 일봉과 기준일 창(SeriesView).

- `ResearchBar`: 날짜·OHLC(실수 — 지수는 소수)·거래량·실제 거래대금(원, 없으면 None).
  운영용 `DailyBar`(정수 가격, 거래대금 없음)와 따로 둡니다. 종가 × 거래량을 거래대금으로
  채우지 않습니다.
- `SeriesView(bars, sessions, t)`: 기준일 t까지만 봅니다.
  * t 뒤의 봉·세션은 만들 때 잘라냅니다(미래 데이터 차단).
  * 창(window)은 **거래 세션 기준**입니다. 세션 목록(달력)에 있는데 봉이 없으면
    상장 전(INSUFFICIENT_HISTORY)인지 중간 공백(DATA_GAP — 거래정지·수집 누락)인지
    구분해 UNKNOWN을 돌려줍니다. 앞뒤 봉을 당겨 채우지 않습니다.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Sequence

from domain.research.types import FV


class ResearchBarError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_value: int | None = None   # 실제 거래대금(원). 원천에 없으면 None

    def __post_init__(self) -> None:
        # A13-R5: 날짜 타입, 유한한 양수 가격, bool이 아닌 정수 수량만 허용
        if not isinstance(self.date, date) or isinstance(self.date, datetime):
            raise ResearchBarError(f"date는 date 타입이어야 함 — {self.date!r}")
        for name in ("open", "high", "low", "close"):
            v = getattr(self, name)
            if (not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not math.isfinite(v) or not v > 0):
                raise ResearchBarError(f"{self.date}: {name}는 유한한 양수여야 함 — {v!r}")
        if not isinstance(self.volume, int) or isinstance(self.volume, bool) or self.volume < 0:
            raise ResearchBarError(f"{self.date}: volume은 0 이상 정수 — {self.volume!r}")
        if self.trade_value is not None and (not isinstance(self.trade_value, int)
                                             or isinstance(self.trade_value, bool) or self.trade_value < 0):
            raise ResearchBarError(f"{self.date}: trade_value는 0 이상 정수(원) 또는 None — {self.trade_value!r}")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ResearchBarError(f"{self.date}: 가격 관계 모순")

    @property
    def range(self) -> float:
        return self.high - self.low


def bars_from_daily(daily_bars: Iterable, trade_values: dict[date, int] | None = None) -> list[ResearchBar]:
    """운영용 DailyBar → ResearchBar. 거래대금은 따로 확보한 값만 붙입니다(없으면 None)."""
    tv = trade_values or {}
    return [ResearchBar(b.date, float(b.open), float(b.high), float(b.low), float(b.close), int(b.volume),
                        tv.get(b.date)) for b in daily_bars]


def validate_sessions(sessions: Sequence[date], what: str = "sessions") -> None:
    """A13-R3: 세션 날짜는 date 타입·엄격한 오름차순(중복 없음). 조용히 고치지 않고 오류."""
    for d in sessions:
        if not isinstance(d, date) or isinstance(d, datetime):
            raise ResearchBarError(f"{what}: date 타입이 아님 — {d!r}")
    for a, b in zip(sessions, list(sessions)[1:]):
        if b <= a:
            raise ResearchBarError(f"{what}: 오름차순·중복 없음이 아님 ({a} → {b})")


class SeriesView:
    """기준일 t에서 본 한 종목(또는 지수)의 일봉."""

    def __init__(self, bars: Sequence[ResearchBar], sessions: Sequence[date], t: date) -> None:
        dates = [b.date for b in bars]
        if any(b <= a for a, b in zip(dates, dates[1:])):
            raise ResearchBarError("일봉 날짜가 오름차순·중복 없음이 아님")
        validate_sessions(sessions)
        if not isinstance(t, date) or isinstance(t, datetime):
            raise ResearchBarError(f"기준일 t는 date 타입이어야 함 — {t!r}")
        self.t = t
        self.sessions = [s for s in sessions if s <= t]          # 미래 세션 차단
        self.by_date = {b.date: b for b in bars if b.date <= t}  # 미래 봉 차단
        self.first_bar_date = min(self.by_date) if self.by_date else None
        self._idx = {s: i for i, s in enumerate(self.sessions)}
        self.t_index = self._idx.get(t)

    # ── 상태 ──
    def status(self) -> str:
        """기준일 자체의 사용 가능 여부. 빈 문자열이면 정상."""
        if self.t_index is None:
            return "T_NOT_SESSION"
        if self.t not in self.by_date:
            return "NO_BAR_AT_T"
        return ""

    def session_at(self, offset: int) -> date | None:
        """t에서 offset 세션 전의 날짜 (offset=0 → t)."""
        if self.t_index is None or offset < 0 or self.t_index - offset < 0:
            return None
        return self.sessions[self.t_index - offset]

    def bar_at(self, offset: int) -> ResearchBar | None:
        d = self.session_at(offset)
        return self.by_date.get(d) if d else None

    def window(self, n: int, end_offset: int = 0) -> tuple[list[ResearchBar] | None, str]:
        """t-end_offset을 끝으로 하는 연속 n개 세션의 봉. (봉 목록, "") 또는 (None, 사유)."""
        st = self.status()
        if st:
            return None, st
        if not isinstance(n, int) or not isinstance(end_offset, int) or n <= 0 or end_offset < 0:
            return None, "BAD_WINDOW"
        end = self.t_index - end_offset
        start = end - n + 1
        if start < 0:
            return None, "INSUFFICIENT_SESSIONS"   # 세션 목록(달력) 자체가 짧음
        out = []
        for d in self.sessions[start:end + 1]:
            b = self.by_date.get(d)
            if b is None:
                if self.first_bar_date is None or d < self.first_bar_date:
                    return None, "INSUFFICIENT_HISTORY"
                return None, f"DATA_GAP:{d.isoformat()}"
            out.append(b)
        if len(out) != n:                       # 방어: 세션 검증이 있으므로 도달하지 않음
            return None, "WINDOW_LENGTH_MISMATCH"
        return out, ""

    def fv_window(self, n: int, end_offset: int = 0):
        bars, why = self.window(n, end_offset)
        return bars, (FV.unknown(why) if bars is None else None)

    def contiguous_run_start(self) -> date | None:
        """t에서 거슬러 올라가며 공백 없이 이어진 구간의 첫 세션 날짜."""
        if self.status():
            return None
        i = self.t_index
        while i - 1 >= 0 and self.sessions[i - 1] in self.by_date:
            i -= 1
        return self.sessions[i]
