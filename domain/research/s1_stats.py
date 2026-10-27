from __future__ import annotations

"""S1 빈도 분석 보조 집계 (E3 — GPT 10/22): 월별 표본, 기간 비교, 눌림 실패 세부 사유, 가장 긴 무신호 기간, 신호 묶음.

입력은 `s1_frequency.analyze`의 rows(종목·날짜 한 줄). 순수 계산.

용어(보고서에 그대로 씀):
- **종목·날짜 신호 수**: BASE 최종 PASS인 (종목, 날짜) 개수.
- **7일 묶음(탐색용)**: 같은 종목의 PASS가 7일(달력) 안에 이어지면 하나로 셈. 진입 수가 아님.
- **운영 규칙 적용 진입 수**: 별도(신호 결과 도구 `s1_outcome`의 순차 계좌 결과).
"""

from collections import Counter
from datetime import date
from typing import Iterable

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"

# PULLBACK 판정의 실제 사유(domain/research/s1.py _find_pullback) → 표시 이름
PULLBACK_REASON = {
    "PULLBACK_LEN": "눌림 기간 범위 밖(2~10일)",
    "EMPTY_PULLBACK": "고점이 전날(눌림 없음)",
    "NO_LOWER_CLOSE": "고점 뒤 더 낮은 종가 없음",
    "MA60_BREAK": "눌림 중 60일선 이탈",
    "MA60_UNKNOWN": "눌림 중 60일선 판정 불가",
}


def _reason_key(detail: str) -> str:
    d = (detail or "").strip()
    for sep in (":", "("):
        d = d.split(sep)[0]
    return d or "UNKNOWN"


def pullback_reasons(rows: Iterable[dict]) -> dict[str, int]:
    """PULLBACK이 FAIL·UNKNOWN인 줄의 세부 사유. UNKNOWN(창 부족 등)은 '데이터 부족:<사유>'."""
    out: Counter = Counter()
    for r in rows:
        if r.get("data_status") != "OK":
            continue
        c = next((x for x in r.get("checks") or [] if x["name"] == "PULLBACK"), None)
        if c is None or c["result"] == PASS:
            continue
        k = _reason_key(c.get("detail", ""))
        if c["result"] == UNKNOWN and k not in PULLBACK_REASON:
            out[f"데이터 부족({k})"] += 1
        else:
            out[PULLBACK_REASON.get(k, k)] += 1
    return dict(out.most_common())


def monthly(rows: Iterable[dict]) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows:
        m = r["date"][:7]
        b = by.setdefault(m, {"month": m, "rows": 0, "ready": 0, "holds": Counter(), "pattern": 0, "final": 0,
                              "signal_days": set()})
        b["rows"] += 1
        if r.get("data_status") == "OK":
            b["ready"] += 1
        else:
            b["holds"][r.get("data_status") or "?"] += 1
        b["pattern"] += r.get("pattern_pass") == PASS
        if r.get("eligible_signal") == PASS:
            b["final"] += 1
            b["signal_days"].add(r["date"])
    return [{**v, "holds": dict(v["holds"]), "signal_days": len(v["signal_days"])} for _, v in sorted(by.items())]


def longest_gap(dates: list[date], rows: Iterable[dict]) -> dict:
    """신호(BASE PASS)가 하나도 없는 가장 긴 연속 거래일 구간. 기간 끝까지 이어지면 ongoing=True."""
    sig = {r["date"] for r in rows if r.get("eligible_signal") == PASS}
    best = cur = 0
    start = best_start = best_end = None
    for d in dates:
        if d.isoformat() in sig:
            cur, start = 0, None
            continue
        cur += 1
        start = start or d
        if cur > best:
            best, best_start, best_end = cur, start, d
    return {"days": best, "start": best_start.isoformat() if best_start else None,
            "end": best_end.isoformat() if best_end else None,
            "ongoing": bool(dates) and best_end == dates[-1]}


def episodes(rows: Iterable[dict], gap_days: int = 7) -> int:
    last: dict[str, date] = {}
    n = 0
    for r in sorted((r for r in rows if r.get("eligible_signal") == PASS), key=lambda r: (r["code"], r["date"])):
        d = date.fromisoformat(r["date"])
        if r["code"] not in last or (d - last[r["code"]]).days > gap_days:
            n += 1
        last[r["code"]] = d
    return n


def period_summary(rows: list[dict], dates: list[date]) -> dict:
    ds = {d.isoformat() for d in dates}
    rr = [r for r in rows if r["date"] in ds]
    fin = [r for r in rr if r.get("eligible_signal") == PASS]
    return {"days": len(dates), "rows": len(rr), "ready": sum(r.get("data_status") == "OK" for r in rr),
            "pattern": sum(r.get("pattern_pass") == PASS for r in rr), "final": len(fin),
            "signal_days": len({r["date"] for r in fin}), "symbols": len({r["code"] for r in fin}),
            "episodes_7d": episodes(rr), "longest_gap": longest_gap(dates, rr)}


def data_quality(bars: list, sessions: list[date]) -> dict:
    """종목 일봉 품질(G1 — GPT 10/27): 첫·마지막 봉, 그 사이 봉이 없는 세션(원인 불명 공백), 거래량 0 봉.
    sessions: 기준 거래일 목록(KOSPI 지수 일봉 날짜). 첫 봉 이전·마지막 봉 이후는 공백으로 세지 않음."""
    if not bars:
        return {"first": None, "last": None, "gaps": [], "no_trades": []}
    have = {b.date for b in bars}
    first, last = bars[0].date, bars[-1].date
    return {"first": first, "last": last,
            "gaps": [d for d in sessions if first <= d <= last and d not in have],
            "no_trades": [b.date for b in bars if not b.volume]}


def hold_counts(rows: Iterable[dict]) -> dict[str, int]:
    return dict(Counter(r.get("data_status") for r in rows if r.get("data_status") != "OK").most_common())
