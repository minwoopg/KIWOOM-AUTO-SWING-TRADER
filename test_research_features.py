# -*- coding: utf-8 -*-
"""A3: 연구용 지표·주봉·시장 환경 회귀 테스트 (가짜 데이터, 네트워크 없음).

기대값은 손으로 계산 가능한 단순 입력으로 정하고, 경계·실패·미래 데이터 변조를 봅니다.
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from domain.research import features as F
from domain.research.market import MIXED, RISK_OFF, RISK_ON, UNKNOWN, classify_market
from domain.research.series import ResearchBar, ResearchBarError, SeriesView
from domain.research.types import FV, Tri, combine
from domain.research.weekly import DOWN_PROXY, UP_PROXY, weekly_bars, weekly_trend

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def approx(a, b, tol=1e-9) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol * max(1.0, abs(b))


def weekdays(start: date, n: int, skip: set[date] = frozenset()) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def bars_from(sessions, closes, *, rng=2.0, vol=1000, tv=None):
    out = []
    for d, c in zip(sessions, closes):
        out.append(ResearchBar(d, c, c + rng / 2, c - rng / 2, c, vol, tv))
    return out


S = weekdays(date(2025, 1, 6), 400)

# ── 1. 기본 타입 ──────────────────────────────────────────────
check("1-1) NaN·무한대는 UNKNOWN으로", not FV(float("nan")).ok and not FV(math.inf).ok)
check("1-2) 빈 판정 목록은 UNKNOWN(통과 아님)", combine([]) == Tri.UNKNOWN)
check("1-3) FAIL 우선, 그다음 UNKNOWN", combine([Tri.PASS, Tri.UNKNOWN, Tri.FAIL]) == Tri.FAIL
      and combine([Tri.PASS, Tri.UNKNOWN]) == Tri.UNKNOWN)
try:
    ResearchBar(S[0], 10, 9, 8, 10, 1)
    bad = False
except ResearchBarError:
    bad = True
check("1-4) 가격 관계 모순 봉 거부", bad)

# ── 2. 창·공백·미래 차단 ─────────────────────────────────────
bars = bars_from(S[:50], [100.0] * 50)
v = SeriesView(bars, S, S[49])
check("2-1) 세션 기준 창: 50개 중 20개", len(v.window(20)[0]) == 20)
late = SeriesView(bars_from(S[10:60], [100.0] * 50), S, S[59])       # S[10]에 상장
check("2-2) 상장 전 구간 → INSUFFICIENT_HISTORY", late.window(51)[1] == "INSUFFICIENT_HISTORY"
      and v.window(51)[1] == "INSUFFICIENT_SESSIONS")
gap = [b for b in bars if b.date != S[40]]
vg = SeriesView(gap, S, S[49])
check("2-3) 중간 공백 → DATA_GAP(날짜), 앞 봉으로 채우지 않음", vg.window(20)[1] == f"DATA_GAP:{S[40].isoformat()}"
      and vg.window(9)[0] is not None)
check("2-4) 기준일 봉 없음 → NO_BAR_AT_T", SeriesView(bars[:49], S, S[49]).window(5)[1] == "NO_BAR_AT_T")
check("2-5) 기준일이 세션이 아님 → T_NOT_SESSION", SeriesView(bars, S, date(2025, 1, 11)).window(5)[1] == "T_NOT_SESSION")
future = bars + bars_from(S[50:60], [999.0] * 10)
check("2-6) 미래 봉은 보이지 않음", F.sma(SeriesView(future, S, S[49]), 20).value == 100.0)

# ── 3. 지표 값 (손계산) ──────────────────────────────────────
closes = [100.0 + i for i in range(60)]                       # 100..159
v = SeriesView(bars_from(S[:60], closes), S, S[59])
check("3-1) SMA20 = 평균(140..159) = 149.5", approx(F.sma(v, 20).value, 149.5))
check("3-2) SMA20 5세션 전 = 144.5, 기울기 = 149.5/144.5-1", approx(F.sma_slope(v, 20, 5).value, 149.5 / 144.5 - 1))
check("3-3) ret20 = 159/139-1", approx(F.ret(v, 20).value, 159 / 139 - 1))
check("3-4) ATR14: 폭 2, 전일 대비 1 상승 → TR=max(2,|160.. |)=2", approx(F.atr(v).value, 2.0))
jump = bars_from(S[:30], [100.0] * 29 + [110.0])            # 마지막 날 갭 상승
va = SeriesView(jump, S, S[29])
check("3-5) 갭은 |H-전일C|로 TR에 반영: (13×2 + 11)/14", approx(F.atr(va).value, (13 * 2 + 11) / 14))
check("3-6) extension20 = (C-SMA20)/ATR14", approx(F.extension(v).value, (159 - 149.5) / 2.0))
flat = [ResearchBar(d, 10.0, 10.0, 10.0, 10.0, 0, None) for d in S[:40]]
vf = SeriesView(flat, S, S[39])
check("3-7) 고저 같으면 close_location UNKNOWN(ZERO_RANGE)", F.close_location(vf).reason == "ZERO_RANGE")
check("3-8) 거래량 평균 0이면 volume_ratio UNKNOWN", not F.volume_ratio(vf).ok)
check("3-9) ATR 0이면 extension UNKNOWN(분모 0)", "ZERO_DENOMINATOR" in F.extension(vf).reason)
tvb = bars_from(S[:25], [100.0] * 25, tv=5_000_000_000)
check("3-10) 거래대금 평균(원)", F.trade_value_avg(SeriesView(tvb, S, S[24]), 20).value == 5e9)
tvb2 = tvb[:10] + [ResearchBar(S[10], 100, 101, 99, 100, 1000, None)] + tvb[11:]
check("3-11) 거래대금 하나라도 없으면 UNKNOWN (종가×거래량으로 채우지 않음)",
      F.trade_value_avg(SeriesView(tvb2, S, S[24]), 20).reason.startswith("TRADE_VALUE_MISSING"))

# ── 4. 돌파 직전 수축: t 당일 제외 ───────────────────────────
base = bars_from(S[:40], [100.0] * 40, rng=4.0, vol=2000)
for i in range(34, 39):                                     # t-5..t-1: 폭 2, 거래량 1000
    base[i] = ResearchBar(S[i], 100.0, 101.0, 99.0, 100.0, 1000)
vt = SeriesView(base, S, S[39])
check("4-1) tr_contraction = 2/4 = 0.5", approx(F.tr_contraction(vt).value, 0.5))
check("4-2) volume_dryup = 1000/2000 = 0.5", approx(F.volume_dryup(vt).value, 0.5))
spiked = base[:39] + [ResearchBar(S[39], 100.0, 130.0, 95.0, 128.0, 999_999)]
vs = SeriesView(spiked, S, S[39])
check("4-3) t의 급등·거래량 급증은 수축 지표를 바꾸지 않음",
      F.tr_contraction(vs).value == F.tr_contraction(vt).value and F.volume_dryup(vs).value == F.volume_dryup(vt).value)

# ── 5. 52주·좁은 일봉·return_atr ─────────────────────────────
long = bars_from(S[:252], [100.0 + (i % 7) for i in range(252)])
check("5-1) 252개 있으면 high252 계산", F.high252_ratio(SeriesView(long, S, S[251])).ok)
check("5-2) 251개면 UNKNOWN (짧은 이력으로 52주 고점 표시 안 함)",
      F.high252_ratio(SeriesView(long[1:], S, S[251])).reason == "INSUFFICIENT_HISTORY")
nr = bars_from(S[:10], [100.0] * 10, rng=4.0)
nr[9] = ResearchBar(S[9], 100.0, 101.0, 99.0, 100.0, 1000)
check("5-3) narrow_range7: 직전 6개보다 좁으면 1", F.narrow_range7(SeriesView(nr, S, S[9])).value == 1.0)
check("5-4) 넓으면 0", F.narrow_range7(SeriesView(bars_from(S[:10], [100.0] * 9 + [100.0], rng=4.0)[:9] +
                                                  [ResearchBar(S[9], 100, 105, 95, 100, 1)], S, S[9])).value == 0.0)
check("5-5) return_atr = (C-C전일)/ATR14[t-1] = 1/2", approx(F.return_atr(v).value, 0.5))

# ── 6. EMA: 시작값·준비 구간·공백 ─────────────────────────────
e = F.ema(SeriesView(bars_from(S[:120], [50.0] * 120), S, S[119]), 20)
check("6-1) EMA20: 6N=120봉이면 유효, 상수 입력 → 50", e.value.ok and approx(e.value.value, 50.0)
      and e.seed_date == S[19] and e.updates_after_seed == 100)
e2 = F.ema(SeriesView(bars_from(S[:119], [50.0] * 119), S, S[118]), 20)
check("6-2) 119봉이면 준비 구간 미달 UNKNOWN", not e2.value.ok and e2.value.reason.startswith("EMA_WARMUP"))
cl = [10.0, 20.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0, 30.0]   # N=2 → 12봉
ev = F.ema(SeriesView(bars_from(S[:12], cl), S, S[11]), 2).value.value
exp = 15.0                                  # seed = (10+20)/2
for c in cl[2:]:
    exp = (2 / 3) * c + (1 / 3) * exp
check("6-3) EMA2 손계산 일치 (seed=첫 2개 평균)", approx(ev, exp))
gb = bars_from(S[:200], [50.0] * 200)
gb = [b for b in gb if b.date != S[150]]                   # 150번째 세션 공백
eg = F.ema(SeriesView(gb, S, S[199]), 20)
check("6-4) 공백 뒤 49봉뿐 → 공백 이후 구간만 쓰므로 준비 부족 UNKNOWN", not eg.value.ok and eg.seed_date == S[170])
long2 = bars_from(S[:300], [50.0] * 300)
check("6-5) 같은 입력이면 매번 같은 값(재시드 없음)", F.ema(SeriesView(long2, S, S[299]), 20).value.value ==
      F.ema(SeriesView(long2, S, S[299]), 20).value.value)

# ── 7. 시장 환경 ─────────────────────────────────────────────
up = bars_from(S[:130], [2500 * 1.002 ** i for i in range(130)])
down = bars_from(S[:130], [2500 * 0.998 ** i for i in range(130)])
check("7-1) 상승 지수 → RISK_ON", classify_market(SeriesView(up, S, S[129])).state == RISK_ON)
check("7-2) 하락 지수 → RISK_OFF", classify_market(SeriesView(down, S, S[129])).state == RISK_OFF)
mixed = bars_from(S[:130], [2500.0] * 130)
check("7-3) 횡보(MA60 기울기 0, 종가=MA) → MIXED", classify_market(SeriesView(mixed, S, S[129])).state == MIXED)
crash = bars_from(S[:130], [2500 * 1.002 ** i for i in range(125)] + [2500 * 1.002 ** 125 * 0.85] * 5)
check("7-3b) 상승 후 급락(MA120 아래·MA60 하락 전환) → RISK_OFF 우선",
      classify_market(SeriesView(crash, S, S[129])).state == RISK_OFF)
check("7-4) 기준일 지수 봉 없음 → UNKNOWN", classify_market(SeriesView(up[:129], S, S[129])).state == UNKNOWN)
check("7-5) 이력 부족 → UNKNOWN", classify_market(SeriesView(up[:100], S, S[99])).state == UNKNOWN)

# ── 8. 주봉 (w2: 예정 일정·사용 가능 시각) ──────────────────────
from datetime import datetime as DT, time as TM  # noqa: E402

from domain.research.weekly import (  # noqa: E402
    ExplicitWeekSchedule, ScheduleCoverageError, weekly_bars, weekly_trend,
)

fri_holiday = date(2025, 3, 7)                              # 금요일 휴장(확인된 일정)
WS = weekdays(date(2025, 1, 6), 300, skip={fri_holiday})
KNOWN = date(2026, 12, 31)
FULL = weekdays(date(2025, 1, 6), 600, skip={fri_holiday})
FULL = [d for d in FULL if d <= KNOWN]
SCHED = ExplicitWeekSchedule(FULL, [fri_holiday], KNOWN)
wb = bars_from(WS, [100.0 + i for i in range(300)])
wed = date(2025, 3, 5)
weeks_wed = weekly_bars(wb, SCHED, DT.combine(wed, TM(20, 0)))
check("8-1) 수요일 저녁: 월·화·수 봉이 다 있어도 진행 중인 주는 제외 (마지막 = 직전 주 금요일)",
      weeks_wed[-1].week_end_session == date(2025, 2, 28))
try:
    ExplicitWeekSchedule([d for d in FULL if d <= wed], [fri_holiday], KNOWN)
    truncated_ok = True
except ScheduleCoverageError:
    truncated_ok = False
check("8-2) [A13-R2 재현] 수요일까지 잘린 세션 목록을 연말까지 안다고 넘기면 오류(수요일을 주 마지막으로 오인 불가)",
      not truncated_ok)
thu = date(2025, 3, 6)
at = lambda h, m=0: DT.combine(thu, TM(h, m))
w_close = weekly_bars(wb, SCHED, at(16, 0))
check("8-3) 금요일 휴장 주: 목요일 마감+30분(16:00)에 완성·사용 가능, 기록 필드 분리",
      w_close[-1].week_end_session == thu and w_close[-1].complete
      and w_close[-1].session_closed_at == at(15, 30) and w_close[-1].available_at == at(16, 0)
      and w_close[-1].availability_basis == "ASSUMED_DELAY")
check("8-4) 목요일 장중(14:00)에는 그 주 제외", weekly_bars(wb, SCHED, at(14, 0))[-1].week_end_session == date(2025, 2, 28))
check("8-5) 목요일 마감 직후(15:40, 데이터 확보 여유 전)도 제외 — 장중과 같은 결과",
      weekly_bars(wb, SCHED, at(15, 40))[-1].week_end_session == date(2025, 2, 28))
obs = weekly_bars(wb, SCHED, at(17, 0), data_ready_at={thu: at(16, 45)})
check("8-6) 실제 확보 시각을 주면 OBSERVED(16:45)로 기록, 그 전(16:30)이면 제외",
      obs[-1].available_at == at(16, 45) and obs[-1].availability_basis == "OBSERVED"
      and weekly_bars(wb, SCHED, at(16, 30), data_ready_at={thu: at(16, 45)})[-1].week_end_session != thu)
short = ExplicitWeekSchedule([d for d in FULL if d <= date(2025, 3, 5)], [], date(2025, 3, 2))
check("8-7) 일정이 확정되지 않은 주는 완성으로 보지 않음",
      all(w.week_start < date(2025, 3, 3) for w in weekly_bars(wb, short, at(20, 0)) if w.complete))
wk = [w for w in w_close if w.week_start == date(2025, 2, 24)][0]
check("8-8) 주봉 OHLC: 시가=첫 세션, 종가=마지막 세션, 거래량 합",
      wk.open == wb[WS.index(date(2025, 2, 24))].open and wk.close == wb[WS.index(date(2025, 2, 28))].close
      and wk.volume == 5000)
holed = [b for b in wb if b.date != date(2025, 2, 26)]
wh = weekly_bars(holed, SCHED, at(16, 0))
check("8-9) 일봉 빠진 주는 불완전(건너뛰어 압축하지 않음)",
      any(w.week_start == date(2025, 2, 24) and not w.complete for w in wh))
t_end = WS[-1]
as_end = DT.combine(t_end, TM(20, 0))
full_weeks = weekly_bars(wb, SCHED, as_end)
tr = weekly_trend(full_weeks)
check("8-10) 상승 주봉 → UP_PROXY, 기준 주 사용 가능 시각 기록", tr.state == UP_PROXY and tr.slope4w > 0
      and tr.available_at is not None)
dn = bars_from(WS, [1000.0 - i for i in range(300)])
check("8-11) 하락 주봉 → DOWN_PROXY", weekly_trend(weekly_bars(dn, SCHED, as_end)).state == DOWN_PROXY)
check("8-12) 34주 안에 불완전 주 → UNKNOWN", weekly_trend(weekly_bars(
    [b for b in wb if b.date != WS[-10]], SCHED, as_end)).state == "UNKNOWN")
sma150 = F.sma(SeriesView(wb, WS, t_end), 150).value
check("8-13) SMA30W와 일봉 SMA150은 다른 표본(값이 다름)", tr.sma30w is not None and tr.sma30w != sma150)
check("8-14) 주봉 34개 미만 → UNKNOWN", weekly_trend(full_weeks[:33]).state == "UNKNOWN")
from domain.research.weekly import CalendarWeekSchedule  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

cal_s = CalendarWeekSchedule(TradingCalendar.load())
check("8-15) 실제 달력 어댑터: 2026년 추석 주(9/21~25)는 월·화·수 세션, 목·금 휴장 → 마지막 세션 수요일",
      cal_s.sessions_in_week(date(2026, 9, 21))[-1] == date(2026, 9, 23))
check("8-16) 달력이 다루지 않는 해는 일정 불명(None)", cal_s.sessions_in_week(date(2019, 1, 7)) is None)
check("8-17) 특수 운영일 마감 시각 반영(2026-01-02 15:30)", cal_s.close_at(date(2026, 1, 2)) == DT(2026, 1, 2, 15, 30))

# ── 9. A13 경계 (R1·R3·R5) ───────────────────────────────────
import json  # noqa: E402

from domain.research.series import ResearchBarError as RBE  # noqa: E402

dup = S[:20] + [S[19]] + S[20:40]
try:
    SeriesView(bars_from(S[:40], [100.0] * 40), dup, S[39])
    dup_ok = True
except RBE:
    dup_ok = False
check("9-1) [A13-R3] 중복 세션 목록은 오류(조용히 고치지 않음)", not dup_ok)
try:
    SeriesView(bars_from(S[:40], [100.0] * 40), list(reversed(S[:40])), S[39])
    rev_ok = True
except RBE:
    rev_ok = False
check("9-2) 역순 세션 목록도 오류", not rev_ok)
vv = SeriesView(bars_from(S[:40], [100.0] * 40), S, S[39])
check("9-3) 창 인자 음수·0 → BAD_WINDOW", vv.window(0)[1] == "BAD_WINDOW" and vv.window(5, -1)[1] == "BAD_WINDOW")
bad_vals = []
for val in (float("inf"), float("nan"), -1.0):
    try:
        ResearchBar(S[0], val, val, val, val, 1)
        bad_vals.append(False)
    except RBE:
        bad_vals.append(True)
check("9-4) [A13-R5] +무한대·NaN·음수 가격 봉 거부", all(bad_vals))
bool_ok = []
for kw in ({"volume": True}, {"trade_value": True}):
    try:
        ResearchBar(S[0], 1.0, 1.0, 1.0, 1.0, **({"volume": 1} | kw))
        bool_ok.append(False)
    except RBE:
        bool_ok.append(True)
check("9-5) 거래량·거래대금에 bool 거부", all(bool_ok))
check("9-6) 정상 주봉 추세 결과는 NaN 없이 표준 JSON 직렬화", bool(json.dumps(tr.to_dict(), allow_nan=False)))
iv_future = SeriesView(bars_from(S[:120], [2500.0 + i for i in range(120)]), S, S[119])
sv_past = SeriesView(bars_from(S[:120], [100.0 + i for i in range(120)]), S, S[99])
check("9-7) [A13-R1] 종목·지수 기준일이 다르면 RS UNKNOWN(AS_OF_MISMATCH)",
      F.rs(sv_past, iv_future, 60).reason.startswith("AS_OF_MISMATCH"))
S_missing = [d for d in S if d != S[70]]
iv_misaligned = SeriesView(bars_from(S_missing[:119], [2500.0 + i for i in range(119)]), S_missing, S[119])
sv_same = SeriesView(bars_from(S[:120], [100.0 + i for i in range(120)]), S, S[119])
check("9-8) 기준일은 같아도 수익률 구간 날짜가 다르면 UNKNOWN(SESSION_ALIGNMENT_MISMATCH)",
      F.rs(sv_same, iv_misaligned, 60).reason == "SESSION_ALIGNMENT_MISMATCH")
iv_same = SeriesView(bars_from(S[:120], [2500.0 + i for i in range(120)]), S, S[119])
check("9-9) 같은 기준일·같은 날짜 구간이면 정상 계산",
      approx(F.rs(sv_same, iv_same, 60).value, (219 / 159 - 1) - (2619 / 2559 - 1)))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
