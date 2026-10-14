# -*- coding: utf-8 -*-
"""M3 B: 후보 승인(읽기 전용) + S1 진입 전략 그림자 운영 — 관리자 실제 흐름(가짜 키움)으로 만든 DB를 읽음.

    python test_s1_shadow.py

임시 폴더만, 실제 API·주문 없음.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

_HEAD = (Path(__file__).resolve().parent / "test_watch_daemon.py").read_text(encoding="utf-8").split("# ── 1. 일정")[0]
exec(compile(_HEAD, "test_watch_daemon_head", "exec"))        # 관리자 시험 장치(가짜 시계·가짜 키움·env·daemon)

from datetime import time as dtime  # noqa: E402  (머리말의 import time과 겹치지 않게)
from app.candidates import ERROR as B_ERROR, READY as B_READY, WAITING as B_WAITING, CandidateReader  # noqa: E402
from app.main import StartupBlockedError, build_session_runner, run_session  # noqa: E402
from domain.market_data.tick_size import floor_to_tick  # noqa: E402
from infra.storage.signal_keys import SignalKeyStore, keys_path  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402

D108 = date(2026, 10, 8)


def flow(name: str, *, after_close_min=300, mutate=None):
    """10/7 20:30 마감 준비 → 10/8 08:40 입력 재확인 → 09:05 개장 확인까지 관리자 실제 흐름."""
    e = env(name, datetime(2026, 10, 7, 21, 0), [S_5930])
    d = daemon(e, after_close=timedelta(minutes=after_close_min))
    d.dstore.set_meta("since", "2026-10-07")
    d.dstore.conn.execute("DELETE FROM task")
    for _ in range(3):
        d.tick()
    if mutate:
        mutate(e)
    at(e, datetime(2026, 10, 8, 8, 40))
    d.tick()
    at(e, datetime(2026, 10, 8, 9, 5, 30))
    d.tick()
    d.stop()
    return e


def watch_dir(e) -> Path:
    return e["paths"].daemon_db.parent


def tree(p: Path) -> dict:
    return {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in sorted(p.glob("*.sqlite3"))}


# 1. 정상 흐름 → 승인
E1 = flow("ok")
w1 = watch_dir(E1)
before = tree(w1)
reader = CandidateReader(w1, CAL)
b1 = reader.read(D108, datetime(2026, 10, 8, 9, 6))
a1 = b1.approved[0] if b1.approved else None
check("1-1) 20:30 스캔(완성 기준 300분)·08:40 입력 재확인 STABLE·09:05 개장 확인 WITHIN_CAP → 005930 승인, 지정가 = 진입 상한 "
      "호가 내림, 참고 손절가보다 큼",
      b1.state == B_READY and a1 is not None and a1.symbol == "005930" and a1.limit_price == floor_to_tick(a1.entry_cap)
      and a1.limit_price > a1.stop_ref and a1.observed_outcome == "WITHIN_CAP" and b1.rejected == {})
check("1-2) 읽기 전용: 관리자 DB 세 개의 바이트 그대로(만들기·쓰기·스키마 변경 없음)", tree(w1) == before)

# 2. 거부 경로
E2 = flow("provisional", after_close_min=160)
b2 = CandidateReader(watch_dir(E2), CAL).read(D108, datetime(2026, 10, 8, 9, 6))
check("2-1) 18:10(160분) 스캔은 잠정 관찰 — 다른 단계가 모두 통과해도 승인 안 함(PROVISIONAL_SCAN)",
      b2.state == B_READY and not b2.approved and any(r.startswith("PROVISIONAL_SCAN") for r in b2.rejected["005930"]))


def change_bar(e):
    old = e["fake"].series["005930"][PASS_DAY]
    e["fake"].series["005930"][PASS_DAY] = RawBar(PASS_DAY, old.open_raw, old.high_raw, old.low_raw,
                                                  old.close_raw + 10, old.volume, old.trade_value_raw, "")


E3 = flow("changed", mutate=change_bar)
b3 = CandidateReader(watch_dir(E3), CAL).read(D108, datetime(2026, 10, 8, 9, 6))
check("2-2) 08:40 재확인에서 입력이 바뀌면(INPUT_CHANGED) 승인 안 함",
      not b3.approved and "INPUT_CHECK_STOCK:INPUT_CHANGED" in b3.rejected.get("005930", []))

w4 = TMP / "copy_above"
w4.mkdir()
for f in w1.glob("*.sqlite3"):
    (w4 / f.name).write_bytes(f.read_bytes())
with sqlite3.connect(w4 / "watch_open.sqlite3") as cx:
    cx.execute("UPDATE price_check SET outcome='ABOVE_CAP'")
b4 = CandidateReader(w4, CAL).read(D108, datetime(2026, 10, 8, 9, 6))
with sqlite3.connect(w4 / "daemon.sqlite3") as cx:
    cx.execute("UPDATE candidate_gate SET eligible=0, reasons_json='[\"RISK_FLAGS\"]' WHERE stage='OPEN'")
    cx.execute("DELETE FROM input_check WHERE role='INDEX'")
b5 = CandidateReader(w4, CAL).read(D108, datetime(2026, 10, 8, 9, 6))
check("2-3) 개장 가격이 상한 위(ABOVE_CAP)·OPEN 게이트 제외·지수 재확인 없음 → 각각 사유로 승인 안 함",
      not b4.approved and "OPEN_PRICE:ABOVE_CAP" in b4.rejected["005930"]
      and "OPEN_GATE:RISK_FLAGS" in b5.rejected["005930"] and "INPUT_CHECK_INDEX:NONE" in b5.rejected["005930"])

E6 = env("waiting", datetime(2026, 10, 7, 21, 0), [S_5930])
d6 = daemon(E6, after_close=timedelta(minutes=300))
d6.dstore.set_meta("since", "2026-10-07")
d6.dstore.conn.execute("DELETE FROM task")
for _ in range(3):
    d6.tick()
d6.stop()
b6 = CandidateReader(watch_dir(E6), CAL).read(D108, datetime(2026, 10, 8, 9, 1))
b7 = CandidateReader(TMP / "nowhere", CAL).read(D108, datetime(2026, 10, 8, 9, 6))
check("2-4) 개장 확인 전이면 WAITING(다시 읽음), 관리자 DB가 없으면 ERROR(신규 매수만 보류) — 폴더·파일을 만들지 않음",
      b6.state == B_WAITING and not b6.approved and b7.state == B_ERROR and not (TMP / "nowhere").exists())


# 3. 그림자 실행 — 실행기 전체 경로
class Quote:
    def __init__(self, price, ask):
        self.price, self.ask, self.calls = price, ask, 0

    def get_prices(self, syms):
        return {s: self.price for s in syms}

    def get_quote(self, sym):
        self.calls += 1
        return self.price, self.ask


class Clock2:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def shadow_settings(name, mode="shadow"):
    d = TMP / name
    d.mkdir()
    s = build_minimal_settings(str(d))
    return dataclasses.replace(s, swing_strategy=dataclasses.replace(s.swing_strategy, mode=mode,
                                                                      watch_dir=str(w1)))


def run_day(s, broker, quote, start=dtime(9, 0), end=dtime(9, 40)):
    clk = Clock2(datetime.combine(D108, start))
    lg = Mock()
    summ = run_session(s, broker, lg, clock=clk, sleep=clk.sleep, quote_source=quote,
                       should_stop=lambda: clk.t >= datetime.combine(D108, end))
    msgs = [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
    return summ, msgs


def jl(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


s8 = shadow_settings("shadow_ok")
br8 = ScriptedBroker()
q8 = Quote(int(a1.limit_price - 100), int(a1.limit_price))
sm8, m8 = run_day(s8, br8, q8)
sh8 = jl(Path(s8.storage.state_file).with_name("shadow_intents.jsonl"))
dec8 = jl(Path(s8.storage.state_file).with_name("strategy_decisions.jsonl"))
check("3-1) 그림자: 09:05 이후 승인 후보 005930 1주 지정가(상한 호가 내림) 의도 → 안전 한도 통과 → 전송하지 않고 기록 "
      "(브로커 주문 0, 그림자 의도 1, 요약에 모드 표시)",
      br8.place_calls == [] and len(sh8) == 1 and sh8[0]["would_send"] is True and sh8[0]["quantity"] == 1
      and sh8[0]["order_type"] == "limit" and sh8[0]["limit_price"] == a1.limit_price
      and sh8[0]["context"]["signal_id"] == a1.signal_id and sm8.shadow_intents == 1
      and any("그림자 의도 1건" in ln for ln in sm8.lines()) and any("[SHADOW]" in m for m in m8))
check("3-2) 판단 기록: 후보 승인 상태(READY·승인 목록)와 의도가 strategy_decisions에 남음, 09:05 전에는 후보를 읽지 않음",
      [d["event"] for d in dec8][:2] == ["CANDIDATES_READY", "INTENT"] and dec8[0]["approved"] == ["005930"]
      and dec8[0]["at"] >= "2026-10-08T09:05:00")
sm8b, _ = run_day(s8, ScriptedBroker(), q8, start=dtime(9, 10))
check("3-3) 같은 신호는 한 번만 — 같은 날 실행기를 다시 켜도 그림자 신호 키가 있어 의도 없음",
      sm8b.shadow_intents == 0 and len(jl(Path(s8.storage.state_file).with_name("shadow_intents.jsonl"))) == 1
      and len(SignalKeyStore(keys_path(s8.storage.state_file, "shadow")).keys()) == 1
      and not SignalKeyStore(keys_path(s8.storage.state_file, "paper")).keys())

s9 = shadow_settings("shadow_wait")
q9 = Quote(int(a1.limit_price + 1000), int(a1.limit_price + 1000))
sm9, _ = run_day(s9, ScriptedBroker(), q9)
dec9 = jl(Path(s9.storage.state_file).with_name("strategy_decisions.jsonl"))
check("3-4) 주문 직전 현재가가 상한 위·매도호가가 지정가 위면 의도 없음(WAIT_PRICE 사유 기록, 같은 사유 반복 기록 없음), "
      "09:30 이후 다시 보지 않음",
      sm9.shadow_intents == 0 and [d["event"] for d in dec9].count("WAIT_PRICE") == 1
      and set(dec9[-1]["reasons"]) == {"PRICE_ABOVE_CAP", "ASK_ABOVE_LIMIT"} and q9.calls < 30
      and not SignalKeyStore(keys_path(s9.storage.state_file, "shadow")).keys())

s10 = shadow_settings("shadow_held")
br10 = ScriptedBroker()
br10.positions["000660"] = 1
from domain.position.fill_event import FillEvent  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402
FillLedgerStore(s10.storage.fill_ledger_file).append(FillEvent("h", "BUY", "000660", 1, 10000, "BROKER_FILL",
                                                               date(2026, 10, 7), datetime(2026, 10, 7, 10, 0)))
sm10, _ = run_day(s10, br10, Quote(int(a1.limit_price - 100), int(a1.limit_price)))
dec10 = jl(Path(s10.storage.state_file).with_name("strategy_decisions.jsonl"))
check("3-5) 최대 보유 1: 이미 보유가 있으면 신규 의도 없음(MAX_POSITION 기록)",
      sm10.shadow_intents == 0 and any(d["event"] == "MAX_POSITION" for d in dec10))

s11 = shadow_settings("paper", mode="paper")
try:
    build_session_runner(s11, ScriptedBroker(), Mock(), use_settings_strategy=True)
    p11 = None
except StartupBlockedError as exc:
    p11 = str(exc)
s12 = shadow_settings("off", mode="off")
r12, _ = build_session_runner(s12, ScriptedBroker(), Mock(), use_settings_strategy=True)
r13, _ = build_session_runner(s8, ScriptedBroker(), Mock())            # M1 도구 경로(설정 전략 안 씀)
check("3-6) paper 모드는 사전 점검 실패(가짜 브로커·허용 종목 없음)면 기동 거부, off는 NullStrategy, "
      "M1 수동 도구 경로는 설정이 shadow여도 전략·그림자 없음(전송 경로 유지)",
      p11 and "기동 거부" in p11 and "allowed_symbols" in p11 and r12.strategy.strategy_id == "null" and r12.send_orders
      and r13.strategy.strategy_id == "null" and r13.send_orders)

# 4. C1 승인 보완 (GPT 검토 R1~R4) — 정상 흐름 DB 사본을 바꿔 각 근거를 끊음
def copy_ok(name: str) -> Path:
    w = TMP / name
    w.mkdir()
    for f in w1.glob("*.sqlite3"):
        (w / f.name).write_bytes(f.read_bytes())
    return w


def sql(w: Path, db: str, q: str, args=()):
    with sqlite3.connect(w / db) as cx:
        cx.execute(q, args)


def why(w: Path) -> tuple:
    b = CandidateReader(w, CAL).read(D108, datetime(2026, 10, 8, 9, 6))
    return b.state, [a.symbol for a in b.approved], b.rejected.get("005930", []), b.note


wA = copy_ok("c1_gate_run")
sql(wA, "daemon.sqlite3", "UPDATE candidate_gate SET scan_run_id='WRONG_RUN'")
stA, apA, rjA, _ = why(wA)
check("C1-R2a) 게이트의 스캔 실행 ID가 후보와 다르면 그 게이트를 쓰지 않음 → SCAN·OPEN 게이트 없음으로 승인 안 함(이전 코드: 승인 1)",
      apA == [] and "SCAN_GATE:NONE" in rjA and "OPEN_GATE:NONE" in rjA)
wB = copy_ok("c1_task_contract")
sql(wB, "daemon.sqlite3", "UPDATE task SET contract_hash='other' WHERE kind='OPEN_CHECK'")
wC = copy_ok("c1_open_at")
sql(wC, "watch_open.sqlite3", "UPDATE candidate_set SET open_at='2026-10-08T08:00:00'")
wD = copy_ok("c1_ic_run")
sql(wD, "daemon.sqlite3", "UPDATE input_check SET scan_run_id='WRONG_RUN'")
check("C1-R2b) 개장 확인 작업 계약 ≠ 후보 목록 계약, 후보 목록 개장 시각 ≠ 달력 → ERROR(승인 없음). 입력 재확인이 다른 스캔 실행이면 없음으로",
      why(wB)[0] == B_ERROR and "계약" in why(wB)[3] and why(wC)[0] == B_ERROR and "달력" in why(wC)[3]
      and "INPUT_CHECK_STOCK:NONE" in why(wD)[2])
wE = copy_ok("c1_late_ic")
sql(wE, "daemon.sqlite3", "UPDATE input_check SET checked_at='2026-10-08T08:58:00', committed_at='2026-10-08T08:58:01'")
wE2 = copy_ok("c1_early_ic")
sql(wE2, "daemon.sqlite3", "UPDATE input_check SET checked_at='2026-10-08T08:30:00', committed_at='2026-10-08T08:30:01'")
check("C1-R3) 입력 재확인의 확인·저장이 08:40~08:55 밖(08:58·08:30)이면 STABLE이어도 승인 안 함(이전 코드: 08:58 승인)",
      why(wE)[1] == [] and any(r.startswith("INPUT_CHECK_STOCK:OUTSIDE_WINDOW") for r in why(wE)[2])
      and why(wE2)[1] == [] and any("OUTSIDE_WINDOW" in r for r in why(wE2)[2]))
wF = copy_ok("c1_late_price")
sql(wF, "watch_open.sqlite3", "UPDATE price_check SET timing='LATE_RESPONSE'")
wG = copy_ok("c1_kind")
sql(wG, "watch_open.sqlite3", "UPDATE price_check SET check_kind='OPEN+10m'")
wH = copy_ok("c1_target")
sql(wH, "watch_open.sqlite3", "UPDATE price_check SET target_at='2026-10-08T09:15:00'")
check("C1-R4) 개장 가격: LATE_RESPONSE 거부(이전 코드: 승인), 작업의 확인 종류(OPEN+5m)가 아닌 관찰은 쓰지 않음, 목표 시각 ≠ 달력 개장+5분 거부",
      "OPEN_PRICE:TIMING_LATE_RESPONSE" in why(wF)[2] and why(wF)[1] == []
      and any(r.startswith("OPEN_PRICE:NONE") for r in why(wG)[2])
      and any(r.startswith("OPEN_PRICE:TIME_MISMATCH") for r in why(wH)[2]))
check("C1-0) 정상 DB는 여전히 승인(보완이 정상 흐름을 막지 않음)", why(w1)[1] == ["005930"])

# R1 — 전략: 승인을 폴링마다 다시 읽음
from app.s1_strategy import S1EntryStrategy  # noqa: E402
from domain.models import AccountBalance  # noqa: E402
from domain.strategy.interface import TickContext  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402


class Seq:
    def __init__(self, books):
        self.books, self.n = list(books), 0

    def __call__(self, day, now):
        self.n += 1
        return self.books[min(self.n - 1, len(self.books) - 1)]


import copy as _copy  # noqa: E402
b_err = _copy.deepcopy(b1)
b_err.state, b_err.approved, b_err.note = B_ERROR, [], "관리자 기록 읽기 실패(시험)"
seq = Seq([b1, b_err, b_err])
prices = iter([(int(a1.entry_cap + 1000), int(a1.entry_cap + 1000)), (a1.limit_price - 100, a1.limit_price),
               (a1.limit_price - 100, a1.limit_price)])
keys_r1 = SignalKeyStore(TMP / "r1_keys.jsonl")
dec_r1: list = []
strat = S1EntryStrategy(mode="shadow", scope="t", candidates=seq, quote=lambda sym: next(prices), keys=keys_r1,
                        decisions=dec_r1.append, entry_start=dtime(9, 5), entry_end=dtime(9, 30))


def ctx_at(t):
    return TickContext(now=datetime.combine(D108, t), phase=MarketPhase.REGULAR, trade_date=D108,
                       balance=AccountBalance(10_000_000, 10_000_000, []), positions={}, metas={},
                       blocked_symbols=frozenset(), orders_in_flight=False)


out_r1 = [strat.on_tick(ctx_at(dtime(9, 6))), strat.on_tick(ctx_at(dtime(9, 7))), strat.on_tick(ctx_at(dtime(9, 8)))]
check("C1-R1) 첫 폴링 승인 + 가격 상한 위(보류) → 다음 폴링에 승인 조회가 ERROR면 가격이 정상이어도 의도 없음, 조회는 폴링마다(3회), "
      "승인 상태 변화가 판단 기록에 남음(이전 코드: 의도 1·조회 1)",
      out_r1 == [[], [], []] and seq.n == 3 and not keys_r1.keys()
      and [d["event"] for d in dec_r1 if d["topic"] == "book"] == ["CANDIDATES_READY", "CANDIDATES_ERROR"])

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
