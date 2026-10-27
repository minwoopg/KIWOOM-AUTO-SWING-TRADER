from __future__ import annotations

"""S1 신호 빈도 분석 (E1 — GPT 10/21: "원래 신호가 드문 전략인지, 지금 관찰 종목에만 없는지").

과거 거래일마다 종목별로 **운영과 같은 `evaluate_s1`**을 다시 계산해, 기본 S1과 비교 변형(분석 전용 — 주문·운영 판정에 쓰지 않음)의
통과 수를 셉니다. 순수 계산(입력: 봉 목록·세션·자격) — 파일·API를 건드리지 않음.

비교 변형 (S1 조건을 바꾸자는 결정이 아니라 "어느 조건이 얼마나 막는지" 측정용):
| 이름 | 내용 |
|---|---|
| BASE | 운영 S1 그대로 |
| NO_MARKET | 시장 조건(MARKET_REGIME)만 뺌 — 패턴·자격·손절 기준 |
| MARKET_MIXED_OK | 시장 판정 MIXED도 허용(RISK_OFF·UNKNOWN은 제외) |
| NO_TREND | 추세 조건(C_GT_MA120·MA60_RISING)만 뺌 |
| PULLBACK_2_15 | 눌림 허용 2~15일(기본 2~10) — S1 다시 계산 |

가격 열(stop_ref·entry_cap·pullback_days)은 **BASE(운영 S1) 계산값**이며 변형별 값이 아님(E3).

한계(보고서에 표시): 현재 저장된 수정주가 기준(그날 실제로 보였던 값이 아님), 종목 자격은 최근 종목 목록 기준(과거 관리·경고
상태 아님), 진입·청산·수익은 계산하지 않음(신호 수만).
"""

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable

from domain.research.market import MIXED, RISK_ON, classify_market
from domain.research.s1 import Eligibility, S1Config, evaluate_s1
from domain.research.series import SeriesView

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"
VARIANTS = ("BASE", "NO_MARKET", "MARKET_MIXED_OK", "NO_TREND", "PULLBACK_2_15")
VARIANT_LABEL = {"BASE": "운영 S1 그대로", "NO_MARKET": "시장 조건 제외", "MARKET_MIXED_OK": "시장 MIXED도 허용",
                 "NO_TREND": "추세 조건(120일선 위·60일선 상승) 제외", "PULLBACK_2_15": "눌림 2~15일 허용"}
TREND_CHECKS = ("C_GT_MA120", "MA60_RISING")


def _combine(vals: Iterable[str]) -> str:
    vals = list(vals)
    if not vals:
        return UNKNOWN
    if FAIL in vals:
        return FAIL
    if UNKNOWN in vals:
        return UNKNOWN
    return PASS


def _pattern_without(res: dict, names: tuple[str, ...]) -> str:
    groups = {"SECURITY_TYPE", "RISK_STATUS", "LIQUIDITY_TV20", "MARKET_REGIME", "STOP_VALID"}
    return _combine(c["result"] for c in res["checks"] if c["name"] not in groups and c["name"] not in names)


def variants_of(res: dict, res_pb15: dict | None) -> dict[str, str]:
    """S1Result.to_dict() → 변형별 최종 판정."""
    pe = [res["eligibility_pass"], res["stop_valid"]]
    state = (res.get("market") or {}).get("state")
    out = {"BASE": res["eligible_signal"],
           "NO_MARKET": _combine([res["pattern_pass"], *pe]),
           "MARKET_MIXED_OK": _combine([res["pattern_pass"], *pe,
                                        PASS if state in (RISK_ON, MIXED) else (FAIL if state else UNKNOWN)])
           if res["market_pass"] != UNKNOWN or state in (RISK_ON, MIXED) else _combine([res["pattern_pass"], *pe, UNKNOWN]),
           "NO_TREND": _combine([_pattern_without(res, TREND_CHECKS), *pe, res["market_pass"]])}
    if res_pb15 is not None:
        out["PULLBACK_2_15"] = res_pb15["eligible_signal"]
    return out


# G1(GPT 10/27): 이력 창(min_history) 검사 실패를 원인별로 나눔 — 이전에는 모두 SHORT_HISTORY였음.
HIST_SHORT = "SHORT_HISTORY"              # 첫 봉 이전 세션이 창에 걸림(상장·수집 시작이 늦음 — 진짜 이력 부족)
HIST_GAP = "DATA_GAP_IN_WINDOW"           # 첫 봉 이후인데 봉이 없는 세션(수집 누락·원인 불명 공백)
HIST_NO_TRADES = "NO_TRADES_IN_WINDOW"    # 창 안에 거래량 0 봉(거래정지 가능성 — 정지 확정 아님)
HIST_SESSIONS = "SHORT_SESSIONS"          # 분석 세션 목록 자체가 짧음
HOLD_LABEL = {HIST_SHORT: "이력 부족", HIST_GAP: "중간 공백(원인 불명)", HIST_NO_TRADES: "창 안 거래 없는 봉",
              HIST_SESSIONS: "세션 목록 부족", "NO_TRADES_AT_T": "그날 거래 없는 봉", "NO_BAR_AT_T": "그날 봉 없음",
              "T_NOT_SESSION": "거래일 아님"}


def history_status(hist: dict | None) -> tuple[str, str]:
    """HISTORY 검사 → (data_status, 세부). detail 예: INSUFFICIENT_HISTORY · DATA_GAP:2026-04-02 · NO_TRADES:2026-04-02."""
    if hist and hist["result"] == PASS:
        return "OK", ""
    why = (hist or {}).get("detail") or ""
    if why.startswith("DATA_GAP"):
        return HIST_GAP, why.partition(":")[2]
    if why.startswith("NO_TRADES"):
        return HIST_NO_TRADES, why.partition(":")[2]
    if why == "INSUFFICIENT_SESSIONS":
        return HIST_SESSIONS, ""
    return HIST_SHORT, why


@dataclass
class FrequencyResult:
    rows: list[dict] = field(default_factory=list)          # 종목·날짜 한 줄(판정 요약)
    regimes: dict[str, dict[str, str]] = field(default_factory=dict)   # 날짜 → 지수 → 시장 판정
    skipped: dict[str, str] = field(default_factory=dict)    # 종목 → 건너뛴 사유


def analyze(dates: list[date], sessions_for: callable, stocks: dict[str, dict], indexes: dict[str, list],
            cfg: S1Config | None = None) -> FrequencyResult:
    """dates: 분석할 신호일들. sessions_for(t) → 그날 기준 세션 목록(운영 스캔과 같은 창).
    stocks: code → {"bars": [...], "eligibility": Eligibility, "index_id": "INDEX:KOSPI:001", "name": ...}
    indexes: index_id → bars."""
    cfg = cfg or S1Config()
    cfg15 = replace(cfg, pullback_max=15)
    out = FrequencyResult()
    for t in dates:
        sess = sessions_for(t)
        iviews = {iid: SeriesView(b, sess, t, source_id=iid) for iid, b in indexes.items()}
        # E2(GPT 10/22): 시장 판정 분포는 종목 구성과 무관하게 지수마다 날짜별로 직접 계산
        reg = {iid: classify_market(v).state for iid, v in iviews.items()}
        for code, s in stocks.items():
            iv = iviews.get(s["index_id"])
            if iv is None:
                out.skipped.setdefault(code, f"지수 {s['index_id']} 없음")
                continue
            view = SeriesView(s["bars"], sess, t, source_id=f"STOCK:{code}")
            st = view.status()
            if st:
                out.rows.append({"date": t.isoformat(), "code": code, "data_status": st,
                                 **{g: UNKNOWN for g in ("pattern_pass", "eligibility_pass", "market_pass",
                                                         "stop_valid", "eligible_signal")},
                                 "checks": [], "variants": {v: UNKNOWN for v in VARIANTS}})
                continue
            res = evaluate_s1(code, view, iv, None, s["eligibility"], cfg).to_dict()
            res15 = evaluate_s1(code, view, iv, None, s["eligibility"], cfg15).to_dict()
            hist = next((c for c in res["checks"] if c["name"] == "HISTORY"), None)
            ds, dd = history_status(hist)
            out.rows.append({"date": t.isoformat(), "code": code, "data_status": ds, "data_detail": dd,
                             **{g: res[g] for g in ("pattern_pass", "eligibility_pass", "market_pass", "stop_valid",
                                                    "eligible_signal")},
                             "checks": [{"name": c["name"], "result": c["result"],
                                         **({"detail": c.get("detail") or ""} if c["result"] != PASS else {})}
                                        for c in res["checks"]],
                             "variants": variants_of(res, res15),
                             "market_state": (res.get("market") or {}).get("state"),
                             "stop_ref": (res.get("levels") or {}).get("stop_ref"),
                             "entry_cap": (res.get("levels") or {}).get("entry_cap"),
                             "pullback_days": (res.get("pullback") or {}).get("pullback_len")})
        out.regimes[t.isoformat()] = reg
    return out
