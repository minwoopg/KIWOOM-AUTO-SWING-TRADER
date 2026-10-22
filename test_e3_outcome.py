# -*- coding: utf-8 -*-
"""E3 (GPT 10/22): 연구용 신호 결과 측정(일봉 근사) 고정 사례 + 빈도 분석 보조 집계.

    python test_e3_outcome.py

순수 계산 — 임시 데이터만. 운영 체결 재현이 아님(근사 규칙이 문서대로 동작하는지 확인).
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from domain.research.s1_outcome import (  # noqa: E402
    ENTERED, ENTRY_NO_DATA, NO_NEXT_DAY, NOT_ENTERED_ABOVE_CAP, NOT_ENTERED_AT_OR_BELOW_STOP, OPEN_AT_END, STOP,
    STOP_GAP, TIME, OutcomeParams, metrics, sequential, simulate,
)
from domain.research.s1_stats import episodes, longest_gap, monthly, pullback_reasons  # noqa: E402
from domain.research.series import ResearchBar  # noqa: E402
from domain.service.trade_costs import CostProfile, estimate_cost  # noqa: E402
from infra.research.report_files import create_exclusive  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


DAYS = [date(2026, 3, 2) + timedelta(days=i) for i in range(40)]
DAYS = [d for d in DAYS if d.weekday() < 5]
T = DAYS[0]


def bars(spec: dict[int, tuple]) -> dict:
    """spec: 거래일 순번 → (시가, 고가, 저가, 종가[, 거래량])"""
    out = {}
    for i, v in spec.items():
        o, h, l, c = v[:4]
        out[DAYS[i]] = ResearchBar(DAYS[i], o, h, l, c, v[4] if len(v) > 4 else 1000)
    return out


def flat(n, over=None, px=10_000):
    sp = {i: (px, px + 50, px - 50, px) for i in range(n)}
    sp.update(over or {})
    return sp


SIG = {"date": T.isoformat(), "code": "000001", "stop_ref": 9_500.4, "entry_cap": 10_105.0}   # 지정가 10,100 · 손절 9,500
_PROF = CostProfile()
P = OutcomeParams(exit_slip_bps=10.0, cost_fn=lambda k, q, px: estimate_cost(k, q, px, _PROF).amount)

r1 = simulate(SIG, DAYS, bars(flat(20, {1: (10_200, 10_250, 10_000, 10_100)})), P)
check("1) 시가 10,200 > 지정가 10,100 → 장중 저가 10,000이 지정가 아래로 내려와도 미진입(NOT_ENTERED_ABOVE_CAP)",
      r1["status"] == NOT_ENTERED_ABOVE_CAP and r1["limit"] == 10_100 and r1["stop"] == 9_500)
r2 = simulate(SIG, DAYS, bars(flat(20, {1: (9_500, 9_600, 9_400, 9_550)})), P)
check("2) 시가 ≤ 손절 기준(9,500) → 미진입", r2["status"] == NOT_ENTERED_AT_OR_BELOW_STOP)
r3 = simulate(SIG, DAYS, bars(flat(20, {3: (9_000, 9_100, 8_900, 9_050)})), P)
check("3) 갭 손절: 셋째 날 시가 9,000 ≤ 손절 9,500 → 손절가가 아니라 시가 × (1−0.1%) = 8,991 에 청산(STOP_GAP)",
      r3["status"] == ENTERED and r3["exit"] == STOP_GAP and abs(r3["exit_price"] - 8_991.0) < 1e-6
      and r3["exit_date"] == DAYS[3].isoformat())
r4 = simulate(SIG, DAYS, bars(flat(20, {1: (10_000, 10_050, 9_400, 9_900)})), P)
check("4) 진입일 저가 9,400 ≤ 손절 — 진입·손절 선후 불명 → 불확실 표본, 보수적으로 그날 손절가×(1−0.1%) 청산",
      r4["exit"] == STOP and r4["same_day_uncertain"] and r4["exit_date"] == DAYS[1].isoformat()
      and abs(r4["exit_price"] - 9_500 * 0.999) < 1e-6)
r5 = simulate(SIG, DAYS, bars(flat(20, {2: (10_000, 10_050, 9_480, 9_900)})), P)
check("5) 장중 손절: 둘째 날 저가 9,480 ≤ 9,500(시가는 위) → 손절가×(1−0.1%)", r5["exit"] == STOP and not r5["same_day_uncertain"]
      and r5["hold_sessions"] == 1)
r6 = simulate(SIG, DAYS, bars(flat(20)), P)
check("6) 손절 없음 → 진입일 0일 기준 10번째 거래일 종가×(1−0.1%) 시간 청산(종가 근사)",
      r6["exit"] == TIME and r6["exit_date"] == DAYS[11].isoformat() and r6["hold_sessions"] == 10
      and abs(r6["exit_price"] - 10_000 * 0.999) < 1e-6)
sp7 = flat(20)
sp7.pop(11)
sp7.pop(5)
r7 = simulate(SIG, DAYS, bars(sp7), P)
check("7) 보유 중 봉 없는 날(거래정지·휴장 등) 건너뜀 — 10번째 거래일 봉이 없으면 다음 봉 있는 날 시간 청산, 빠진 날 2",
      r7["exit"] == TIME and r7["exit_date"] == DAYS[12].isoformat() and r7["missing_days"] == 2)
r8 = simulate(SIG, DAYS, bars(flat(6)), P)
check("8) 데이터가 끝날 때까지 청산 안 됨 → 미청산(OPEN_AT_END, 마지막 종가 평가) — 손익 통계에서 제외",
      r8["exit"] == OPEN_AT_END and metrics([r8])["n"] == 0)
sp9 = flat(20)
sp9.pop(1)
r9 = simulate(SIG, DAYS, bars(sp9), P)
r9b = simulate({**SIG, "date": DAYS[-1].isoformat()}, DAYS, bars(flat(20)), P)
check("9) 진입일 봉 없음(거래정지) → ENTRY_NO_DATA, 신호일이 데이터 끝 → NO_NEXT_DAY",
      r9["status"] == ENTRY_NO_DATA and r9b["status"] == NO_NEXT_DAY)
check("10) 비용: 진입 10,000·청산 9,990 → 비용 전 −10원, 비용 = 매수 35 + 매도 35·20(올림) = 90 → 비용 후 −100원(−1.00%)",
      abs(r6["gross"] + 10) < 1e-6 and r6["cost"] == 35 + 35 + 20 and abs(r6["net"] + 100) < 1e-6
      and abs(r6["net_pct"] + 1.0) < 1e-9)
r11 = simulate(SIG, DAYS, bars(flat(20, {1: (10_000, 10_050, 9_950, 10_000)})),
               OutcomeParams(entry_slip_bps=200))
check("11) 진입 슬리피지는 지정가를 넘지 않음(10,000×1.02 → 10,100)", r11["entry"] == 10_100)

# 순차 계좌
S = [{"date": DAYS[0].isoformat(), "code": "000002"}, {"date": DAYS[0].isoformat(), "code": "000001"},
     {"date": DAYS[3].isoformat(), "code": "000003"}, {"date": DAYS[12].isoformat(), "code": "000001"}]
RES = {(DAYS[0].isoformat(), "000002"): {"date": DAYS[0].isoformat(), "code": "000002", "status": ENTERED,
                                         "entry_date": DAYS[1].isoformat(), "exit": TIME, "exit_date": DAYS[11].isoformat(),
                                         "net_pct": 2.0, "gross_pct": 2.2, "net": 200},
       (DAYS[0].isoformat(), "000001"): {"date": DAYS[0].isoformat(), "code": "000001", "status": NOT_ENTERED_ABOVE_CAP,
                                         "entry_date": DAYS[1].isoformat()},
       (DAYS[3].isoformat(), "000003"): {"date": DAYS[3].isoformat(), "code": "000003", "status": ENTERED,
                                         "entry_date": DAYS[4].isoformat(), "exit": STOP, "exit_date": DAYS[5].isoformat(),
                                         "net_pct": -3.0, "gross_pct": -2.8, "net": -300},
       (DAYS[12].isoformat(), "000001"): {"date": DAYS[12].isoformat(), "code": "000001", "status": ENTERED,
                                          "entry_date": DAYS[13].isoformat(), "exit": STOP, "exit_date": DAYS[15].isoformat(),
                                          "net_pct": -1.0, "gross_pct": -0.8, "net": -100}}
seq = sequential(S, RES)
check("12) 순차 계좌: 같은 진입일 후보는 코드순으로 진입 가정이 되는 첫 신호(000001 상한 초과 → 000002), 보유 중 신호(000003) 제외, "
      "청산 뒤 다른 신호는 진입",
      [(t["code"], t["entry_date"]) for t in seq] == [("000002", DAYS[1].isoformat()), ("000001", DAYS[13].isoformat())])
m = metrics([RES[k] for k in sorted(RES) if RES[k]["status"] == ENTERED])
check("13) 지표: 3건(독립) 평균 −0.67% · 승률 33% · 최대 연속 손실 2 · 누적 최대 낙폭 −4%p",
      m["n"] == 3 and abs(m["net_mean"] + 2 / 3) < 1e-9 and round(m["win_rate"]) == 33 and m["max_consec_losses"] == 2
      and abs(m["mdd_pct_points"] + 4.0) < 1e-9)

# 빈도 보조 집계
rows = [
    {"date": "2026-03-02", "code": "A", "data_status": "OK", "pattern_pass": "PASS", "eligible_signal": "PASS",
     "checks": [{"name": "PULLBACK", "result": "PASS"}]},
    {"date": "2026-03-03", "code": "A", "data_status": "OK", "pattern_pass": "FAIL", "eligible_signal": "FAIL",
     "checks": [{"name": "PULLBACK", "result": "FAIL", "detail": "PULLBACK_LEN(2~10)"}]},
    {"date": "2026-03-04", "code": "A", "data_status": "OK", "pattern_pass": "PASS", "eligible_signal": "PASS",
     "checks": [{"name": "PULLBACK", "result": "PASS"}]},
    {"date": "2026-04-01", "code": "A", "data_status": "OK", "pattern_pass": "FAIL", "eligible_signal": "FAIL",
     "checks": [{"name": "PULLBACK", "result": "FAIL", "detail": "MA60_BREAK:2026-03-30"}]},
    {"date": "2026-04-02", "code": "B", "data_status": "NO_TRADES_AT_T", "pattern_pass": "UNKNOWN",
     "eligible_signal": "UNKNOWN", "checks": []},
    {"date": "2026-04-03", "code": "B", "data_status": "OK", "pattern_pass": "UNKNOWN", "eligible_signal": "UNKNOWN",
     "checks": [{"name": "PULLBACK", "result": "UNKNOWN", "detail": "SHORT_WINDOW(20)"}]},
]
pr = pullback_reasons(rows)
mo = monthly(rows)
dd = [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4), date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3)]
lg = longest_gap(dd, rows)
check("14) 눌림 실패 세부 사유(기간 초과·60일선 이탈·데이터 부족), 월별 표본·보류 사유·신호 날 수, 가장 긴 무신호(진행 중), "
      "7일 묶음",
      pr == {"눌림 기간 범위 밖(2~10일)": 1, "눌림 중 60일선 이탈": 1, "데이터 부족(SHORT_WINDOW)": 1}
      and mo[0]["final"] == 2 and mo[0]["signal_days"] == 2 and mo[1]["holds"] == {"NO_TRADES_AT_T": 1}
      and lg["days"] == 3 and lg["ongoing"] and episodes(rows) == 1)

# 결과 파일 고유·배타
import tempfile  # noqa: E402
td = Path(tempfile.mkdtemp())
ids = {create_exclusive(td, "x", (".md", ".csv"))[0] for _ in range(5)}
check("15) 결과 파일: 고유 실행 ID·배타적 생성(5번 → 서로 다른 10개 파일, 덮어쓰기 없음)",
      len(ids) == 5 and len(list(td.iterdir())) == 10)

passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
