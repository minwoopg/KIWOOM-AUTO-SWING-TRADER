# -*- coding: utf-8 -*-
"""A3: S1 눌림 회복 평가기(s1_pullback_v0.1) 회귀 테스트 (가짜 데이터).

시나리오를 가격 흐름으로 만들고, 조건별 PASS/FAIL/UNKNOWN과 사유를 확인합니다.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, timedelta

sys.path.insert(0, ".")

from domain.research.market import classify_market
from domain.research.s1 import COMMON, Eligibility, S1Config, candidate_sort_key, evaluate_s1
from domain.research.series import ResearchBar, SeriesView
from domain.research.types import Tri

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


S = weekdays(date(2025, 1, 6), 260)
TV = 5_000_000_000


def bar(d, c, *, up=0.01, dn=0.01, vol=100_000, tv=TV):
    return ResearchBar(d, c, c * (1 + up), c * (1 - dn), c, vol, tv)


def scenario(*, n_trend=200, drift=1.003, pivot_jump=1.03, pullback=(0.99, 0.98, 0.97, 0.965),
             recover=1.03, tv=TV):
    """완만한 상승 → 고점(p) → 눌림 → t일 회복. 반환: (bars, t)."""
    closes = [10_000 * drift ** i for i in range(n_trend)]
    bars = [bar(S[i], c, tv=tv) for i, c in enumerate(closes)]
    i = n_trend
    cp = closes[-1] * pivot_jump
    bars.append(bar(S[i], cp, tv=tv))                       # 고점 봉 p
    i += 1
    for f in pullback:
        bars.append(bar(S[i], cp * f, up=0.005, dn=0.005, tv=tv))
        i += 1
    ct = bars[-1].close * recover
    bars.append(bar(S[i], ct, tv=tv))                       # 회복일 t
    return bars, S[i]


def index_bars(rate=1.001, n=260):
    return [bar(S[i], 2500 * rate ** i, up=0.005, dn=0.005, tv=None) for i in range(n)]


OK = Eligibility(COMMON, "OK")
IDX = index_bars()


def run(bars, t, *, idx=IDX, elig=OK, cfg=None, sessions=S):
    sv, iv = SeriesView(bars, sessions, t), SeriesView(idx, sessions, t)
    return evaluate_s1("005930", sv, iv, classify_market(iv), elig, cfg)


def res_of(r, name):
    return r.check(name).result


# ── 1. 정상 눌림 회복 ──────────────────────────────────────────
bars, t = scenario()
r = run(bars, t)
check("1-1) 정상 눌림 회복 → 패턴·자격·시장 모두 PASS, 최종 PASS",
      r.pattern_pass == Tri.PASS and r.eligibility_pass == Tri.PASS and r.market_pass == Tri.PASS
      and r.eligible_signal == Tri.PASS)
check("1-2) 고점 봉 = 급등일, 조정 4개, 눌림 저점 기록", r.pullback["pivot_date"] == S[200].isoformat()
      and r.pullback["pullback_len"] == 4 and r.pullback["pullback_low"] < r.pullback["pivot_close"])
lv = r.levels
check("1-3) 참고 손절가 = min(눌림 저점, t 저가) - 0.2×ATR, 진입 상한 = C + 0.5×ATR",
      abs(lv["stop_ref"] - (min(r.pullback["pullback_low"], bars[-1].low) - 0.2 * lv["atr14"])) < 1e-6
      and abs(lv["entry_cap"] - (bars[-1].close + 0.5 * lv["atr14"])) < 1e-6 and lv["stop_status"] == "OK")
check("1-4) 관찰값(RS20·거래량비·종가위치·120선 거리)은 조건이 아니라 기록만",
      all(k in r.observations for k in ("rs20", "volume_ratio", "close_location", "ma120_distance_atr"))
      and r.check("RS20") is None)

# ── 2. 탈락 사유 ─────────────────────────────────────────────
bars, t = scenario(pullback=(0.95, 0.88, 0.80, 0.78))
r = run(bars, t)
check("2-1) 깊은 조정으로 MA60 이탈 → PULLBACK FAIL(MA60_BREAK)",
      res_of(r, "PULLBACK") == Tri.FAIL and "MA60_BREAK" in r.check("PULLBACK").detail and r.pattern_pass == Tri.FAIL)
r = run(*scenario(), idx=index_bars(rate=1.006))
check("2-2) 지수가 더 강하면 RS60 FAIL", res_of(r, "RS60_POS") == Tri.FAIL)
r = run(*scenario(recover=1.12))
check("2-3) 회복일 급등(과열) → NOT_EXTENDED FAIL", res_of(r, "NOT_EXTENDED") == Tri.FAIL)
r = run(*scenario(recover=1.001))
check("2-4) 전일 고가를 못 넘으면 CLOSE_GT_PREV_HIGH FAIL", res_of(r, "CLOSE_GT_PREV_HIGH") == Tri.FAIL)
r = run(*scenario(drift=0.997, pivot_jump=1.02))
check("2-5) 하락 추세 속 반등(120선 아래·MA60 하락) → 추세 FAIL",
      res_of(r, "C_GT_MA120") == Tri.FAIL and res_of(r, "MA60_RISING") == Tri.FAIL and r.eligible_signal == Tri.FAIL)
r = run(*scenario(pullback=(0.99,)))
check("2-6) 조정 1개(2~10개 미만) → PULLBACK_LEN FAIL", "PULLBACK_LEN" in r.check("PULLBACK").detail)
r = run(*scenario(pullback=(1.0, 1.0, 1.0)))
check("2-7) 고점 종가보다 낮은 종가 없음 → NO_LOWER_CLOSE", "NO_LOWER_CLOSE" in r.check("PULLBACK").detail)

# 고점이 t-1 → 빈 조정 구간
bars_e = [bar(S[i], 10_000 * 1.003 ** i) for i in range(210)]
r = run(bars_e, S[209])
check("2-8) 매일 신고가(고점이 t-1) → EMPTY_PULLBACK", "EMPTY_PULLBACK" in r.check("PULLBACK").detail)

# 고점 동률 → 가장 최근 봉
bars, t = scenario()
p1 = bars[200]
twin = ResearchBar(bars[201].date, p1.close, p1.high, p1.close * 0.99, p1.close, 100_000, TV)
bars2 = bars[:201] + [twin] + bars[202:]
r = run(bars2, t)
check("2-9) 고점 동률이면 가장 최근 봉을 고점으로", r.pullback["pivot_date"] == S[201].isoformat())

# ── 3. 자격·시장 분리 ────────────────────────────────────────
bars, t = scenario()
r = run(bars, t, elig=Eligibility(COMMON, None))
check("3-1) 위험 상태 UNKNOWN → 자격 UNKNOWN, 최종 UNKNOWN, 패턴은 PASS로 보존",
      r.eligibility_pass == Tri.UNKNOWN and r.eligible_signal == Tri.UNKNOWN and r.pattern_pass == Tri.PASS)
r = run(bars, t, elig=Eligibility("PREFERRED", "OK"))
check("3-2) 우선주 → SECURITY_TYPE FAIL", res_of(r, "SECURITY_TYPE") == Tri.FAIL and r.eligible_signal == Tri.FAIL)
r = run(bars, t, elig=Eligibility(COMMON, "FLAGGED", "관리종목"))
check("3-3) 위험 종목 → RISK_STATUS FAIL(사유 기록)", res_of(r, "RISK_STATUS") == Tri.FAIL
      and "관리종목" in r.check("RISK_STATUS").detail)
r = run(*scenario(tv=2_000_000_000))
check("3-4) 20일 거래대금 20억 → LIQUIDITY FAIL (기준 30억)", res_of(r, "LIQUIDITY_TV20") == Tri.FAIL)
r = run(*scenario(tv=None))
check("3-5) 거래대금 원천 없음 → LIQUIDITY UNKNOWN, 패턴은 계산",
      res_of(r, "LIQUIDITY_TV20") == Tri.UNKNOWN and r.pattern_pass == Tri.PASS)
r = run(bars, t, idx=[bar(S[i], 2500.0, up=0.005, dn=0.005, tv=None) for i in range(260)])
check("3-6) 시장 MIXED → 시장 보류(FAIL·MARKET_HOLD), 패턴 신호는 보존",
      r.market_pass == Tri.FAIL and r.check("MARKET_REGIME").detail == "MARKET_HOLD" and r.pattern_pass == Tri.PASS)
idx_gap = [b for b in IDX if b.date != t]
r = run(bars, t, idx=idx_gap)
check("3-7) 기준일 지수 봉 없음 → 시장 UNKNOWN, RS UNKNOWN, 최종 UNKNOWN",
      r.market_pass == Tri.UNKNOWN and res_of(r, "RS60_POS") == Tri.UNKNOWN and r.eligible_signal == Tri.UNKNOWN)

# ── 4. 데이터 부족·공백·ATR 0·손절가 ─────────────────────────
bars, t = scenario()
r = run(bars[60:], t)
check("4-1) 이력 145개(<160) → HISTORY UNKNOWN, 패턴 UNKNOWN(FAIL로 바꾸지 않음)",
      res_of(r, "HISTORY") == Tri.UNKNOWN and r.pattern_pass == Tri.UNKNOWN
      and r.eligible_signal == Tri.UNKNOWN)
r = run([b for b in bars if b.date != S[150]], t)
check("4-2) 160봉 창 안의 공백 → HISTORY UNKNOWN(DATA_GAP), 앞 봉으로 채우지 않음",
      res_of(r, "HISTORY") == Tri.UNKNOWN and r.check("HISTORY").detail.startswith("DATA_GAP"))
flat = [ResearchBar(S[i], 100.0, 100.0, 100.0, 100.0, 1000, TV) for i in range(210)]
r = run(flat, S[209])
check("4-3) ATR 0 → ATR_POS FAIL, 과열 판정 UNKNOWN(분모 0)",
      res_of(r, "ATR_POS") == Tri.FAIL and res_of(r, "NOT_EXTENDED") == Tri.UNKNOWN)
r = run(*scenario(), cfg=replace(S1Config(), stop_atr_buffer=10_000.0))
check("4-4) 참고 손절가 ≤ 0 → INVALID_STOP (위험 비율 계산 안 함)",
      r.levels["stop_status"] == "INVALID_STOP" and r.levels["risk_ratio_at_cap"] is None)

# ── 5. 미래 데이터·재현성 ────────────────────────────────────
bars, t = scenario()
base = run(bars, t).to_dict()
t_i = S.index(t)
future = bars + [bar(S[t_i + k], 1.0 + k) for k in range(1, 20)]          # 이후 폭락 봉 추가
idx_future = IDX[:t_i + 1] + [bar(S[t_i + k], 99_999.0) for k in range(1, 20)]
check("5-1) t 이후 봉·지수를 추가·변경해도 t 신호 불변", run(future, t, idx=idx_future).to_dict() == base)
check("5-2) 같은 입력·설정 → 같은 결과", run(bars, t).to_dict() == base)
cfg2 = replace(S1Config(), min_trade_value_20=2_000_000_000)
check("5-3) 설정 해시: 같은 설정 동일, 값 바꾸면 다름",
      S1Config().config_hash() == S1Config().config_hash() and cfg2.config_hash() != S1Config().config_hash())
check("5-4) 기록에 전략·지표 버전·설정 해시", base["strategy"] == "s1_pullback_v0.1" and base["feature_version"] == "f1"
      and base["config_hash"] == S1Config().config_hash())

# ── 6. 후보 정렬 ─────────────────────────────────────────────
a = run(*scenario(), idx=IDX)
b = run(*scenario(drift=1.004), idx=IDX)
a.symbol, b.symbol = "000660", "005930"
c = run(*scenario(), idx=IDX)
c.symbol = "000100"
order = [x.symbol for x in sorted([a, b, c], key=candidate_sort_key)]
check("6-1) RS60 내림차순 → 동률이면 거래대금 → 종목코드 오름차순 (결정적)",
      order[0] == "005930" and order[1:] == ["000100", "000660"])

# ── 7. 주문 경로와 분리 ──────────────────────────────────────
from pathlib import Path  # noqa: E402

src = " ".join(p.read_text(encoding="utf-8") for p in Path("domain/research").glob("*.py"))
check("7-1) 연구 계층은 브로커·주문 실행부·원장·네트워크를 쓰지 않음",
      not any(w in src for w in ("infra.broker", "order_executor", "place_order", "fill_ledger",
                                 "requests", "OrderIntent", "import os")))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
