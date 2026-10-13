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
check("3-6) paper 모드는 C 전에는 기동 거부, off는 NullStrategy, M1 수동 도구 경로는 설정이 shadow여도 전략·그림자 없음(전송 경로 유지)",
      p11 and "그림자(shadow)만" in p11 and r12.strategy.strategy_id == "null" and r12.send_orders
      and r13.strategy.strategy_id == "null" and r13.send_orders)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
