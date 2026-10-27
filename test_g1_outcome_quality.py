# -*- coding: utf-8 -*-
"""G1 (GPT 10/27): 데이터 공백 처리 · 계좌 기준 손익 · 진입 시 손절 거리 · 데이터 보류 사유 세분화.

    python test_g1_outcome_quality.py

순수 계산 — 임시 데이터만. 연구용 일봉 근사(운영 체결 재현 아님)의 규칙이 문서대로 동작하는지 확인.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.research.s1_frequency import (  # noqa: E402
    HIST_GAP, HIST_NO_TRADES, HIST_SHORT, history_status,
)
from domain.research.s1_outcome import (  # noqa: E402
    DATA_GAP_UNCERTAIN, ENTERED, ENTRY_DATA_GAP, ENTRY_NO_TRADES, OPEN_AT_END, QUALITY_OK, TIME, OutcomeParams,
    account, distance_buckets, distance_caps, metrics, sequential, simulate,
)
from domain.research.s1_stats import data_quality  # noqa: E402
from domain.research.series import ResearchBar, SeriesView  # noqa: E402
from domain.service.trade_costs import CostProfile, estimate_cost  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


DAYS = [d for d in (date(2026, 3, 2) + timedelta(days=i) for i in range(60)) if d.weekday() < 5]
T = DAYS[0]
_PROF = CostProfile()
CF = lambda k, q, px: estimate_cost(k, q, px, _PROF).amount  # noqa: E731
P = OutcomeParams(exit_slip_bps=0.0, cost_fn=CF)
SIG = {"date": T.isoformat(), "code": "000001", "stop_ref": 9_500.4, "entry_cap": 10_105.0}   # 지정가 10,100 · 손절 9,500


def bars(spec: dict[int, tuple]) -> dict:
    out = {}
    for i, v in spec.items():
        o, h, l, c = v[:4]
        out[DAYS[i]] = ResearchBar(DAYS[i], o, h, l, c, v[4] if len(v) > 4 else 1000)
    return out


def flat(n, over=None, px=10_000):
    sp = {i: (px, px + 50, px - 50, px) for i in range(n)}
    sp.update(over or {})
    return sp


# ── 1. 데이터 공백 ──
# GPT 재현: 셋째 날 봉(저가 9,400 — 손절 9,500 아래)이 수집에서 빠지고 이후 상승 → 이전 코드는 +11% 시간 청산을 확정 손익에 넣었음
up = flat(20, {i: (11_100, 11_150, 11_050, 11_100) for i in range(4, 20)})
up.pop(3)
r1 = simulate(SIG, DAYS, bars(up), P)
check("1-1) GPT 재현: 보유 중 봉 하나 빠짐(그날 손절 여부 불명) → 계산은 이어 가되(+11%) DATA_GAP_UNCERTAIN, 공백 날짜 기록, "
      "확정 손익 통계 0건",
      r1["status"] == ENTERED and r1["exit"] == TIME and r1["net_pct"] > 10 and r1["quality"] == DATA_GAP_UNCERTAIN
      and r1["gap_days"] == [DAYS[3].isoformat()] and metrics([r1])["n"] == 0)
r2 = simulate(SIG, DAYS, bars(flat(20, {3: (10_000, 10_000, 10_000, 10_000, 0)})), P)
check("1-2) 거래량 0 봉은 공백이 아님 — 거래 없는 날로 건너뜀(no_trades_days 1), 품질 OK, 확정 표본에 포함, 시간 청산은 거래일 기준 그대로(10번째 거래일)",
      r2["quality"] == QUALITY_OK and r2["no_trades_days"] == 1 and r2["exit_date"] == DAYS[11].isoformat()
      and metrics([r2])["n"] == 1)
r3 = simulate(SIG, DAYS, bars(flat(7)), P)
check("1-3) 그 종목 마지막 봉 이후 세션은 공백이 아니라 데이터 끝 → 미청산(OPEN_AT_END), 공백 0",
      r3["exit"] == OPEN_AT_END and r3["gap_days"] == [] and r3["quality"] == QUALITY_OK)
g = flat(20)
g.pop(1)
r4 = simulate(SIG, DAYS, bars(g), P)
r5 = simulate(SIG, DAYS, bars(flat(20, {1: (10_000, 10_000, 10_000, 10_000, 0)})), P)
check("1-4) 진입일: 봉 없음 = ENTRY_DATA_GAP, 거래량 0 = ENTRY_NO_TRADES (이전엔 둘 다 ENTRY_NO_DATA)",
      r4["status"] == ENTRY_DATA_GAP and r5["status"] == ENTRY_NO_TRADES)

# ── 2. 손절 거리 ──
r6 = simulate(SIG, DAYS, bars(flat(20)), P)
check("2-1) 손절 거리: 진입가 10,000 기준 (10,000−9,500)/10,000 = 5.00%, 상한 10,100 기준 5.94%(따로 기록)",
      abs(r6["stop_dist_pct"] - 5.0) < 1e-9 and abs(r6["stop_dist_cap_pct"] - 600 / 10_100 * 100) < 1e-9)
check("2-2) 예상 손실액(1주) = 500 + 매수 비용 35 + 손절가 매도 비용(수수료 34·세금 19) = 588원(5.88%)",
      r6["exp_loss"] == 500 + 35 + 34 + 19 and abs(r6["exp_loss_pct"] - 5.88) < 1e-9)


def tr(dist, net, q=QUALITY_OK):
    return {"status": ENTERED, "exit": TIME, "quality": q, "stop_dist_pct": dist, "net_pct": net}


TS = [tr(3, -3), tr(6, 20), tr(9, -8), tr(13, 30), tr(14, -15), tr(4, 50, DATA_GAP_UNCERTAIN)]
bk = distance_buckets(TS)
check("2-3) 구간(상한 표와 같은 경계): 5% 이하 1(−3%) · 5~8% 1(+20%) · 8~12% 1(−8%) · 12% 초과 2(평균 +7.5%, 최악 −15%, 큰 이익 1) — 공백 불확실 제외",
      [b["n"] for b in bk] == [1, 1, 1, 2] and abs(bk[3]["mean"] - 7.5) < 1e-9 and bk[3]["worst"] == -15
      and bk[3]["big_wins"] == 1)
cp = {c["cap"]: c for c in distance_caps(TS)}
check("2-4) 상한별: ≤5% → 진입 가정 2(공백 포함)·확정 1, ≤8% → 확정 2·큰 이익 1/2, 상한 없음 → 확정 5·큰 이익 2/2",
      cp[5.0]["entered"] == 2 and cp[5.0]["n"] == 1 and cp[8.0]["n"] == 2 and cp[8.0]["big_wins"] == 1
      and cp[8.0]["all_big"] == 2 and cp[None]["n"] == 5 and cp[None]["big_wins"] == 2)

# ── 3. 계좌 기준 ──
dip = flat(20, {5: (9_700, 9_750, 9_550, 9_600)})                # 중간 종가 9,600(손절 9,500 위) 뒤 회복
B = {"000001": bars(dip)}
ra = simulate(SIG, DAYS, B["000001"], P)
seq = sequential([SIG], {(SIG["date"], SIG["code"]): ra})
A1 = account(seq, B, DAYS, 100_000, lambda px: 1, CF)
check("3-1) 1주: 매수 10,000+35 → 매도 10,000−55 → 최종 99,910(−90원) · 일별 평가 최대 낙폭은 중간 종가 9,600 날 −435원"
      "(거래 수익률 합산으로는 안 보이는 보유 중 하락)",
      A1["final"] == 99_910 and A1["net_won"] == -90 and A1["mdd_won"] == -435 and abs(A1["mdd_pct"] + 0.435) < 1e-9
      and A1["total_cost"] == 90 and A1["n_trades"] == 1)
A2 = account(seq, B, DAYS, 100_000, lambda px: 50_000 // px, CF)
check("3-2) 고정 금액 50,000 → 5주: 최종 100,000 − 175 − 275 = 99,550, 최대 투입 비중 50%",
      A2["final"] == 99_550 and abs(A2["max_exposure_pct"] - 50.0) < 1e-9)
A3 = account(seq, B, DAYS, 100_000, lambda px: 100_000 // px, CF)
A4 = account(seq, B, DAYS, 100_000, lambda px: 5_000 // px, CF)
A6 = account(seq, B, DAYS, 9_000, lambda px: 1, CF)
check("3-3) 투입 100,000(자금 전액) → 10주는 비용 포함 현금 초과라 9주로 줄임(90,000+315) · 투입 5,000 → 수량 0 건너뜀 · "
      "자금 9,000 → 1주도 못 삼(현금 부족, 자금 그대로)",
      A3["n_trades"] == 1 and abs(A3["max_exposure_pct"] - 90.0) < 1e-9 and A4["skipped"][0][1].startswith("수량 0")
      and A6["skipped"][0][1] == "현금 부족" and A6["final"] == 9_000 and A6["n_trades"] == 0)
ro = simulate(SIG, DAYS, bars(flat(7, {6: (10_400, 10_450, 10_350, 10_400)})), P)
A5 = account(sequential([SIG], {(SIG["date"], SIG["code"]): ro}), {"000001": bars(flat(7, {6: (10_400, 10_450, 10_350, 10_400)}))},
             DAYS[:7], 100_000, lambda px: 1, CF)
check("3-4) 미청산은 마지막 날 종가로 평가(매도 비용 없음): 100,000 − 10,035 + 10,400 = 100,365",
      A5["open_at_end"] and A5["final"] == 100_365)

# ── 4. 데이터 보류 사유 ──
sess = DAYS[:30]
bl = [ResearchBar(d, 100, 101, 99, 100, 10) for d in sess[5:]]
v_short = SeriesView(bl, sess, sess[-1])
bl_gap = [b for b in bl if b.date != sess[20]]
v_gap = SeriesView(bl_gap, sess, sess[-1])
bl_nt = [ResearchBar(b.date, 100, 100, 100, 100, 0) if b.date == sess[20] else b for b in bl]
v_nt = SeriesView(bl_nt, sess, sess[-1])


def hs(v, n=20):
    w, why = v.window(n)
    return history_status({"result": "PASS" if w else "UNKNOWN", "detail": why})


check("4-1) 이력 창 실패를 원인별로: 첫 봉 이전 = SHORT_HISTORY, 중간 봉 없음 = DATA_GAP_IN_WINDOW(날짜), "
      "거래량 0 봉 = NO_TRADES_IN_WINDOW(날짜) — 이전엔 모두 SHORT_HISTORY",
      hs(v_short, 28)[0] == HIST_SHORT and hs(v_short)[0] == "OK" and hs(v_gap) == (HIST_GAP, sess[20].isoformat())
      and hs(v_nt) == (HIST_NO_TRADES, sess[20].isoformat()))
q = data_quality(bl_nt[:10] + [b for b in bl_nt[11:]], sess)
check("4-2) 종목별 품질: 첫·마지막 봉, 그 사이 봉 없는 세션(앞쪽 상장 전·뒤쪽 데이터 끝은 세지 않음), 거래량 0 봉",
      q["first"] == sess[5] and q["last"] == sess[-1] and q["gaps"] == [sess[15]] and q["no_trades"] == [sess[20]])

passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
