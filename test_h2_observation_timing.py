# -*- coding: utf-8 -*-
"""H2 (GPT 10/27 검토): H1 관찰이 주문을 늦추지 않음 · 전송 직전 실제 시각 재검사 · 관찰 시각은 실제 수신 시각.

    python test_h2_observation_timing.py

가짜 시계(조회할 때마다 시간이 흐르게)·가짜 브로커, 임시 폴더만. 실제 API·주문 없음.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.candidates import ApprovedCandidate, CandidateBook  # noqa: E402
from app.entry_observation import OBS_FIRST_OK, OBS_QUOTE, EntryObserver, summarize  # noqa: E402
from app.main import build_session_runner  # noqa: E402
from app.s1_strategy import S1EntryStrategy  # noqa: E402
from domain.strategy.interface import OrderIntent  # noqa: E402
from infra.storage.signal_keys import SignalKeyStore  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402

results: list = []


def check(label, cond):
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp())
D = date(2026, 10, 28)          # 수요일 거래일


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += timedelta(seconds=sec)


def cand(sym, limit, stop, cap):
    return ApprovedCandidate(sym, f"N{sym}", f"sig-{sym}", "2026-10-27", "h", "run", cap, stop, limit, None, "")


A = cand("005930", 100_000, 95_000.0, 100_200.0)
B = cand("000660", 200_000, 180_000.0, 200_400.0)
BOOK = CandidateBook("READY", D.isoformat(), "2026-10-27", "h", [A, B])


def runner_with(name, clk, quote, *, on_place=None):
    s = build_minimal_settings(str(TMP / name))
    br = ScriptedBroker()
    if on_place:
        orig = br.place_order

        def place(order):
            on_place(order)
            return orig(order)
        br.place_order = place
    rec: list = []
    dec: list = []
    obs = EntryObserver(quote, rec.append, mode="paper", clock=clk)
    st = S1EntryStrategy(mode="paper", scope="t", candidates=lambda d, n: BOOK, quote=quote,
                         keys=SignalKeyStore(TMP / f"{name}_keys.jsonl"), decisions=dec.append,
                         entry_start=time(9, 5), entry_end=time(9, 30), allowed_symbols=("005930",), observer=obs)
    runner, ex = build_session_runner(s, br, Mock(), strategy=st, clock=clk, sleep=lambda x: clk.advance(x),
                                      should_stop=lambda: True)
    return runner, ex, br, rec, dec


# 1. GPT 재현: 09:29:55 매수 판단 + 느린 관찰(종목당 10초) — 주문은 관찰 전에 전송, 관찰 시각은 실제 수신 시각
clk = Clock(datetime.combine(D, time(9, 29, 55)))
events: list = []


def slow_quote(sym):
    events.append(("quote", sym, clk()))
    if sym != "005930":
        clk.advance(10)                       # 관찰 조회가 느림
    return {"005930": (99_900, 100_000), "000660": (199_000, 199_500)}[sym]


runner, ex, br, rec, dec = runner_with("slow", clk, slow_quote,
                                       on_place=lambda o: events.append(("place", o.symbol, clk())))
runner._tick(allow_orders=True)
ex.shutdown()
order = [(e[0], e[1]) for e in events]
check("1-1) 주문(005930 지정가 매수)은 관찰 조회(000660)보다 먼저 전송 — 이전 코드: 관찰이 끝난 뒤에야 의도가 실행기로 돌아감",
      ("place", "005930") in order and order.index(("place", "005930")) < order.index(("quote", "000660"))
      and br.place_calls and br.place_calls[0][0] == "005930")
q = [r for r in rec if r["event"] == OBS_QUOTE]
check("1-2) 관찰 기록 시각 = 실제 요청·수신 시각(09:29:55 요청 → 09:30:05 수신), 폴링 시작 시각 아님 · 마감 뒤 수신이라 "
      "조건을 만족해도 in_window=false, 최초 충족(OBS_FIRST_OK) 없음, 요약 충족 0·마감 뒤 1",
      len(q) == 1 and q[0]["requested_at"].endswith("09:29:55") and q[0]["received_at"].endswith("09:30:05")
      and q[0]["ok"] and q[0]["in_window"] is False and not [r for r in rec if r["event"] == OBS_FIRST_OK]
      and summarize(rec)[0]["ok_n"] == 0 and summarize(rec)[0]["late"] == 1)


# 2. 매수 판단이 늦어 실제 시각이 마감을 넘음 → 전송 직전 재검사로 차단(폴링 시작 시각은 09:29:50)
clk2 = Clock(datetime.combine(D, time(9, 29, 50)))


def late_quote(sym):
    clk2.advance(15)                          # 주문 직전 재확인 조회 자체가 늦음 → 09:30:05
    return 99_900, 100_000


runner, ex, br, rec, dec = runner_with("late", clk2, late_quote)
runner._tick(allow_orders=True)
ex.shutdown()
check("2-1) GPT 재현: 폴링 시작 09:29:50, 판단 끝 09:30:05 → 전송 직전 실제 시각으로 ENTRY_WINDOW_CLOSED 차단(주문 0) — "
      "이전 코드: 폴링 시작 시각으로 검사해 통과",
      not br.place_calls and runner.summary.denied_codes.get("ENTRY_WINDOW_CLOSED") == 1)

# 3. 마감 전이라도 판단이 30초 넘게 걸리면 STALE_DECISION
clk3 = Clock(datetime.combine(D, time(9, 10)))


def stale_quote(sym):
    clk3.advance(45)
    return 99_900, 100_000


runner, ex, br, rec, dec = runner_with("stale", clk3, stale_quote)
runner._tick(allow_orders=True)
ex.shutdown()
check("3-1) 폴링 시작 뒤 45초 지난 매수 판단(가격 재확인이 낡음) → STALE_DECISION 차단(주문 0), 그 신호는 다시 쓰지 않음",
      not br.place_calls and runner.summary.denied_codes.get("STALE_DECISION") == 1
      and any("STALE_DECISION" in str(d.get("status")) for d in dec))

# 4. 관찰 예산·마감 (U2: 경과는 monotonic — 시험은 가짜 monotonic을 조회가 밀어 줌)
clk4 = Clock(datetime.combine(D, time(9, 10)))
many = [cand(f"00{i:04d}", 10_000, 9_000.0, 10_020.0) for i in range(1, 6)]
calls: list = []
mono = [0.0]


def q4(sym):
    calls.append(sym)
    clk4.advance(6)
    mono[0] += 6
    return 9_990, 10_000


o4 = EntryObserver(q4, lambda r: None, clock=clk4, monotonic=lambda: mono[0])
n = o4.observe(D, many, window_end=datetime.combine(D, time(9, 30)))
clk5 = Clock(datetime.combine(D, time(9, 30)))
o5 = EntryObserver(q4, lambda r: None, clock=clk5)
n5 = o5.observe(D, many, window_end=datetime.combine(D, time(9, 30)))
check("4-1) 한 폴링 관찰 예산 15초(monotonic): 종목당 6초 걸리면 3종목 조회 뒤 멈춤 · 마감(09:30) 뒤엔 조회 안 함",
      n == 3 and calls[:3] == ["000001", "000002", "000003"] and n5 == 0)

# 4-2. U2(GPT 10/27 ③) — 이미 시작한 조회가 예산을 넘겨도 기다리지 않음(실제 최대 대기 = 예산)
import threading  # noqa: E402
import time as _rt  # noqa: E402
gate_ev = threading.Event()
slow_calls: list = []


def stuck(sym):
    slow_calls.append(sym)
    gate_ev.wait(5)                               # 응답이 늦는 조회(시험은 최대 5초 뒤 풀림)
    return 9_990, 10_000


rec6: list = []
clk6 = Clock(datetime.combine(D, time(9, 10)))
o6 = EntryObserver(stuck, rec6.append, clock=clk6, budget_sec=0.3)
t0 = _rt.monotonic()
n6 = o6.observe(D, many[:2], window_end=datetime.combine(D, time(9, 30)))
waited = _rt.monotonic() - t0
clk6.advance(60)
n6b = o6.observe(D, many[:2], window_end=datetime.combine(D, time(9, 30)))     # 남은 조회 진행 중 → 건너뜀
gate_ev.set()
for _ in range(50):
    if not o6.busy():
        break
    _rt.sleep(0.05)
clk6.advance(60)
n6c = o6.observe(D, many[:1], window_end=datetime.combine(D, time(9, 30)))
check("4-2) GPT 재현: 예산 0.3초인데 조회가 멈춤 → 약 0.3초만 기다리고 반환(이전: 조회가 끝날 때까지 대기), QUOTE_TIMEOUT 기록·"
      "늦은 값은 충족으로 안 셈, 남은 조회가 끝나기 전 다음 폴링은 관찰을 건너뜀(동시 조회 1개 이하), 끝난 뒤엔 다시 관찰",
      n6 == 0 and waited < 1.5 and rec6[0]["reasons"] == ["QUOTE_TIMEOUT"] and not rec6[0]["ok"]
      and n6b == 0 and slow_calls == ["000001", "000001"] and n6c == 1
      and not any(r.get("ok") and r["requested_at"].endswith("09:10:00") for r in rec6))

# 5. after_tick 예외는 실행기를 멈추지 않음
clk6 = Clock(datetime.combine(D, time(9, 10)))


class Boom:
    strategy_id = "x"

    def on_session_start(self, ctx):
        return None

    def on_tick(self, ctx):
        return [OrderIntent("005930", "BUY", 1, 99_900, reason="t", order_type="limit", limit_price=100_000)]

    def after_tick(self, ctx, clock):
        raise RuntimeError("observe boom")

    def on_session_end(self, ctx):
        return None


s6 = build_minimal_settings(str(TMP / "boom"))
br6 = ScriptedBroker()
lg6 = Mock()
r6, ex6 = build_session_runner(s6, br6, lg6, strategy=Boom(), clock=clk6, sleep=lambda x: None, should_stop=lambda: True)
r6._tick(allow_orders=True)
ex6.shutdown()
check("5-1) after_tick 예외 → 경고만, 그 폴링 주문은 이미 전송됨",
      br6.place_calls and any("after_tick" in str(c.args[0]) for c in lg6.warning.call_args_list))

# 6. PowerShell 앞자리 0 — add-many·backfill 안내
import contextlib  # noqa: E402
import io  # noqa: E402
import tools.watchlist as W  # noqa: E402
check("6-1) 앞자리 0이 빠진 코드(660·9150)는 PowerShell 따옴표 안내, 정상 6자리는 통과",
      "따옴표" in W.bad_code_hint(["660", "402340", "9150"]) and W.bad_code_hint(["000660", "402340"]) == "")
cfgp = TMP / "wl" / "watchlist.yaml"
cfgp.parent.mkdir(parents=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = W.main(["--config", str(cfgp), "--watch-db", str(TMP / "wl" / "w.sqlite3"), "--db", str(TMP / "wl" / "r.sqlite3"),
                 "add-many", "660,402340", "--interest", "--s1"])
check("6-2) 10/27 재현: add-many 660,402340 → 거부(설정 파일 만들지 않음), 따옴표 안내",
      rc == 2 and "따옴표" in buf.getvalue() and not cfgp.exists())

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
