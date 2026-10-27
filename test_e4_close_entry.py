# -*- coding: utf-8 -*-
"""E4 (사용자 10/27): 진입 시점 비교 — 신호일 종가 진입(마감 직전 매수 근사) vs 다음 날 시가 진입(현재 운영). 측정용.

    python test_e4_close_entry.py

순수 계산 — 임시 데이터만.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.research.s1_outcome import (  # noqa: E402
    DATA_GAP_UNCERTAIN, ENTERED, NOT_ENTERED_ABOVE_CAP, NOT_ENTERED_AT_OR_BELOW_STOP, STOP, STOP_GAP, TIME,
    OutcomeParams, paired, simulate,
)
from domain.research.series import ResearchBar  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


DAYS = [d for d in (date(2026, 3, 2) + timedelta(days=i) for i in range(40)) if d.weekday() < 5]
SIG = {"date": DAYS[0].isoformat(), "code": "000001", "stop_ref": 9_500.4, "entry_cap": 10_105.0}
PO = OutcomeParams(exit_slip_bps=10.0)
PC = replace(PO, entry_mode="close", close_entry_slip_bps=10.0)


def bars(spec):
    return {DAYS[i]: ResearchBar(DAYS[i], *v[:4], v[4] if len(v) > 4 else 1000) for i, v in spec.items()}


def flat(n, over=None, px=10_000):
    sp = {i: (px, px + 50, px - 50, px) for i in range(n)}
    sp.update(over or {})
    return sp


r1 = simulate(SIG, DAYS, bars(flat(20)), PC)
check("1) 종가 진입: 신호일 종가 10,000 × (1+10bp) = 10,010, 진입일 = 신호일(0일), 손절 없으면 10번째 거래일 종가×(1−0.1%) 시간 청산",
      r1["status"] == ENTERED and abs(r1["entry"] - 10_010) < 1e-6 and r1["entry_date"] == DAYS[0].isoformat()
      and r1["exit"] == TIME and r1["exit_date"] == DAYS[10].isoformat() and r1["hold_sessions"] == 10
      and abs(r1["stop_dist_pct"] - (10_010 - 9_500) / 10_010 * 100) < 1e-9)
up = bars(flat(20, {1: (10_400, 10_500, 10_300, 10_450)}))
ro, rc = simulate(SIG, DAYS, up, PO), simulate(SIG, DAYS, up, PC)
check("2) 다음 날 시가 10,400 > 지정가 10,100 → 시가 진입은 놓침(상한 초과), 종가 진입은 신호일 종가로 이미 보유",
      ro["status"] == NOT_ENTERED_ABOVE_CAP and rc["status"] == ENTERED)
r3 = simulate(SIG, DAYS, bars(flat(20, {1: (9_000, 9_100, 8_900, 9_050)})), PC)
r3b = simulate(SIG, DAYS, bars(flat(20, {1: (9_900, 9_950, 9_450, 9_900)})), PC)
check("3) 종가 진입 뒤 밤사이 악재: 다음 날 시가 9,000 ≤ 손절 → 갭 손절(시가×0.999, 첫날 판정 불확실 없음), 장중 저가 9,450 → 손절가 청산",
      r3["exit"] == STOP_GAP and abs(r3["exit_price"] - 9_000 * 0.999) < 1e-6 and r3["hold_sessions"] == 1
      and not r3["same_day_uncertain"] and r3b["exit"] == STOP and abs(r3b["exit_price"] - 9_500 * 0.999) < 1e-6)
r4 = simulate(SIG, DAYS, bars(flat(20, {0: (9_600, 9_700, 9_400, 9_450)})), PC)
g = flat(20)
g.pop(4)
r5 = simulate(SIG, DAYS, bars(g), PC)
check("4) 신호일 종가 ≤ 손절 기준 → 미진입, 보유 중 봉 없는 세션 → DATA_GAP_UNCERTAIN(확정 제외)",
      r4["status"] == NOT_ENTERED_AT_OR_BELOW_STOP and r5["quality"] == DATA_GAP_UNCERTAIN)
O = {("a", "1"): {**ro, "date": "a", "code": "1"},
     ("b", "1"): {"status": ENTERED, "exit": TIME, "quality": "OK", "net_pct": 2.0, "date": "b", "code": "1"}}
C = {("a", "1"): {**rc, "date": "a", "code": "1"},
     ("b", "1"): {"status": ENTERED, "exit": TIME, "quality": "OK", "net_pct": 5.0, "date": "b", "code": "1"}}
pr = paired(O, C)
check("5) 짝 비교: 두 방식 모두 확정인 신호만 차이(+3.0%p, 종가가 나은 1/1), 시가에서 상한 초과로 놓친 신호 1건은 종가 진입 결과로 따로",
      pr["n_both"] == 1 and abs(pr["mean_diff"] - 3.0) < 1e-9 and pr["close_better"] == 1 and len(pr["missed"]) == 1
      and pr["missed_metrics"]["n"] == 1)

passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
