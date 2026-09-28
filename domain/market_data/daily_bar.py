from __future__ import annotations

"""일봉 모델과 엄격 파싱 (스윙 분리 5라운드, 2026-09-28).

단타 `KiwoomBroker.get_daily_prices()`는 파싱할 수 없는 값을 0으로 채우고
(`parse_abs_int`), 날짜를 문자열로 둡니다. 스윙 판단의 원재료이므로 여기서는
**이상한 행을 조용히 넘기지 않습니다** — 하나라도 이상하면 `BarValidationError`.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from domain.models import PriceBar


class BarValidationError(ValueError):
    """일봉 행이 유효하지 않음 (파싱 실패·가격 관계 모순·중복 날짜 등)."""


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: int
    high: int
    low: int
    close: int
    volume: int

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close"):
            v = getattr(self, name)
            if type(v) is not int or v <= 0:
                raise BarValidationError(f"{self.date}: {name}는 양의 정수여야 함 — {v!r}")
        if type(self.volume) is not int or self.volume < 0:
            raise BarValidationError(f"{self.date}: volume은 0 이상 정수 — {self.volume!r}")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise BarValidationError(
                f"{self.date}: 가격 관계 모순 (시 {self.open}, 고 {self.high}, 저 {self.low}, 종 {self.close})")

    def to_price_bar(self) -> PriceBar:
        """기존 모델(`domain.models.PriceBar`, 날짜 YYYYMMDD 문자열)로 변환."""
        return PriceBar(self.date.strftime("%Y%m%d"), self.open, self.high, self.low,
                        self.close, self.volume)


def _strict_abs_int(value: Any, field: str, row_date: str) -> int:
    text = str(value if value is not None else "").strip().replace(",", "")
    if text.startswith(("+", "-")):
        text = text[1:]
    if not text.isdigit():
        raise BarValidationError(f"{row_date}: {field} 숫자 아님 — {value!r}")
    return int(text)


def parse_kiwoom_daily_row(row: dict) -> DailyBar:
    """ka10081 `stk_dt_pole_chart_qry` 행 하나 → DailyBar.

    키움은 등락 부호를 가격 앞에 붙이므로(+/-) 절대값으로 읽습니다.
    """
    if not isinstance(row, dict):
        raise BarValidationError(f"행이 dict가 아님: {type(row).__name__}")
    raw_date = str(row.get("dt", "")).strip()
    if len(raw_date) != 8 or not raw_date.isdigit():
        raise BarValidationError(f"dt 형식 오류 — {raw_date!r}")
    try:
        d = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
    except ValueError as exc:
        raise BarValidationError(f"dt 날짜 아님 — {raw_date!r}") from exc
    return DailyBar(
        date=d,
        open=_strict_abs_int(row.get("open_pric"), "open_pric", raw_date),
        high=_strict_abs_int(row.get("high_pric"), "high_pric", raw_date),
        low=_strict_abs_int(row.get("low_pric"), "low_pric", raw_date),
        close=_strict_abs_int(row.get("cur_prc"), "cur_prc", raw_date),
        volume=_strict_abs_int(row.get("trde_qty"), "trde_qty", raw_date),
    )


def check_series(bars: Iterable[DailyBar]) -> list[DailyBar]:
    """날짜 오름차순·중복 없음을 확인해 리스트로 반환."""
    out = list(bars)
    for a, b in zip(out, out[1:]):
        if b.date <= a.date:
            raise BarValidationError(f"날짜 순서/중복 오류: {a.date} → {b.date}")
    return out
