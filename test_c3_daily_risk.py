# -*- coding: utf-8 -*-
"""C3: 공통 비용 함수 · 일일 손실/실패 주문 한도 · paper 사전 점검 · 체결 근거 재시작 복구 · 번들.

    python test_c3_daily_risk.py

가짜 브로커(test_e1_exit의 Case — 실측 행 형식)와 임시 폴더만. API·실제 주문 없음.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

_HEAD = (Path(__file__).resolve().parent / "test_e1_exit.py").read_text(encoding="utf-8").split("# 1. 진입 → 체결 → 손절")[0]
exec(compile(_HEAD, "test_e1_exit_head", "exec"))   # Case·Px·SnapBroker·LimitBroker·check·TMP·CAND·D1

from app.daily_risk import DailyRiskGuard  # noqa: E402
from app.main import build_daily_risk_guard, build_session_runner, paper_account_problems, paper_config_problems  # noqa: E402
from domain.position.fill_event import FillEvent  # noqa: E402
from domain.risk.daily_loss import day_pnl  # noqa: E402
from domain.service.lot_ledger import apply_events  # noqa: E402
from domain.service.trade_costs import ACTUAL, ESTIMATE, CostProfile, allocate, event_cost, event_costs, match_costs  # noqa: E402
from infra.storage.daily_risk import DailyRiskStore, daily_risk_path  # noqa: E402
from infra.storage.runner_status import read_buy_halt, resume_buy_halt  # noqa: E402

P = CostProfile()


class RejectBroker(LimitBroker):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind

    def place_order(self, order):
        self.orders.append({"symbol": order.symbol, "side": order.side.value, "type": order.order_type, "oid": ""})
        if self.kind == "reject":
            return OrderResult("0000001", order.symbol, order.side, order.quantity, False, "[RC4007] 매매제한", datetime.now())
        return OrderResult("", order.symbol, order.side, order.quantity, False, "timeout", datetime.now(),
                           is_ambiguous=True)


def fe(eid, kind, qty, price, d=D1, oid="1"):
    return FillEvent(eid, kind, "005930", qty, price, "BROKER_FILL" if kind != "OPENING" else "BROKER_AVG", d,
                     datetime.combine(d, time(10)), order_id=oid)


# 1. 공통 비용 함수
b = event_cost(fe("b", "BUY", 1, 229_000), None, P)
s_ = event_cost(fe("s", "SELL", 1, 229_000), None, P)
a = event_cost(fe("s", "SELL", 1, 229_000), (800, 457), P)
check("1-1) 추정(근거 없음): 229,000원 1주 매수 802 / 매도 802+458(올림 — 실측 800 / 1,257보다 작지 않음). "
      "근거가 있으면 실제 1,257만(모델 비용을 더하지 않음)",
      (b.amount, b.source) == (802, ESTIMATE) and (s_.amount, s_.commission, s_.tax) == (1_260, 802, 458)
      and (a.amount, a.source) == (1_257, ACTUAL) and event_cost(fe("s", "SELL", 1, 1), (800, None), P).source == ESTIMATE)
check("1-2) 배분: 합이 정확히 원래 값(1,257을 1:1:1 → 419·419·419, 1,000을 1:1:1 → 334·333·333, 7을 3:1 → 5·2)",
      allocate(1_257, [1, 1, 1]) == [419, 419, 419] and allocate(1_000, [1, 1, 1]) == [334, 333, 333]
      and allocate(7, [3, 1]) == [5, 2] and sum(allocate(1_001, [2, 5, 7])) == 1_001)
evs = [fe("B3", "BUY", 3, 10_000), fe("S1", "SELL", 1, 10_500), fe("S2", "SELL", 1, 10_400)]
costs = event_costs(evs, {"B3": (100, 0), "S1": (35, 21), "S2": (36, 21)}, P)
led = apply_events(evs)
mc = match_costs(led.realized, costs, {e.event_id: e.quantity for e in evs})
check("1-3) 부분 매도: 매수 비용 100을 매칭 1주·1주·남은 1주로 나눔(34·33, 잔량 33은 매칭 밖) — 반올림으로 합이 바뀌지 않음",
      [(x[0], x[1], x[2]) for x in mc] == [(34, 56, ACTUAL), (33, 57, ACTUAL)])

# 2. 일일 손익 계산(순수)
start = {"qty": {"005930": 1}, "marks": {"005930": 10_000}}
today = [fe("S", "SELL", 1, 9_800)]
pn = day_pnl(start, today, {}, {}, event_costs(today, {}, P))
pn2 = day_pnl({"qty": {}, "marks": {}}, [fe("B", "BUY", 1, 10_000)], {"005930": 1}, {"005930": 9_900},
              event_costs([fe("B", "BUY", 1, 10_000)], {}, P))
pn3 = day_pnl({"qty": {}, "marks": {}}, [fe("B", "BUY", 1, 10_000)], {"005930": 1}, {},
              event_costs([fe("B", "BUY", 1, 10_000)], {}, P))
check("2-1) 시작 보유 1주(기준 10,000) → 9,800 매도: −200 − 비용 추정 55(34+21) = −255. 오늘 매수 10,000 → 현재가 9,900: "
      "−100 − 35 = −135. 보유가 있는데 현재가 없으면 None(모름). 현금은 쓰지 않음(입출금 무관)",
      pn.pnl == -255 and pn.estimated_costs == 55 and pn2.pnl == -135 and pn3.pnl is None and "005930" in pn3.missing_prices)


# 3. 실행기 통합 — 일일 손실 한도
class RCase(Case):
    """Case + 일일 위험 한도(paper 경로와 같은 build_daily_risk_guard)."""

    def __init__(self, name, *, limit=10_000, enforce=True):
        super().__init__(name)
        st = dataclasses.replace(self.s.swing_strategy, daily_loss_limit=limit)
        self.s = dataclasses.replace(self.s, swing_strategy=st)
        self.enforce = enforce
        self.status = None

    def run(self, day, start, until, events=()):
        clk = Clock(datetime.combine(day, start))
        ev = Events(clk, events)
        lg = Mock()
        runner, ex = build_session_runner(self.s, self.broker, lg, strategy=self.strategy(), clock=clk, sleep=ev.sleep,
                                          quote_source=self.px,
                                          should_stop=lambda: clk.t >= datetime.combine(day, until))
        runner.risk_guard = build_daily_risk_guard(self.s, runner.ledger_store, lg, enforce=self.enforce)
        try:
            summ = runner.run()
        finally:
            ex.shutdown()
        self.status = runner._status_dict(clk.t)
        self.msgs += [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
        return summ

    def risk(self, d=D1):
        return DailyRiskStore(daily_risk_path(self.s.storage.state_file)).day(d.isoformat())

    def halt(self):
        return read_buy_halt(self.s.storage.commands_dir)


A = RCase("loss", limit=800)
A.run(D1, time(9, 0), time(10, 0), events=[(time(9, 6), A.fill_buy(10_000)), (time(9, 20), setpx(A, 9_900))])
stA, fA, hA = A.risk()
hs, hreq = A.halt()
check("3-1) 하루 시작 기준 1건(보유 없음) → 체결 10,000(체결 근거 수수료 720 — 실제 비용만, 추정 0) → 현재가 9,900: "
      "손익 −100 − 720 = −820 ≤ −800 → commands/halt.json(by=daily_risk) + HALT 기록(09:20 가격 하락 뒤), 상태 파일에 손익·한도",
      stA is not None and stA["qty"] == {} and len(hA) == 1 and hs != "NONE" and hreq.get("requested_by") == "daily_risk"
      and hA[0]["at"] >= "2026-10-14T09:20" and A.status["daily_risk"]["pnl"] == -820
      and A.status["daily_risk"]["estimated_costs"] == 0 and A.status["daily_risk"]["limit"] == 800
      and A.status["daily_risk"]["block"] == "DAILY_RISK_HALTED")
A.px.price = 9_870
A.run(date(2026, 10, 15), time(9, 0), time(9, 40), events=[(time(9, 3), A.fill_sell(9_870))])
dec_A = [d for d in A.decisions if d["event"] == "RESULT_BUY"]
check("3-2) 중단은 재시작·다음 날에도 유지(사람이 재개 전까지) — 그사이 손절 매도는 나감, 새 승인 신호가 있어도 매수 없음",
      [o["side"] for o in A.broker.orders] == ["BUY", "SELL"] and A.ledger_qty() == 0 and A.halt()[0] != "NONE")

B = RCase("restart_same_day", limit=10_000)
B.run(D1, time(9, 0), time(9, 10), events=[(time(9, 6), B.fill_buy(10_000))])
_orig_bal = B.broker.get_account_balance
B.broker.get_account_balance = lambda: dataclasses.replace(_orig_bal(), cash=_orig_bal().cash + 5_000_000)   # 입금
B.run(D1, time(9, 20), time(9, 40))
stB, _, hB = B.risk()
recsB = [r for r in DailyRiskStore(daily_risk_path(B.s.storage.state_file)).records() if r["type"] == "DAY_START"]
check("3-3) 같은 날 재시작: 시작 기준을 다시 쓰지 않음(1건 유지), 손익은 원장 기준이라 입금(현금 +5,000,000)과 무관"
      "(−720 = 실제 수수료만), 중단 없음",
      len(recsB) == 1 and B.status["daily_risk"]["pnl"] == -720 and not hB and B.halt()[0] == "NONE")

# 4. 실패 주문 한도
C = RCase("fail")
C.broker = RejectBroker("reject")
C.run(D1, time(9, 0), time(9, 40))
_, fC, hC = C.risk()
check("4-1) 미접수 주문 1건 → ORDER_FAILURE 기록 + 신규 매수 중단(실패 한도 1), 재전송 없음",
      len(C.broker.orders) == 1 and len(fC) == 1 and fC[0]["status"].startswith("NOT_ACCEPTED") and len(hC) == 1
      and C.halt()[1].get("requested_by") == "daily_risk")
Cu = RCase("unknown")
Cu.broker = RejectBroker("ambiguous")
Cu.run(D1, time(9, 0), time(9, 40))
_, fCu, hCu = Cu.risk()
check("4-2) 응답 불명 1건도 같은 처리(UNKNOWN)", len(fCu) == 1 and fCu[0]["status"] == "UNKNOWN" and len(hCu) == 1)
resume_buy_halt(C.s.storage.commands_dir, note="test", now=datetime(2026, 10, 14, 10, 0))
C.broker = SnapBroker(C.s.storage.state_file)
C.book = dataclasses.replace(BOOK, approved=[dataclasses.replace(CAND, signal_id="s1|c0ffee|005930|2026-10-13b")])
C.run(D1, time(10, 0), time(10, 10))
C.run(date(2026, 10, 15), time(9, 0), time(9, 10))
check("4-3) 사람이 중단을 풀어도 그날은 일일 위험 기록(HALT)으로 신규 매수 없음, 다음 거래일은 새 기준으로 다시 가능",
      [o["side"] for o in C.broker.orders] == ["BUY"] and C.broker.orders[0]["type"] == "limit"
      and C.risk(date(2026, 10, 15))[0] is not None)

# 5. 손상·그림자
D = RCase("corrupt")
p = daily_risk_path(D.s.storage.state_file)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("{broken\n", encoding="utf-8")
D.run(D1, time(9, 0), time(9, 40))
check("5-1) 일일 위험 기록 손상 → 그날 신규 매수 없음(DAILY_RISK_UNKNOWN, 중단 파일은 쓰지 않음)",
      D.broker.orders == [] and D.status["daily_risk"]["state"] == "ERROR" and D.halt()[0] == "NONE"
      and any(r.get("status") == "GUARD_REJECTED:DAILY_RISK_UNKNOWN" for r in D.keys.records()))
E = RCase("shadow_guard", limit=100, enforce=False)
E.run(D1, time(9, 0), time(10, 0), events=[(time(9, 6), E.fill_buy(10_000)), (time(9, 20), setpx(E, 9_900))])
check("5-2) 그림자(enforce=False): 같은 계산, 중단 조건이면 '중단했을 것'만 기록 — 중단 파일 없음",
      E.halt()[0] == "NONE" and E.status["daily_risk"].get("would_halt") and not E.risk()[2])

# 6. paper 사전 점검
s_mock = build_minimal_settings(str(TMP / "cfg1"))
pr1 = paper_config_problems(s_mock, ScriptedBroker())
cfg_ok = dataclasses.replace(s_mock.broker, use_mock=False, base_url="https://mockapi.kiwoom.com", app_key="k",
                             secret_key="s", account_number="12345678", account_scope_id="swing_paper_1")
s_ok = dataclasses.replace(s_mock, broker=cfg_ok,
                           swing_strategy=dataclasses.replace(s_mock.swing_strategy, mode="paper",
                                                              allowed_symbols=("005930",)))
from infra.broker.execution_mode import PaperKiwoomBroker  # noqa: E402
pr2 = paper_config_problems(s_ok, PaperKiwoomBroker(cfg_ok))
pr3 = paper_config_problems(dataclasses.replace(s_ok, swing_strategy=dataclasses.replace(
    s_ok.swing_strategy, allowed_symbols=())), PaperKiwoomBroker(cfg_ok))
live = dataclasses.replace(cfg_ok, is_paper_trading=False)
pr4 = paper_config_problems(dataclasses.replace(s_ok, broker=live), ScriptedBroker())
check("6-1) paper 설정 점검: 가짜 브로커(use_mock) 거부, 실전(is_paper_trading false) 거부, 허용 종목 없으면 거부, "
      "모의 서버·모의 브로커·허용 종목 1개면 통과",
      any("KIWOOM_PAPER" in x for x in pr1) and any("allowed_symbols" in x for x in pr1) and pr2 == []
      and any("allowed_symbols" in x for x in pr3) and pr4 and "실전" in pr4[0])
from types import SimpleNamespace  # noqa: E402
from domain.models import AccountBalance, Position  # noqa: E402
from domain.position.swing_state import PositionMeta  # noqa: E402
rep_ok = SimpleNamespace(has_unresolved_orders=False, unresolved_intent_symbols=[], journal_symbols=[], reconcile=None,
                         balance=AccountBalance(1, 1, [Position("005930", 1, 10_000)]))
st_meta = SimpleNamespace(positions={"005930": PositionMeta("005930", strategy_id=STRATEGY_ID, stop_price=9_000)})
st_none = SimpleNamespace(positions={})
rep_bad = SimpleNamespace(has_unresolved_orders=True, unresolved_intent_symbols=["000660"], journal_symbols=[],
                          reconcile=None, balance=AccountBalance(1, 1, []))
check("6-2) 계좌 사전 점검: S1 메타 있는 보유는 통과, 메타 없는 보유·미해결 주문은 문제(→ 신규 매수 중단, 청산은 계속)",
      paper_account_problems(rep_ok, st_meta) == [] and "S1 진입 메타 없는 보유" in paper_account_problems(rep_ok, st_none)[0]
      and "미해결 주문" in paper_account_problems(rep_bad, st_none)[0])
F = Case("allowed")
F2 = F.strategy()
F2.allowed = frozenset({"000660"})
from domain.strategy.interface import TickContext  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402
ctxF = TickContext(now=datetime(2026, 10, 14, 9, 6), phase=MarketPhase.REGULAR, trade_date=D1,
                   balance=AccountBalance(10_000_000, 10_000_000, []), positions={}, metas={},
                   blocked_symbols=frozenset(), orders_in_flight=False)
check("6-3) 허용 종목 밖 승인 후보는 의도 없음(NOT_ALLOWED), 신호 키도 쓰지 않음",
      F2.on_tick(ctxF) == [] and F.events("NOT_ALLOWED") and not F.keys.keys())

# 7. 체결 근거 재시작 복구
from domain.service.fill_recorder import FillRecorder  # noqa: E402
from infra.storage.fill_evidence import FillEvidenceStore  # noqa: E402
G = TMP / "recover"
G.mkdir()
ls = FillLedgerStore(G / "fill_ledger.jsonl")
e_today = FillEvent("sc|20261014|BUY|005930|0000123|1", "BUY", "005930", 1, 10_020, "ORDER_ESTIMATE", D1,
                    datetime(2026, 10, 14, 9, 6), order_id="0000123")
e_old = FillEvent("sc|20261013|BUY|005930|0000100|1", "BUY", "005930", 1, 10_000, "ORDER_ESTIMATE", date(2026, 10, 13),
                  datetime(2026, 10, 13, 9, 6), order_id="0000100")
e_done = FillEvent("sc|20261014|SELL|005930|0000124|1", "SELL", "005930", 1, 10_100, "ORDER_ESTIMATE", D1,
                   datetime(2026, 10, 14, 9, 10), order_id="0000124")
ls.append(e_old)
ls.append(e_today)
ls.append(e_done)
from domain.service.fill_evidence import FillDetail, record_for_event  # noqa: E402
FillEvidenceStore(G / "fill_evidence.jsonl").append(record_for_event(
    e_done, FillDetail("0000124", "005930", "SELL", 1, 10_100, 720, 400), origin="AUTO", now=datetime(2026, 10, 14, 9, 11)))
calls = []


def lookup(oid, sym):
    calls.append(oid)
    return None


rec = FillRecorder(ls, scope="sc", fill_lookup=lookup, evidence_store=FillEvidenceStore(G / "fill_evidence.jsonl"))
n = rec.recover_unconfirmed(ls.load(), D1, datetime(2026, 10, 14, 9, 30))
rec.observe(AccountBalance(1, 1, [Position("005930", 2, 10_000)]), trade_date=D1, now=datetime(2026, 10, 14, 9, 30))
check("7-1) 재시작 복구: 오늘 사건 중 체결 근거 없는 것만 재확인 목록에(근거 있는 오늘 매도·어제 사건 제외), 다음 폴링에 체결조회 1회",
      n == 1 and list(rec.unconfirmed) == [e_today.event_id] and calls == ["0000123"])

# 8. 보고서·번들
from app.reports import export_bundle, generate_daily_report  # noqa: E402
H = Case("report")
H.run(D1, time(9, 0), time(10, 0), events=[(time(9, 6), H.fill_buy(10_000)), (time(9, 30), setpx(H, 9_870)),
                                           (time(9, 32), H.fill_sell(9_870))])
rp = generate_daily_report(H.s, trade_date=D1, today=D1, calendar=CAL)
txt = Path(rp).read_text(encoding="utf-8")
check("8-1) 리포트: 당일 비용(실제 우선·없으면 추정)·비용 후 실현손익 줄 — 근거 있음(가짜 브로커 실측 형식: 720·720+400)",
      "당일 비용 (실제 우선·없으면 추정) | 1,840" in txt and "당일 실현손익 (비용 후 — 실제 우선·없으면 추정) | -1,970" in txt)
(Path(H.s.storage.app_log_file).parent / "market_basis_20261013.json").write_text(
    json.dumps({"symbol": "005930", "rows": 3}), encoding="utf-8")
DailyRiskStore(daily_risk_path(H.s.storage.state_file)).append({"type": "DAY_START", "trade_date": "2026-10-14", "qty": {}})
zp = export_bundle(H.s, D1, root=Path(__file__).resolve().parent, out_dir=TMP / "exports")
names = zipfile.ZipFile(zp).namelist()
check("8-2) 번들: daily_risk·strategy_decisions·신호 키·시장 기준 실측 원문(logs/market_basis_*.json) 포함",
      {"daily_risk.jsonl", "strategy_decisions.jsonl", "market_basis/market_basis_20261013.json"} <= set(names)
      and "manifest.json" in names)

repo = Path(__file__).resolve().parent
check("9) 저장소 data/·commands/에 시험 파일 없음",
      not (repo / "data" / "daily_risk.jsonl").exists() and not (repo / "commands" / "halt.json").exists())

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
