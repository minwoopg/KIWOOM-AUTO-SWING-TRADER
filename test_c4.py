# -*- coding: utf-8 -*-
"""C4 (GPT C 검토): 시작 기준 전 당일 거래(R1) · 청산 가격 실패(R2) · 시간 청산 forced · 청산 재전송 간격 · 저장 대기 근거 spool(R3).

    python test_c4.py

가짜 브로커(test_e1_exit의 Case)와 임시 폴더만. API·실제 주문 없음.
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

_HEAD = (Path(__file__).resolve().parent / "test_e1_exit.py").read_text(encoding="utf-8").split("# 1. 진입 → 체결 → 손절")[0]
exec(compile(_HEAD, "test_e1_exit_head", "exec"))   # Case·Px·SnapBroker·LimitBroker·check·TMP·CAND·BOOK·D1·CAL

from app.daily_risk import BLOCK_BASELINE, BLOCK_UNKNOWN, DailyRiskGuard  # noqa: E402
from app.main import build_daily_risk_guard, build_session_runner  # noqa: E402
from domain.models import AccountBalance, Position  # noqa: E402
from domain.position.fill_event import FillEvent  # noqa: E402
from domain.position.swing_state import PositionMeta  # noqa: E402
from domain.service.fill_evidence import FillDetail, record_for_event  # noqa: E402
from domain.service.fill_recorder import FillRecorder  # noqa: E402
from domain.service.lot_ledger import apply_events  # noqa: E402
from domain.service.trade_costs import CostProfile  # noqa: E402
from domain.strategy.interface import TickContext  # noqa: E402
from infra.storage.daily_risk import DailyRiskStore  # noqa: E402
from infra.storage.fill_evidence import EvidenceSpool, FillEvidenceStore  # noqa: E402
from infra.storage.runner_status import read_buy_halt  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402

D2 = date(2026, 10, 15)


def fe(eid, kind, qty, price, d=D1, sym="005930", oid="0000001", src="BROKER_FILL", hh=9, mm=6):
    return FillEvent(eid, kind, sym, qty, price, src if kind != "OPENING" else "BROKER_AVG", d,
                     datetime.combine(d, time(hh, mm)), order_id=oid if kind != "OPENING" else "")


def guard(name, events):
    d = TMP / name
    d.mkdir()
    ls = FillLedgerStore(d / "fill_ledger.jsonl")
    for e in events:
        ls.append(e)
    st = DailyRiskStore(d / "daily_risk.jsonl")
    g = DailyRiskGuard(store=st, ledger_store=ls, evidence_store=None, commands_dir=d / "commands", logger=Mock(),
                       limit=10_000, max_failures=1, profile=CostProfile(), enforce=True)
    return g, ls, st, d


def step(g, ls, now, prices, cash=100_000_000):
    led = apply_events(ls.load())
    return g.step(now, now.date(), led.positions(), AccountBalance(cash, cash, []), prices)


# 1. R1 — 시작 기준 전 당일 거래
g1, ls1, st1, d1 = guard("r1_buy", [fe("b1", "BUY", 1, 229_000)])
r1 = step(g1, ls1, datetime(2026, 10, 14, 9, 10), {"005930": 229_000})
check("R1-1) GPT 재현: 09:06 229,000원 1주 매수 뒤 첫 기준 생성 → 이전 코드 손익 −229,802·중단, 지금은 "
      "BASELINE_MISSING_WITH_TRADES로 신규 매수 보류, 기준을 추정해 쓰지 않음, 중단 파일 없음",
      r1 == BLOCK_BASELINE and st1.day("2026-10-14")[0] is None and not st1.day("2026-10-14")[2]
      and read_buy_halt(d1 / "commands")[0] == "NONE" and g1.last["state"] == BLOCK_BASELINE)
g2, ls2, st2, _ = guard("r1_sold", [fe("b0", "BUY", 1, 10_000, d=date(2026, 10, 13)),
                                     fe("s1", "SELL", 1, 10_500, oid="0000002")])
g3, ls3, st3, _ = guard("r1_opening", [fe("o1", "OPENING", 1, 50_000)])
check("R1-2) 매도 뒤 늦은 첫 기동(보유 0 — 매도 대금을 수익으로 더하지 않음), 오늘 OPENING 인수도 같은 보류",
      step(g2, ls2, datetime(2026, 10, 14, 10, 0), {}) == BLOCK_BASELINE
      and step(g3, ls3, datetime(2026, 10, 14, 10, 0), {"005930": 50_000}) == BLOCK_BASELINE
      and st2.day("2026-10-14")[0] is None and st3.day("2026-10-14")[0] is None)
g4, ls4, st4, _ = guard("r1_delay", [fe("b0", "BUY", 1, 10_000, d=date(2026, 10, 13))])
a4 = step(g4, ls4, datetime(2026, 10, 14, 9, 0), {})                      # 기준가 없음 → 기준 못 만듦
ls4.append(fe("s1", "SELL", 1, 9_800, oid="0000002", mm=1))               # 그사이 손절 체결
b4 = step(g4, ls4, datetime(2026, 10, 14, 9, 2), {})
c4 = step(g4, ls4, datetime(2026, 10, 15, 9, 0), {})                       # 다음 거래일 — 오늘 사건 없음
check("R1-3) 기준가 조회 실패로 기준 생성 지연 → 그사이 체결이 생기면 그날은 BASELINE 보류(지연 뒤 기준을 만들지 않음), "
      "다음 거래일엔 새 기준(보유 0)으로 정상",
      a4 == BLOCK_UNKNOWN and b4 == BLOCK_BASELINE and st4.day("2026-10-14")[0] is None
      and c4 is None and st4.day("2026-10-15")[0]["qty"] == {})
g5, ls5, st5, _ = guard("r1_normal", [])
n5a = step(g5, ls5, datetime(2026, 10, 14, 9, 0), {})
ls5.append(fe("b1", "BUY", 1, 229_000))
n5b = step(g5, ls5, datetime(2026, 10, 14, 9, 10), {"005930": 229_000})
check("R1-4) 정상: 기준(보유 0) 뒤 매수 → 손익 = −추정 매수 비용 802(가격 그대로), 같은 날 재시작해도 기준 1건",
      n5a is None and n5b is None and g5.last["pnl"] == -802 and len([r for r in st5.records() if r["type"] == "DAY_START"]) == 1
      and g5.last["label"].startswith("시작 기준 이후 거래 손익"))
g6, ls6, st6, _ = guard("r1_qty", [])
step(g6, ls6, datetime(2026, 10, 14, 9, 0), {})
ls6.append(fe("x", "BUY", 1, 10_000, d=date(2026, 10, 13)))              # 기준 뒤에 어제 날짜 사건이 끼어듦(복구 등)
r6 = step(g6, ls6, datetime(2026, 10, 14, 9, 5), {"005930": 10_000})
check("R1-5) 지금 수량 ≠ 시작 수량 + 오늘 순체결(구간 불일치) → 손익 모름, 신규 매수 보류",
      r6 == BLOCK_UNKNOWN and g6.last["qty_mismatch"] and g6.last["pnl"] is None)


class RCase(Case):
    def run(self, day, start, until, events=()):
        clk = Clock(datetime.combine(day, start))
        ev = Events(clk, events)
        lg = Mock()
        runner, ex = build_session_runner(self.s, self.broker, lg, strategy=self.strategy(), clock=clk, sleep=ev.sleep,
                                          quote_source=self.px, should_stop=lambda: clk.t >= datetime.combine(day, until))
        runner.risk_guard = build_daily_risk_guard(self.s, runner.ledger_store, lg, enforce=True)
        try:
            summ = runner.run()
        finally:
            ex.shutdown()
        self.status = runner._status_dict(clk.t)
        self.msgs += [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
        return summ


R = RCase("r1_runner")
_led = FillLedgerStore(R.s.storage.fill_ledger_file)        # 오늘 실행기 시작 전 M1 수동 왕복(매수·매도) — 보유 0
_led.append(fe("m1b", "BUY", 1, 100_000, sym="000660", oid="0000777", hh=8, mm=50))
_led.append(fe("m1s", "SELL", 1, 99_000, sym="000660", oid="0000778", hh=8, mm=55))
R.run(D1, time(9, 0), time(9, 40))
check("R1-6) 실행기 통합: 시작 전 오늘 수동 왕복이 있으면(보유 0) 승인 후보·가격이 맞아도 매수 없음 — "
      "GUARD_REJECTED:BASELINE_MISSING_WITH_TRADES, 기준 기록 없음",
      R.broker.orders == [] and R.status["daily_risk"]["state"] == BLOCK_BASELINE
      and any(r.get("status") == "GUARD_REJECTED:BASELINE_MISSING_WITH_TRADES" for r in R.keys.records())
      and DailyRiskStore(Path(R.s.storage.state_file).with_name("daily_risk.jsonl")).day("2026-10-14")[0] is None)

# 2. R2 — 청산 가격 실패
strat_calls = {"n": 0}


def flaky(sym):
    strat_calls["n"] += 1
    v = prices_seq.pop(0)
    if isinstance(v, Exception):
        raise v
    return v


S = Case("r2")
st_r2 = S.strategy()
st_r2.price = flaky
EXIT_DAY_ = CAL.add_trading_days(D1, 10)
pos = apply_events([fe("b", "BUY", 1, 10_000)]).positions()
meta = {"005930": PositionMeta("005930", strategy_id=STRATEGY_ID, stop_price=9_876, origin="ORDER")}


def ctx(t):
    return TickContext(now=datetime.combine(EXIT_DAY_, t), phase=MarketPhase.REGULAR, trade_date=EXIT_DAY_,
                       balance=AccountBalance(1, 1, [Position("005930", 1, 10_000)]), positions=pos, metas=meta,
                       blocked_symbols=frozenset(), orders_in_flight=False)


prices_seq = [RuntimeError("ka10001 timeout"), None, 0, 10_050]
outs = [st_r2.on_tick(ctx(time(15, m))) for m in (10, 11, 12, 13)]
check("R2-1) 시간 청산 기한(15:10)인데 가격 예외·None·0 → 매도 의도 없음(손절가를 참고가로 쓰지 않음, NO_PRICE·time_due 기록), "
      "다음 유효 가격 10,050에 E1_TIME forced 매도(참고가 = 관측가)",
      outs[:3] == [[], [], []] and len(outs[3]) == 1 and outs[3][0].reason == "E1_TIME" and outs[3][0].forced
      and outs[3][0].reference_price == 10_050
      and any(d["event"] == "NO_PRICE" and d.get("time_due") for d in S.decisions))
S2 = Case("r2_stop")
st2 = S2.strategy()
prices_seq = [RuntimeError("x"), 9_000]
st2.price = flaky
o2 = [st2.on_tick(TickContext(now=datetime(2026, 10, 15, 10, m), phase=MarketPhase.REGULAR, trade_date=D2,
                              balance=AccountBalance(1, 1, [Position("005930", 1, 10_000)]), positions=pos, metas=meta,
                              blocked_symbols=frozenset(), orders_in_flight=False)) for m in (0, 1)]
check("R2-2) 손절도 가격 실패면 보류 → 복구된 가격 9,000 ≤ 9,876에 E1_STOP forced",
      o2[0] == [] and len(o2[1]) == 1 and o2[1][0].reason == "E1_STOP" and o2[1][0].reference_price == 9_000)

# 3. 시간 청산 forced — 15:16(주문 가능 시각 15:15 이후)
T = Case("t1516")
T.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), T.fill_buy(10_000))])
T.run(EXIT_DAY_, time(15, 16), time(15, 19), events=[(time(15, 17), T.fill_sell(10_000))])
t_res = [d for d in T.decisions if d["event"] == "RESULT_SELL"]
check("3-1) 10번째 거래일 실행기가 15:16에 처음 켜짐 → 주문 가능 시각(15:15) 뒤라도 forced라 정규장 안에서 매도",
      [o["side"] for o in T.broker.orders] == ["BUY", "SELL"] and [d["status"] for d in t_res] == ["SUBMITTED"]
      and t_res[0]["at"] == f"{EXIT_DAY_.isoformat()}T15:16:00" and T.ledger_qty() == 0)


# 4. 청산 재전송 간격
class SellReject(SnapBroker):
    def place_order(self, order):
        if order.side.value == "SELL":
            self.orders.append({"symbol": order.symbol, "side": "SELL", "type": order.order_type, "oid": "",
                                "at": None})
            return OrderResult("0000009", order.symbol, order.side, order.quantity, False, "[RC4007] 매매제한",
                               datetime.now())
        return super().place_order(order)


B = Case("backoff")
B.broker = SellReject(B.s.storage.state_file)
B.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), B.fill_buy(10_000))])
B.px.price = 9_000
B.run(D2, time(9, 0), time(9, 14, 30))
sells = [d for d in B.decisions if d["event"] == "RESULT_SELL"]
sell_n = sum(1 for o in B.broker.orders if o["side"] == "SELL")
check("4-1) forced 손절 매도가 미접수면 종목별 재전송 간격 60·120·240…초 — 14분 30초 동안 4회(09:00·09:01·09:03·09:07, "
      "다음은 09:15), 매번 폴링마다 다시 내지 않음",
      sell_n == 4 and any(d["event"] == "EXIT_BACKOFF" for d in B.decisions))

# 5. R3 — 저장 대기 근거 spool
class FlakyStore(FillEvidenceStore):
    fail = True

    def append(self, record):
        if FlakyStore.fail:
            raise OSError("disk busy")
        return super().append(record)


G = TMP / "spool"
G.mkdir()
ls = FillLedgerStore(G / "fill_ledger.jsonl")
ev = FillEvent("sc|20261014|BUY|005930|0000123|1", "BUY", "005930", 1, 10_020, "ORDER_ESTIMATE", D1,
               datetime(2026, 10, 14, 9, 6), order_id="0000123")
ls.append(ev)
calls: list = []


def lookup(oid, sym):
    calls.append(oid)
    return FillDetail("0000123", "005930", "BUY", 1, 10_000, 720, 0)


store = FlakyStore(G / "fill_evidence.jsonl")
spool = EvidenceSpool(G / "fill_evidence_spool.jsonl")
rec1 = FillRecorder(ls, scope="sc", fill_lookup=lookup, evidence_store=store, spool=spool)
rec1._unconfirmed[ev.event_id] = {"event": ev, "attempts": 0, "next_at": datetime(2026, 10, 14, 9, 7)}
rec1.observe(AccountBalance(1, 1, [Position("005930", 1, 10_000)]), trade_date=D1, now=datetime(2026, 10, 14, 9, 7))
u1 = spool.unresolved()
FlakyStore.fail = False
rec2 = FillRecorder(ls, scope="sc", fill_lookup=lookup, evidence_store=store, spool=spool)    # 다음 날 재시작
n2 = rec2.restore_spool(datetime(2026, 10, 15, 9, 0))
n2b = rec2.recover_unconfirmed(ls.load(), D2, datetime(2026, 10, 15, 9, 0))
rec2.observe(AccountBalance(1, 1, [Position("005930", 1, 10_000)]), trade_date=D2, now=datetime(2026, 10, 15, 9, 0))
saved = [r.event_id for r in FillEvidenceStore(G / "fill_evidence.jsonl").load()]
check("R3-1) 확인한 근거 저장 실패 → spool PENDING(근거·주문번호·시도·다음 시각) → 다음 날 재시작: 재조회 없이 저장부터 재개 "
      "→ 근거 파일 기록, spool DONE(미해결 0)",
      list(u1) == [ev.event_id] and u1[ev.event_id]["order_id"] == "0000123" and u1[ev.event_id]["attempts"] == 1
      and n2 == 1 and n2b == 0 and calls == ["0000123"] and saved == [ev.event_id] and spool.unresolved() == {})
G2 = TMP / "spool2"
G2.mkdir()
(G2 / "fill_evidence_spool.jsonl").mkdir()                    # 대기열 파일 자리에 폴더 → 보관 실패
FlakyStore.fail = True
rec3 = FillRecorder(ls, scope="sc", fill_lookup=lookup, evidence_store=FlakyStore(G2 / "fe.jsonl"),
                    spool=EvidenceSpool(G2 / "fill_evidence_spool.jsonl"))
rec3._unconfirmed[ev.event_id] = {"event": ev, "attempts": 0, "next_at": datetime(2026, 10, 14, 9, 7)}
rec3.observe(AccountBalance(1, 1, [Position("005930", 1, 10_000)]), trade_date=D1, now=datetime(2026, 10, 14, 9, 7))
check("R3-2) 대기열 보관 자체가 실패하면 '근거 미확정(재시작하면 잃음)' 표시 — 상태 파일 spool_error",
      "근거 미확정" in rec3.evidence_status()["spool_error"])
G3 = TMP / "spool3"
G3.mkdir()
sp3 = EvidenceSpool(G3 / "s.jsonl")
r_ev = record_for_event(ev, FillDetail("0000123", "005930", "BUY", 1, 10_000, 720, 0), origin="AUTO",
                        now=datetime(2026, 10, 14, 9, 7))
sp3.put(r_ev, attempts=10, next_at="2026-10-14T09:30:00", error="x", at="2026-10-14T09:20:00")
sp3.mark(ev.event_id, "GAVE_UP", at="2026-10-14T09:30:00")
FlakyStore.fail = False
st3 = FlakyStore(G3 / "fe.jsonl")
st3.append(r_ev)
rec4 = FillRecorder(ls, scope="sc", fill_lookup=lookup, evidence_store=st3, spool=sp3)
check("R3-3) 이번 실행 한도로 포기(GAVE_UP)해도 미해결로 남고, 재시작 때 근거 파일에 이미 있으면 DONE으로 닫음(중복 저장 없음)",
      list(EvidenceSpool(G3 / "s.jsonl").unresolved()) == [ev.event_id]
      and rec4.restore_spool(datetime(2026, 10, 15, 9, 0)) == 0 and sp3.unresolved() == {})

repo = Path(__file__).resolve().parent
check("6) 저장소 data/·commands/에 시험 파일 없음",
      not (repo / "data" / "fill_evidence_spool.jsonl").exists() and not (repo / "commands" / "halt.json").exists())

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
