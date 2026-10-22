from __future__ import annotations

"""S1 단계별 건수 (E1 — GPT 10/21 요청: 패턴 PASS / 시장 조건 때문에 제외 / 최종 PASS를 함께).

입력 한 줄 = 종목·신호일 하나의 판정(관리자 스캔 evals 행, 또는 빈도 분석의 S1Result.to_dict()에 data_status를 붙인 것).
값은 "PASS" / "FAIL" / "UNKNOWN".

두 가지 집계를 **같이** 냄 — 섞어 읽지 않게 이름을 구분:
- 독립(independent): 데이터 준비된 줄 중 각 조건 묶음을 통과한 수(다른 묶음과 무관). "시장 PASS 0"은 시장 판정 자체의 결과.
- 순차(sequential): 데이터 준비 → 패턴 → 종목 자격 → 시장 → 손절 기준 → 최종. 각 단계는 앞 단계를 통과한 줄만 셈.
  `blocked_by_market` = 패턴·자격·손절 기준은 모두 통과했는데 시장 판정 FAIL로만 빠진 수(E2).
  `market_unknown_only` = 같은 조건에서 시장 판정이 UNKNOWN(판정 불가)인 수 — 따로 셈.
"""

from typing import Iterable

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
GROUPS = ("pattern_pass", "eligibility_pass", "market_pass", "stop_valid")
GROUP_LABEL = {"pattern_pass": "패턴", "eligibility_pass": "종목 자격", "market_pass": "시장", "stop_valid": "손절 기준"}


def _ready(r: dict) -> bool:
    ds = r.get("data_status", "OK")
    return ds == "OK" or ds is None


def funnel(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    ready = [r for r in rows if _ready(r)]
    indep = {g: sum(1 for r in ready if r.get(g) == PASS) for g in GROUPS}
    seq = []
    cur = ready
    for g in GROUPS:
        cur = [r for r in cur if r.get(g) == PASS]
        seq.append((g, len(cur)))
    # E2(GPT 10/22): "시장 조건만으로 제외" = 패턴·자격·손절 기준이 모두 PASS인데 시장만 PASS가 아님.
    # 시장 FAIL(판정이 막음)과 UNKNOWN(판정 불가)을 나눔 — 손절 기준까지 실패한 줄은 세지 않음.
    pes = [r for r in ready if r.get("pattern_pass") == PASS and r.get("eligibility_pass") == PASS
           and r.get("stop_valid") == PASS]
    blocked_by_market = sum(1 for r in pes if r.get("market_pass") == FAIL)
    market_unknown_only = sum(1 for r in pes if r.get("market_pass") == UNKNOWN)
    final = sum(1 for r in rows if r.get("eligible_signal") == PASS)
    fail_checks: dict[str, int] = {}
    for r in ready:
        for c in (r.get("checks") or (r.get("result") or {}).get("checks") or []):
            if c.get("result") == FAIL:
                fail_checks[c["name"]] = fail_checks.get(c["name"], 0) + 1
    return {"analyzed": len(rows), "data_ready": len(ready), "independent": indep,
            "sequential": dict(seq), "blocked_by_market": blocked_by_market,
            "market_unknown_only": market_unknown_only, "final": final,
            "fail_checks": dict(sorted(fail_checks.items(), key=lambda kv: (-kv[1], kv[0])))}


def funnel_line(f: dict) -> str:
    """한 줄 표시 — 순차 단계 + 시장 때문에 빠진 수 + 독립 집계."""
    s, i = f["sequential"], f["independent"]
    return (f"분석 {f['analyzed']} → 데이터 준비 {f['data_ready']} → 패턴 PASS {s['pattern_pass']} → 종목 자격 "
            f"{s['eligibility_pass']} → 시장 PASS {s['market_pass']} → 손절 기준 {s['stop_valid']} → 최종 S1 PASS "
            f"{f['final']} (순차) · 시장 조건만으로 제외 {f['blocked_by_market']}"
            + (f"(판정 불가 {f['market_unknown_only']} 별도)" if f.get("market_unknown_only") else "")
            + f" · 독립: 패턴 {i['pattern_pass']}·"
            f"자격 {i['eligibility_pass']}·시장 {i['market_pass']}·손절 {i['stop_valid']}")
