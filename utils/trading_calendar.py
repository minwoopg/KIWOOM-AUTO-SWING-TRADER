from __future__ import annotations

"""KRX 거래일 캘린더와 장 단계 판단 (스윙 분리 3라운드, 2026-09-28).

단타 레포의 `utils/time_utils.py`는 "평일 09:00~15:20"만 보고 휴장일을 모릅니다.
스윙은 "N거래일 보유", "직전 거래일의 완성 봉" 같은 계산이 필요하므로
휴장일을 아는 캘린더를 새로 만듭니다. `time_utils.py`는 원본 그대로 둡니다.

원칙
- **fail-closed**: 파일이 없거나 형식이 틀리면 예외. `covered_years` 밖의 날짜를
  물어도 예외(`CalendarCoverageError`) — 모르는 해의 휴장일을 "평일이니 개장"으로
  추측하지 않습니다.
- 장 단계는 KRX 정규장 기준입니다(NXT 시간외 시장은 다루지 않음 — 브로커가
  `dmst_stex_tp=KRX`로 주문하므로).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterable

import yaml

from utils.time_utils import KST_TZ

DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parent.parent / "config" / "krx_calendar.yaml"


class CalendarConfigError(RuntimeError):
    """캘린더 파일이 없거나 형식이 잘못됨."""


class CalendarCoverageError(RuntimeError):
    """캘린더가 다루지 않는 연도의 날짜를 물음 — 휴장일 목록을 추가해야 함."""


class MarketPhase(str, Enum):
    CLOSED_DAY = "CLOSED_DAY"            # 주말·휴장일
    PRE_OPEN = "PRE_OPEN"                # 거래일, 정규장 시작 전
    REGULAR = "REGULAR"                  # 정규장 (접속매매)
    CLOSING_AUCTION = "CLOSING_AUCTION"  # 종가 단일가 (기본 15:20~15:30)
    POST_CLOSE = "POST_CLOSE"            # 거래일, 정규장 종료 후


@dataclass(frozen=True)
class SessionTimes:
    open: time
    close: time
    closing_auction_start: time
    note: str = ""


def _parse_hhmm(value, field: str) -> time:
    try:
        hh, mm = str(value).split(":")
        return time(int(hh), int(mm))
    except Exception as exc:
        raise CalendarConfigError(f"{field}: HH:MM 형식이 아님 — {value!r}") from exc


def _parse_date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except Exception as exc:
        raise CalendarConfigError(f"{field}: YYYY-MM-DD 형식이 아님 — {value!r}") from exc


def _minus_minutes(t: time, minutes: int) -> time:
    return (datetime.combine(date(2000, 1, 1), t) - timedelta(minutes=minutes)).time()


class TradingCalendar:
    def __init__(
        self,
        *,
        covered_years: Iterable[int],
        holidays: dict[date, str],
        regular: SessionTimes,
        special_sessions: dict[date, SessionTimes] | None = None,
    ) -> None:
        self.covered_years = frozenset(int(y) for y in covered_years)
        if not self.covered_years:
            raise CalendarConfigError("covered_years가 비어 있음")
        self.holidays = dict(holidays)
        self.regular = regular
        self.special_sessions = dict(special_sessions or {})
        for d in list(self.holidays) + list(self.special_sessions):
            if d.year not in self.covered_years:
                raise CalendarConfigError(f"{d}: covered_years{sorted(self.covered_years)} 밖의 날짜")
            if d.weekday() >= 5:
                raise CalendarConfigError(f"{d}: 주말은 자동 휴장이므로 목록에 넣지 않습니다")
        overlap = set(self.holidays) & set(self.special_sessions)
        if overlap:
            raise CalendarConfigError(f"휴장일과 특수 운영일이 겹침: {sorted(overlap)}")

    # ── 로딩 ──────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CALENDAR_PATH) -> "TradingCalendar":
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CalendarConfigError(f"캘린더 파일 없음: {path}") from exc
        except yaml.YAMLError as exc:
            raise CalendarConfigError(f"캘린더 YAML 파싱 실패: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise CalendarConfigError(f"캘린더 최상위가 dict가 아님: {path}")
        try:
            covered = [int(y) for y in raw["covered_years"]]
            reg = raw["regular_session"]
            auction_min = int(reg.get("closing_auction_minutes", 10))
            regular = SessionTimes(
                open=_parse_hhmm(reg["open"], "regular_session.open"),
                close=_parse_hhmm(reg["close"], "regular_session.close"),
                closing_auction_start=_minus_minutes(
                    _parse_hhmm(reg["close"], "regular_session.close"), auction_min),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CalendarConfigError(f"필수 항목 누락/형식 오류: {exc}") from exc
        holidays_raw = raw.get("holidays") or {}
        if not isinstance(holidays_raw, dict):
            raise CalendarConfigError("holidays는 {날짜: 이름} 형식이어야 함")
        holidays = {_parse_date(k, "holidays"): str(v) for k, v in holidays_raw.items()}
        specials: dict[date, SessionTimes] = {}
        for k, v in (raw.get("special_sessions") or {}).items():
            d = _parse_date(k, "special_sessions")
            if not isinstance(v, dict):
                raise CalendarConfigError(f"special_sessions.{k}: dict가 아님")
            close = _parse_hhmm(v.get("close", reg["close"]), f"special_sessions.{k}.close")
            specials[d] = SessionTimes(
                open=_parse_hhmm(v.get("open", reg["open"]), f"special_sessions.{k}.open"),
                close=close,
                closing_auction_start=_minus_minutes(
                    close, int(v.get("closing_auction_minutes", auction_min))),
                note=str(v.get("note", "")),
            )
        return cls(covered_years=covered, holidays=holidays, regular=regular,
                   special_sessions=specials)

    # ── 날짜 판단 ────────────────────────────────────────────

    def _check_covered(self, d: date) -> None:
        if d.year not in self.covered_years:
            raise CalendarCoverageError(
                f"{d}: 캘린더가 {sorted(self.covered_years)}년만 다룹니다 — "
                f"config/krx_calendar.yaml에 {d.year}년 휴장일을 추가하세요"
            )

    def is_trading_day(self, d: date) -> bool:
        self._check_covered(d)
        return d.weekday() < 5 and d not in self.holidays

    def holiday_name(self, d: date) -> str | None:
        self._check_covered(d)
        if d.weekday() >= 5:
            return "주말"
        return self.holidays.get(d)

    def next_trading_day(self, d: date) -> date:
        """d 다음(당일 제외) 첫 거래일."""
        cur = d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def previous_trading_day(self, d: date) -> date:
        """d 이전(당일 제외) 마지막 거래일."""
        cur = d - timedelta(days=1)
        while not self.is_trading_day(cur):
            cur -= timedelta(days=1)
        return cur

    def add_trading_days(self, d: date, n: int) -> date:
        """d에서 n거래일 뒤(음수면 앞). n=0이면 d 자체(거래일이어야 함)."""
        if n == 0:
            if not self.is_trading_day(d):
                raise ValueError(f"{d}는 거래일이 아님")
            return d
        cur = d
        step = self.next_trading_day if n > 0 else self.previous_trading_day
        for _ in range(abs(n)):
            cur = step(cur)
        return cur

    def trading_days_between(self, start: date, end: date) -> int:
        """start 초과 ~ end 이하 거래일 수 (보유 거래일수 계산용).

        예: 월요일 매수, 수요일 기준 → 2 (화·수). start > end면 음수.
        """
        if start == end:
            return 0
        sign = 1
        if start > end:
            start, end, sign = end, start, -1
        count = 0
        cur = start
        while cur < end:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                count += 1
        return sign * count

    def trading_days_in_range(self, start: date, end: date) -> list[date]:
        """start 이상 end 이하의 거래일 목록."""
        out = []
        cur = start
        while cur <= end:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    # ── 장 시간 / 단계 ──────────────────────────────────────

    def session_times(self, d: date) -> SessionTimes | None:
        """거래일이면 그날 정규장 시간, 휴장일이면 None."""
        if not self.is_trading_day(d):
            return None
        return self.special_sessions.get(d, self.regular)

    def phase(self, now: datetime) -> MarketPhase:
        """now(naive면 KST로 간주)의 장 단계."""
        if now.tzinfo is not None:
            now = now.astimezone(KST_TZ).replace(tzinfo=None)
        session = self.session_times(now.date())
        if session is None:
            return MarketPhase.CLOSED_DAY
        t = now.time()
        if t < session.open:
            return MarketPhase.PRE_OPEN
        if t < session.closing_auction_start:
            return MarketPhase.REGULAR
        if t < session.close:
            return MarketPhase.CLOSING_AUCTION
        return MarketPhase.POST_CLOSE

    def last_completed_session(self, now: datetime) -> date:
        """now 시점에 **정규장이 끝난** 가장 최근 거래일.

        일봉 판정의 기준일입니다: 장중(PRE_OPEN~CLOSING_AUCTION)이나 휴장일이면
        직전 거래일, 장 마감 후(POST_CLOSE)면 당일.
        """
        if now.tzinfo is not None:
            now = now.astimezone(KST_TZ).replace(tzinfo=None)
        if self.phase(now) == MarketPhase.POST_CLOSE:
            return now.date()
        return self.previous_trading_day(now.date())

    # ── 검증 ────────────────────────────────────────────────

    def compare_with_bar_dates(self, bar_dates: Iterable[date], start: date, end: date) -> dict:
        """실제 일봉이 있는 날짜 목록과 캘린더를 대조합니다 (프로브·점검용).

        반환: {"missing_bars": 캘린더상 거래일인데 봉이 없는 날,
               "unexpected_bars": 캘린더상 휴장인데 봉이 있는 날}
        둘 다 비어 있어야 캘린더가 그 기간에 맞는 것입니다.
        """
        bars = {d for d in bar_dates if start <= d <= end}
        expected = set(self.trading_days_in_range(start, end))
        return {
            "missing_bars": sorted(expected - bars),
            "unexpected_bars": sorted(bars - expected),
        }
