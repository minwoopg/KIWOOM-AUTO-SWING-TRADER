from __future__ import annotations

"""키움 일봉 행 해석 — 종목(ka10081)·지수(ka20006) (A2, 조회 전용·순수 함수).

단위 (키움 공식 명세·A1 실측 2026-09-30)
- 종목 가격: 원(정수). 키움은 등락 부호(+/-)를 앞에 붙이므로 절대값으로 읽습니다.
- 지수 가격: **소수점을 뺀 100배 정수** → 연구용 값은 원천 ÷ 100 (KOSPI 685362 → 6853.62).
- 거래대금(trde_prica): **백만원** → 원으로 환산(× 1,000,000). 정밀도는 백만원 단위이며 원천의
  반올림 방식은 확인되지 않았습니다(오차 범위를 단정하지 않음).
- 지수 거래량 단위는 확인 전(UNVERIFIED) — S1은 지수 거래량을 쓰지 않습니다.

원본 보존: 저장소에는 원천 정수(부호만 뗀 값)를 그대로 두고, 배율·단위 환산은 읽을 때 합니다.

품질 표시(quality)
- ""          : 정상
- NO_TRADES   : 거래량 0 (A2 보완 1). 예: 삼성전자 2018-04-30·05-02·05-03 — OHLC 53,000, 거래량·거래대금 0.
                거래정지로 단정하지 않고 표시만 합니다. 계산 정책은 `domain/research/series.py` NO_TRADES_POLICY.
- INVALID:<사유> : 숫자가 아님·0 이하 가격·고저 모순 등. 연구 봉으로 바꾸지 않고(→ 그 날은 DATA_GAP),
                원래 행은 저장소 bar_invalid_raw에 JSON 그대로 남깁니다.
날짜(dt)를 읽을 수 없는 행은 어느 날짜인지 모르므로 RowError — 그 종목 수집을 실패로 처리합니다.
"""

from dataclasses import dataclass
from datetime import date

from domain.research.series import ResearchBar

NO_TRADES = "NO_TRADES"
INVALID = "INVALID"


class RowError(ValueError):
    """날짜를 알 수 없는 행 — 저장할 자리를 정할 수 없음."""


@dataclass(frozen=True)
class SourceSpec:
    kind: str                 # STOCK / INDEX
    api_id: str
    list_key: str
    price_scale: int          # 원천 정수 ÷ price_scale = 연구용 가격
    trade_value_unit: int     # 원천 거래대금 × trade_value_unit = 원
    volume_unit: str


STOCK_DAILY = SourceSpec("STOCK", "ka10081", "stk_dt_pole_chart_qry", 1, 1_000_000, "SHARES")
INDEX_DAILY = SourceSpec("INDEX", "ka20006", "inds_dt_pole_qry", 100, 1_000_000, "UNVERIFIED")

PRICE_FIELDS = (("open_raw", "open_pric"), ("high_raw", "high_pric"), ("low_raw", "low_pric"), ("close_raw", "cur_prc"))


@dataclass(frozen=True)
class RawBar:
    date: date
    open_raw: int | None
    high_raw: int | None
    low_raw: int | None
    close_raw: int | None
    volume: int | None
    trade_value_raw: int | None
    quality: str = ""

    @property
    def valid(self) -> bool:
        return not self.quality.startswith(INVALID)

    def values(self) -> tuple:
        """비교용 원천 값 (수정주가 기준 변화 감지)."""
        return (self.open_raw, self.high_raw, self.low_raw, self.close_raw, self.volume, self.trade_value_raw)


def _abs_int(v) -> int | None:
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s.startswith(("+", "-")):
        s = s[1:]
    if not s.isdigit():
        return None
    return int(s)


def parse_date(v) -> date:
    s = str(v or "").strip()
    if len(s) != 8 or not s.isdigit():
        raise RowError(f"dt 형식 오류 — {v!r}")
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    except ValueError as exc:
        raise RowError(f"dt 날짜 아님 — {v!r}") from exc


def parse_row(row: dict) -> RawBar:
    if not isinstance(row, dict):
        raise RowError(f"행이 dict가 아님 — {type(row).__name__}")
    d = parse_date(row.get("dt"))
    vals = {name: _abs_int(row.get(key)) for name, key in PRICE_FIELDS}
    vol = _abs_int(row.get("trde_qty"))
    tv_present = row.get("trde_prica") not in (None, "")
    tv = _abs_int(row.get("trde_prica")) if tv_present else None
    problems = [key for name, key in PRICE_FIELDS if vals[name] is None]
    if vol is None:
        problems.append("trde_qty")
    if tv_present and tv is None:
        problems.append("trde_prica")
    if problems:
        quality = f"{INVALID}:FIELD:{','.join(problems)}"
    elif min(vals.values()) <= 0:
        quality = f"{INVALID}:NONPOSITIVE_PRICE"
    elif (vals["high_raw"] < max(vals["open_raw"], vals["close_raw"], vals["low_raw"])
          or vals["low_raw"] > min(vals["open_raw"], vals["close_raw"], vals["high_raw"])):
        quality = f"{INVALID}:PRICE_RELATION"
    elif vol == 0:
        quality = NO_TRADES
    else:
        quality = ""
    return RawBar(d, vals["open_raw"], vals["high_raw"], vals["low_raw"], vals["close_raw"], vol, tv, quality)


def to_research_bar(b: RawBar, spec: SourceSpec) -> ResearchBar | None:
    """저장 원천 → 연구 봉. INVALID는 None(그 날은 DATA_GAP으로 보임)."""
    if not b.valid:
        return None
    s = spec.price_scale
    tv = None if b.trade_value_raw is None else b.trade_value_raw * spec.trade_value_unit
    return ResearchBar(b.date, b.open_raw / s, b.high_raw / s, b.low_raw / s, b.close_raw / s, int(b.volume), tv)
