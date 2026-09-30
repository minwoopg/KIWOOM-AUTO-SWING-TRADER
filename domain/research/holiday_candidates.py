from __future__ import annotations

"""과거 휴장일 후보 — 지수 일봉 날짜에서 뽑아 사람이 확인하도록 (A2, 순수 함수).

합의(docs/research_a_stage.md): 과거 휴장일은 지수 날짜에서 뽑아 공휴일·근로자의 날·연말 휴장·
거래소 지정 휴장·선거일 등과 대조한 뒤 **사람이 확인해** `config/krx_calendar.yaml`에 추가합니다.
이 모듈은 후보와 추정 이름만 만들고 달력 파일을 고치지 않습니다.

- 평일인데 지수 봉이 없는 날 → 후보. 날짜가 고정된 공휴일은 이름을 붙이고, 나머지(설·추석·부처님오신날·
  대체공휴일·선거일·임시공휴일 등 음력·가변 휴일)는 "확인 필요".
- 12/31이 주말이면 그해 마지막 평일을 연말 휴장 추정으로 표시.
- 고정 공휴일인데 봉이 있으면 따로 표시(휴장 규칙 변경·자료 오류 확인용). 주말 봉도 따로 표시.
- 두 지수(KOSPI·KOSDAQ)의 날짜 집합이 다르면 차이를 보고.
- 개장·마감 시각이 달랐던 특수 운영일(연초 지연 개장·수능일)은 일봉 날짜로 알 수 없으므로 목록에 없음.
"""

from datetime import date, timedelta

FIXED_HOLIDAYS = {
    (1, 1): "신정", (3, 1): "삼일절", (5, 1): "근로자의 날", (5, 5): "어린이날", (6, 6): "현충일",
    (8, 15): "광복절", (10, 3): "개천절", (10, 9): "한글날", (12, 25): "성탄절", (12, 31): "연말 휴장",
}
UNKNOWN_NAME = "확인 필요"


def _year_end_weekday(y: int) -> date:
    d = date(y, 12, 31)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def holiday_candidates(bar_dates, start: date, end: date, other_index_dates=None) -> dict:
    bars = {d for d in bar_dates if start <= d <= end}
    cands, fixed_traded, weekend_bars = [], [], sorted(d for d in bars if d.weekday() >= 5)
    d = start
    while d <= end:
        if d.weekday() < 5:
            name = FIXED_HOLIDAYS.get((d.month, d.day))
            if d == _year_end_weekday(d.year) and d.day != 31:
                name = "연말 휴장(12/31 주말 — 마지막 평일 추정)"
            if d not in bars:
                cands.append({"date": d.isoformat(), "weekday": "월화수목금"[d.weekday()], "guess": name or UNKNOWN_NAME})
            elif name:
                fixed_traded.append({"date": d.isoformat(), "fixed": name})
        d += timedelta(days=1)
    by_year: dict[int, int] = {}
    for c in cands:
        y = int(c["date"][:4])
        by_year[y] = by_year.get(y, 0) + 1
    out = {"range": [start.isoformat(), end.isoformat()], "sessions": len(bars), "candidates": cands,
           "by_year": by_year, "fixed_holiday_but_traded": fixed_traded,
           "weekend_bars": [x.isoformat() for x in weekend_bars]}
    if other_index_dates is not None:
        other = {x for x in other_index_dates if start <= x <= end}
        out["index_date_mismatch"] = {"only_first": sorted(x.isoformat() for x in bars - other),
                                      "only_second": sorted(x.isoformat() for x in other - bars)}
    return out


def to_yaml_snippet(result: dict) -> str:
    """사람 확인용 초안. 이름이 '확인 필요'인 줄은 공지·기사로 확인 후 고쳐 넣으세요."""
    years = sorted(result["by_year"])
    lines = ["# 과거 휴장일 후보 — 지수 일봉 날짜에서 추출 (자동 생성, 확인 전 초안)",
             "# '확인 필요'는 음력·대체·선거·임시 휴일 등: 거래소 공지로 확인한 뒤 이름을 고쳐 넣으세요.",
             "# 특수 운영일(지연 개장·수능일)은 일봉으로 알 수 없으므로 따로 추가해야 합니다.",
             f"# covered_years 추가 후보: {years}", "holidays:"]
    for c in result["candidates"]:
        lines.append(f'  "{c["date"]}": {c["guess"]}  # {c["weekday"]}')
    if result["fixed_holiday_but_traded"]:
        lines.append("# 고정 공휴일인데 봉이 있음(확인): " + ", ".join(
            f'{x["date"]} {x["fixed"]}' for x in result["fixed_holiday_but_traded"]))
    if result["weekend_bars"]:
        lines.append("# 주말 봉(자료 확인): " + ", ".join(result["weekend_bars"]))
    mm = result.get("index_date_mismatch")
    if mm and (mm["only_first"] or mm["only_second"]):
        lines.append(f"# 지수 날짜 불일치: KOSPI만 {mm['only_first']} / KOSDAQ만 {mm['only_second']}")
    return "\n".join(lines) + "\n"
