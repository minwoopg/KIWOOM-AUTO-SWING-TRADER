# -*- coding: utf-8 -*-
"""C2: S1 진입 → 체결 → E1 청산(손절·10거래일 시간), 진입 메타 저장·재시작 복구, 신호 키 상태·손상 차단.

    python test_e1_exit.py

가짜 브로커(키움 실측 행 형식, test_limit_cancel_flow의 LimitBroker)로 하루 수명주기 전체 경로(실행기·체결 기록·원장·대조).
임시 폴더만. API·실제 주문 없음.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

_HEAD = (Path(__file__).resolve().parent / "test_limit_cancel_flow.py").read_text(encoding="utf-8").split("# 0. 의도·한도")[0]
exec(compile(_HEAD, "test_limit_cancel_flow_head", "exec"))   # LimitBroker·Clock·Events·check·TMP

from app.candidates import ApprovedCandidate, CandidateBook  # noqa: E402
from app.s1_strategy import STRATEGY_ID, S1EntryStrategy  # noqa: E402
from infra.storage.signal_keys import SignalKeyCorruptError, SignalKeyStore  # noqa: E402
from infra.storage.swing_state_store import SwingStateStore  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

CAL = TradingCalendar.load()
D1 = date(2026, 10, 14)
EXIT_DAY = CAL.add_trading_days(D1, 10)          # 10/28
DAY9 = CAL.add_trading_days(D1, 9)               # 10/27
SYM = "005930"
CAND = ApprovedCandidate(SYM, "삼성전자", "s1|c0ffee|005930|2026-10-13", "2026-10-13", "c0ffee", "run_1",
                         entry_cap=10_025.0, stop_ref=9_876.5, limit_price=10_020, observed_price=10_000,
                         observed_outcome="WITHIN_CAP")
BOOK = CandidateBook("READY", D1.isoformat(), "2026-10-13", "c0ffee", [CAND])


class Px:
    """현재가·호가 — 시각별 대본(없으면 마지막 값)."""

    def __init__(self, price=10_000, ask=10_010):
        self.price, self.ask, self.fail = price, ask, False

    def get_prices(self, syms):
        if self.fail:
            raise RuntimeError("ka10001 timeout")
        return {s: self.price for s in syms}

    def get_quote(self, sym):
        return self.price, self.ask


class SnapBroker(LimitBroker):
    """주문 전송 순간 디스크의 상태 파일에 메타가 있었는지 기록(전송 전 저장 확인)."""

    def __init__(self, state_file):
        super().__init__()
        self.state_file, self.meta_at_place = state_file, []

    def place_order(self, order):
        st, _ = SwingStateStore(self.state_file).load()
        m = st.positions.get(order.symbol)
        self.meta_at_place.append(None if m is None else (m.stop_price, m.meta.get("signal_id"), m.meta.get("entry_status")))
        return super().place_order(order)


class Case:
    """설정 폴더 하나를 여러 날·여러 실행에 걸쳐 씀(재시작 = 새 run_session)."""

    def __init__(self, name):
        d = TMP / name
        d.mkdir()
        self.s = build_minimal_settings(str(d))
        self.dir = Path(self.s.storage.state_file).parent
        self.broker = SnapBroker(self.s.storage.state_file)
        self.px = Px()
        self.book = BOOK
        self.keys = SignalKeyStore(self.dir / "keys_paper.jsonl")
        self.decisions: list[dict] = []
        self.msgs: list[str] = []

    def strategy(self):
        def price(sym):
            v = self.px.get_prices([sym]).get(sym)
            return v if type(v) is int and v > 0 else None
        return S1EntryStrategy(mode="paper", scope="t", candidates=lambda day, now: self.book, quote=self.px.get_quote,
                               keys=self.keys, decisions=self.decisions.append, entry_start=time(9, 5),
                               entry_end=time(9, 30), calendar=CAL, price=price)

    def run(self, day, start, until, events=()):
        clk = Clock(datetime.combine(day, start))
        ev = Events(clk, events)
        lg = Mock()
        summ = run_session(self.s, self.broker, lg, strategy=self.strategy(), clock=clk, sleep=ev.sleep,
                           quote_source=self.px, should_stop=lambda: clk.t >= datetime.combine(day, until))
        self.msgs += [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
        return summ

    def meta(self):
        st, _ = SwingStateStore(self.s.storage.state_file).load()
        return st.positions.get(SYM)

    def ledger_qty(self):
        return sum(-e.quantity if e.kind == "SELL" else e.quantity
                   for e in FillLedgerStore(self.s.storage.fill_ledger_file).load() if e.symbol == SYM)

    def sides(self):
        return [(o["side"], o["type"]) for o in self.broker.orders]

    def last_oid(self, side):
        return [o["oid"] for o in self.broker.orders if o["side"] == side][-1]

    def fill_buy(self, price=10_000):
        return lambda: self.broker.fill(self.last_oid("BUY"), SYM, 1, price)

    def fill_sell(self, price):
        def f():                                   # 매도 주문이 없으면(청산 안 함) 체결도 없음
            if any(o["side"] == "SELL" for o in self.broker.orders):
                self.broker.fill(self.last_oid("SELL"), SYM, 1, price, side="SELL")
        return f

    def events(self, name):
        return [d for d in self.decisions if d["event"] == name]


def setpx(case, price):
    def f():
        case.px.price = price
    return f


# 1. 진입 → 체결 → 손절
A = Case("stop")
A.run(D1, time(9, 0), time(11, 0), events=[(time(9, 6), A.fill_buy(10_000)), (time(10, 0), setpx(A, 9_870)),
                                           (time(10, 2), A.fill_sell(9_870))])
mA = A.meta()
stA = A.keys.status()
check("1-1) 09:05 지정가 1주(상한 호가 내림 10,020) 전송 전 메타 저장: 손절가 = 참고 손절 내림(9,876), 신호·계약·스캔 실행 기록",
      A.sides()[0] == ("BUY", "limit") and A.broker.orders[0]["price"] == 10_020
      and A.broker.meta_at_place[0] == (9_876, CAND.signal_id, "SUBMITTING")
      and any(d["event"] == "INTENT" for d in A.decisions))
check("1-2) 체결 후 현재가 9,870 ≤ 손절가 9,876 → 원장 보유 1주 전량 시장가 매도(E1_STOP), 매도 체결 뒤 원장 0·메타 정리",
      A.sides() == [("BUY", "limit"), ("SELL", "market")] and A.ledger_qty() == 0 and mA is None
      and len(A.events("E1_STOP")) == 1 and A.events("E1_STOP")[0]["stop_price"] == 9_876)
check("1-3) 신호 키 상태: CLAIMED → SUBMITTED(주문번호), 같은 신호 재진입 없음(매도 뒤 승인이 그대로여도 매수 1번)",
      list(stA.values()) == ["SUBMITTED"] and [o["side"] for o in A.broker.orders].count("BUY") == 1
      and [r["status"] for r in A.keys.records()] == ["CLAIMED", "SUBMITTED"])

# 1-4 손절가 바로 위는 매도 안 함 + 진입 메타 내용(재시작 전 저장 상태)
B = Case("hold")
B.run(D1, time(9, 0), time(10, 0), events=[(time(9, 6), B.fill_buy(10_000)), (time(9, 40), setpx(B, 9_877))])
mB = B.meta()
check("1-4) 현재가 9,877(손절가+1)은 매도 없음. 메타: origin ORDER·전략 s1·손절 9,876·신호/계약/스캔 실행/진입 상한/참고 손절·"
      "진입 거래일 = 원장 첫 체결일·상태 FILLED",
      B.sides() == [("BUY", "limit")] and mB is not None and mB.origin == "ORDER" and mB.strategy_id == STRATEGY_ID
      and mB.stop_price == 9_876 and mB.meta.get("signal_id") == CAND.signal_id and mB.meta.get("contract_hash") == "c0ffee"
      and mB.meta.get("scan_run_id") == "run_1" and mB.meta.get("entry_cap") == 10_025.0
      and mB.meta.get("stop_ref") == 9_876.5 and mB.meta.get("entry_trade_date") == "2026-10-14"
      and mB.meta.get("entry_status") == "FILLED")

# 2. 재시작 → 손절(새 프로세스가 저장된 메타로)
B.px.price = 9_000
B.run(date(2026, 10, 15), time(9, 0), time(9, 20), events=[(time(9, 7), B.fill_sell(9_000))])
b_res = [d for d in B.decisions if d["event"] == "RESULT_SELL"]
check("2-1) 재시작(다음 날 새 실행): 저장된 메타의 손절가로 정규장 첫 폴링(09:00)에 시장가 매도(손절은 forced — 주문 시각 제한 예외), "
      "신규 매수 없음",
      B.sides() == [("BUY", "limit"), ("SELL", "market")] and B.ledger_qty() == 0 and B.meta() is None
      and [d["status"] for d in b_res] == ["SUBMITTED"] and b_res[0]["at"] == "2026-10-15T09:00:00")
# 3. 시간 청산 — 진입일 0 → 10번째 거래일 15:10
C = Case("time")
C.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), C.fill_buy(10_000))])
C.run(DAY9, time(15, 0), time(15, 25))
n_day9 = len(C.broker.orders)
C.run(EXIT_DAY, time(15, 0), time(15, 25), events=[(time(15, 12), C.fill_sell(10_100))])
sell = [o for o in C.broker.orders if o["side"] == "SELL"]
t_ev = C.events("E1_TIME")
check(f"3-1) 9번째 거래일({DAY9:%m/%d}) 15:10 이후에도 매도 없음, 10번째({EXIT_DAY:%m/%d}) 15:10 전 매도 없음 → 15:10 시장가 전량 매도",
      n_day9 == 1 and len(sell) == 1 and len(t_ev) == 1 and t_ev[0]["at"] >= f"{EXIT_DAY.isoformat()}T15:10"
      and t_ev[0]["time_exit_at"] == f"{EXIT_DAY.isoformat()}T15:10" and C.ledger_qty() == 0)
C2 = Case("time_late")
C2.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), C2.fill_buy(10_000))])
nxt = CAL.add_trading_days(EXIT_DAY, 1)
C2.run(nxt, time(8, 50), time(9, 10), events=[(time(9, 7), C2.fill_sell(10_000))])
c2_res = [d for d in C2.decisions if d["event"] == "RESULT_SELL"]
check("3-2) 시간 청산 시각에 실행기가 꺼져 있었으면 다음 날 정규장 첫 폴링(09:00)에 매도 — C4부터 시간 청산도 forced",
      [o["side"] for o in C2.broker.orders] == ["BUY", "SELL"]
      and [d["status"] for d in c2_res] == ["SUBMITTED"] and c2_res[0]["at"] == f"{nxt.isoformat()}T09:00:00"
      and C2.events("E1_TIME")[0]["at"] == f"{nxt.isoformat()}T09:00:00" and C2.ledger_qty() == 0)

# 4. 신규 매수 중단·가격 실패·수량 불일치·메타 없음
D = Case("halt")
D.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), D.fill_buy(10_000))])
request_buy_halt(D.s.storage.commands_dir, reason="test", by="test", now=datetime(2026, 10, 15, 8, 0))
D.px.price = 9_500
D.run(date(2026, 10, 15), time(9, 0), time(9, 20), events=[(time(9, 7), D.fill_sell(9_500))])
check("4-1) 신규 매수 중단 중에도 손절 매도는 나감(중단은 매수만)",
      [o["side"] for o in D.broker.orders] == ["BUY", "SELL"] and D.ledger_qty() == 0)

E = Case("pxfail")
E.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), E.fill_buy(10_000))])
E.px.fail = True
E.run(date(2026, 10, 15), time(9, 0), time(9, 20))
E.px.fail, E.px.price = False, 0
E.run(date(2026, 10, 16), time(9, 0), time(9, 20))
check("4-2) 현재가 조회 실패·0원(거래정지 등)은 매도하지 않음(PRICE_FAILED·NO_PRICE 기록), 보유 유지",
      [o["side"] for o in E.broker.orders] == ["BUY"] and E.events("PRICE_FAILED") and E.events("NO_PRICE")
      and E.ledger_qty() == 1)

F = Case("nometa")
F.broker.positions[SYM] = 1
FillLedgerStore(F.s.storage.fill_ledger_file).append(
    __import__("domain.position.fill_event", fromlist=["FillEvent"]).FillEvent(
        "x", "BUY", SYM, 1, 10_000, "BROKER_FILL", date(2026, 10, 13), datetime(2026, 10, 13, 10, 0)))
F.px.price = 5_000
F.run(D1, time(9, 0), time(9, 40))
check("4-3) 보유는 있는데 이 전략 메타(손절가)가 없으면 자동 매도 안 함(META_MISSING), 신규 매수도 없음(최대 보유 1)",
      F.broker.orders == [] and F.events("META_MISSING") and F.events("MAX_POSITION"))

from domain.position.swing_state import PositionMeta  # noqa: E402
L = Case("othermeta")
L.broker.positions[SYM] = 1
FillLedgerStore(L.s.storage.fill_ledger_file).append(
    __import__("domain.position.fill_event", fromlist=["FillEvent"]).FillEvent(
        "y", "BUY", SYM, 1, 10_000, "BROKER_FILL", date(2026, 10, 13), datetime(2026, 10, 13, 10, 0)))
stL, hpL = SwingStateStore(L.s.storage.state_file).load()
stL.upsert_position_meta(PositionMeta(SYM, strategy_id="manual", stop_price=9_900, origin="ORDER"))
SwingStateStore(L.s.storage.state_file).save(stL, hpL)
L.px.price = 5_000
L.run(D1, time(9, 0), time(9, 20))
check("4-4) 메타가 있어도 다른 전략(strategy_id ≠ S1)이면 손절가 아래여도 자동 매도 안 함(META_MISSING)",
      L.broker.orders == [] and L.events("META_MISSING"))

# 5. 미체결 취소·미접수·응답 불명
G = Case("cancel")
G.px.ask = 10_020
G.run(D1, time(9, 0), time(9, 45))
G.run(D1, time(9, 50), time(10, 0))
check("5-1) 09:30까지 미체결 → 취소, 메타 정리(보유 없음), 키 상태 SUBMITTED 유지 → 같은 날 재시작해도 그 신호 재진입 없음",
      [o["side"] for o in G.broker.orders] == ["BUY"] and len(G.broker.cancel_calls) == 1 and G.meta() is None
      and list(G.keys.status().values()) == ["SUBMITTED"])


class RejectBroker(LimitBroker):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    def place_order(self, order):
        if self.kind == "reject":
            self.orders.append({"symbol": order.symbol, "side": order.side.value, "type": order.order_type, "oid": ""})
            return OrderResult("0000001", order.symbol, order.side, order.quantity, False, "[RC4007] 매매제한", datetime.now())
        self.orders.append({"symbol": order.symbol, "side": order.side.value, "type": order.order_type, "oid": ""})
        return OrderResult("", order.symbol, order.side, order.quantity, False, "timeout", datetime.now(),
                           is_ambiguous=True)


H = Case("reject")
H.broker = RejectBroker("reject")
H.run(D1, time(9, 0), time(9, 40))
check("5-2) 미접수(거부) → 메타 정리, 키 NOT_ACCEPTED, 재전송 없음(가격이 계속 맞아도)",
      len(H.broker.orders) == 1 and H.meta() is None and list(H.keys.status().values()) == ["NOT_ACCEPTED"])
I = Case("ambiguous")
I.broker = RejectBroker("ambiguous")
I.run(D1, time(9, 0), time(9, 40))
mI = I.meta()
check("5-3) 응답 불명 → 키 UNKNOWN, 메타 유지(접수됐다면 체결 시 손절가 필요, 상태 UNKNOWN), 재전송 없음",
      len(I.broker.orders) == 1 and list(I.keys.status().values()) == ["UNKNOWN"] and mI is not None
      and mI.meta.get("entry_status") == "UNKNOWN" and mI.stop_price == 9_876)

# 6. 신호 키 손상
J = Case("corrupt")
J.keys.path.write_text('{"key": "x", "status": "CLAIMED"}\n{broken\n', encoding="utf-8")
J.run(D1, time(9, 0), time(9, 40))
try:
    J.keys.keys()
    j_raise = False
except SignalKeyCorruptError:
    j_raise = True
check("6-1) 신호 키 파일에 읽을 수 없는 줄 → 신규 진입 없음(SIGNAL_KEYS_UNREADABLE), 손상 줄을 건너뛰고 진입하지 않음",
      J.broker.orders == [] and J.events("SIGNAL_KEYS_UNREADABLE") and j_raise)
K = Case("corrupt_exit")
K.run(D1, time(9, 0), time(9, 40), events=[(time(9, 6), K.fill_buy(10_000))])
with K.keys.path.open("a", encoding="utf-8") as fh:
    fh.write("not json\n")
K.px.price = 9_000
K.run(date(2026, 10, 15), time(9, 0), time(9, 20), events=[(time(9, 7), K.fill_sell(9_000))])
check("6-2) 키 파일이 손상돼도 보유 손절은 그대로",
      [o["side"] for o in K.broker.orders] == ["BUY", "SELL"] and K.ledger_qty() == 0)
ks = SignalKeyStore(TMP / "unit_keys.jsonl")
ks.mark("none", "SUBMITTED")
c1 = ks.claim("k1", {"a": 1})
c2 = ks.claim("k1", {"a": 2})
ks.mark("k1", "GUARD_REJECTED:QTY_LIMIT", "x" * 500)
check("6-3) 키 저장소: claim 없는 mark는 무시, 같은 키 두 번 claim 안 됨, 마지막 상태 = 최근 줄, 상세 300자 제한",
      c1 and not c2 and ks.status() == {"k1": "GUARD_REJECTED:QTY_LIMIT"} and len(ks.records()) == 2
      and len(ks.records()[-1]["detail"]) == 300)

# 7. 경로 — 저장소 밖에 아무것도 만들지 않음
repo = Path(__file__).resolve().parent
check("7) 저장소 data/·commands/에 시험 파일 없음",
      not (repo / "data" / "strategy_signal_keys_paper.jsonl").exists() and not (repo / "commands" / "buy_halt.json").exists())

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
