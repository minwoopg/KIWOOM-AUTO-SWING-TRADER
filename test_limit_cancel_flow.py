# -*- coding: utf-8 -*-
"""지정가 매수 → 09:30 미체결 취소 → 확정, 체결 근거 기록 (M3 A2) — 하루 수명주기 통합.

가짜 브로커가 키움 실측 행 형식(2026-10-13, tests/fixtures/m1_20261013)으로 미체결·체결 목록을 돌려줌.
판정은 실제 함수(derive_broker_order_status / build_order_status_evidence / extract_fill_detail).
경로는 모두 임시 폴더. API 호출 없음.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.main import run_session  # noqa: E402
from domain.models import AccountBalance, OrderResult, Position  # noqa: E402
from domain.position.lifecycle import PositionLifecycle  # noqa: E402
from domain.risk.account_guard import GuardConfig, check_intent  # noqa: E402
from domain.strategy.interface import OrderIntent, OrderIntentError  # noqa: E402
from infra.broker.execution_mode import CancelResult  # noqa: E402
from infra.broker.fill_detail import extract_fill_detail  # noqa: E402
from infra.broker.kiwoom_order_status import build_order_status_evidence, normalize_order_id  # noqa: E402
from infra.storage.fill_evidence import FillEvidenceStore, evidence_path  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402
from infra.storage.runner_status import request_buy_halt  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="limit_cancel_"))
DAY = date(2026, 10, 14)
CANCEL_BODY = json.loads(Path("tests/fixtures/m1_20261013/cancel_kt10003.json").read_text(encoding="utf-8"))["body"]
OPEN_ROW = json.loads(Path("tests/fixtures/m1_20261013/before_cancel_ka10075.json").read_text(encoding="utf-8"))["body"]["oso"][0]
FILL_ROW = json.loads(Path("tests/fixtures/m1_20261013/after_cancel_ka10076.json").read_text(encoding="utf-8"))["body"]["cntr"][1]


class LimitBroker(ScriptedBroker):
    """주문 → 실측 형식의 미체결 행, 취소 → 행 삭제(실측과 같게), fill() → 체결 행 + 잔고."""

    def __init__(self):
        super().__init__()
        self.orders: list[dict] = []
        self.oso: dict[str, list[dict]] = {}
        self.cntr: dict[str, list[dict]] = {}
        self.cancel_script: list[str] = []
        self.cancel_calls: list[tuple] = []
        self.fill_fail = 0
        self.fill_calls = 0
        self.avg: dict[str, int] = {}

    def get_account_balance(self):
        return AccountBalance(100_000_000, 100_000_000,
                              [Position(s, q, self.avg.get(s, 10000)) for s, q in self.positions.items() if q > 0])

    def place_order(self, order):
        self.seq += 1
        oid = f"{148900 + self.seq:07d}"
        self.orders.append({"symbol": order.symbol, "side": order.side.value, "qty": order.quantity,
                            "type": order.order_type, "price": order.price, "oid": oid})
        self.place_calls.append((order.symbol, order.side.value, order.quantity))
        if order.order_type == "limit":
            self.oso.setdefault(order.symbol, []).append({
                **OPEN_ROW, "ord_no": oid, "stk_cd": order.symbol, "ord_qty": str(order.quantity),
                "oso_qty": str(order.quantity), "cntr_qty": "0", "ord_pric": str(order.price)})
        return OrderResult(oid, order.symbol, order.side, order.quantity, True, "모의투자 매수주문완료", datetime.now())

    def fill(self, oid: str, symbol: str, qty: int, price: int, side="BUY", *, partial_of: int | None = None):
        self.oso[symbol] = [r for r in self.oso.get(symbol, []) if normalize_order_id(r["ord_no"]) != normalize_order_id(oid)]
        ord_qty = partial_of or qty
        self.cntr.setdefault(symbol, []).append({
            **FILL_ROW, "ord_no": oid, "stk_cd": symbol, "io_tp_nm": "+매수" if side == "BUY" else "-매도",
            "ord_qty": str(ord_qty), "cntr_qty": str(qty), "oso_qty": str(ord_qty - qty), "cntr_pric": str(price),
            "ord_stt": "체결", "trde_tp": "보통", "tdy_trde_cmsn": "720", "tdy_trde_tax": "0" if side == "BUY" else "400"})
        if partial_of:
            self.oso.setdefault(symbol, []).append({**OPEN_ROW, "ord_no": oid, "stk_cd": symbol, "ord_qty": str(ord_qty),
                                                    "oso_qty": str(ord_qty - qty), "cntr_qty": str(qty)})
        cur = self.positions.get(symbol, 0)
        self.positions[symbol] = cur + qty if side == "BUY" else cur - qty
        self.avg[symbol] = price

    def cancel_order(self, oid, symbol, qty):
        self.cancel_calls.append((oid, symbol, qty))
        kind = self.cancel_script.pop(0) if self.cancel_script else "accept"
        if kind == "ambiguous":
            return CancelResult(False, True, "", "timeout", {})
        if kind == "reject":
            return CancelResult(False, False, "", "[RC4033] 취소가능수량이 없습니다", {"return_code": 20})
        if kind == "accept":   # 실측: 취소된 주문은 미체결·체결 목록 어디에도 남지 않음
            self.oso[symbol] = [r for r in self.oso.get(symbol, [])
                                if normalize_order_id(r["ord_no"]) != normalize_order_id(oid)]
        body = {**CANCEL_BODY, "ord_no": f"{int(oid) + 15:07d}", "base_orig_ord_no": oid,
                "cncl_qty": f"{qty:012d}"}
        return CancelResult(True, False, body["ord_no"], body["return_msg"], body)

    def get_order_status_evidence(self, order_id, symbol):
        self.status_calls.append((symbol, order_id))
        return build_order_status_evidence(order_id, symbol, self.oso.get(symbol, []), self.cntr.get(symbol, []))

    def get_order_status(self, order_id, symbol):
        return self.get_order_status_evidence(order_id, symbol).broker_order

    def get_fill_detail(self, order_id, symbol):
        self.fill_calls += 1
        if self.fill_fail > 0:
            self.fill_fail -= 1
            raise RuntimeError("HTTP 429 return_code 5")
        return extract_fill_detail(order_id, symbol, self.cntr.get(symbol, []))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class Strat:
    strategy_id = "t"

    def __init__(self, script):
        self.script = list(script)

    def on_session_start(self, ctx):
        pass

    def on_session_end(self, ctx):
        pass

    def on_tick(self, ctx):
        out = [i for t, i in self.script if ctx.now.time() >= t]
        self.script = [(t, i) for t, i in self.script if ctx.now.time() < t]
        return out


class Events:
    """시각별로 브로커 상태를 바꾸는 훅(sleep 뒤에 실행)."""

    def __init__(self, clk, script):
        self.clk, self.script = clk, list(script)

    def sleep(self, s):
        self.clk.sleep(s)
        due = [f for t, f in self.script if self.clk.t.time() >= t]
        self.script = [(t, f) for t, f in self.script if self.clk.t.time() < t]
        for f in due:
            f()


def run(name, broker, script, *, until=time(10, 30), start=time(9, 0), events=(), before=None):
    d = TMP / name
    d.mkdir()
    s = build_minimal_settings(str(d))
    if before:
        before(s)
    clk = Clock(datetime.combine(DAY, start))
    ev = Events(clk, events)
    lg = Mock()
    summ = run_session(s, broker, lg, strategy=Strat(script), clock=clk, sleep=ev.sleep,
                       should_stop=lambda: clk.t >= datetime.combine(DAY, until))
    msgs = [str(c.args[0]) for m in ("info", "warning", "critical") for c in getattr(lg, m).call_args_list]
    return s, summ, msgs


def limit(sym, price, qty=1):
    return OrderIntent(sym, "BUY", qty, price, order_type="limit", limit_price=price)


# 0. 의도·한도 (순수)
errs = []
for kw in ({"side": "SELL", "order_type": "limit", "limit_price": 207_000}, {"order_type": "limit", "limit_price": 207_050},
           {"order_type": "limit"}, {"limit_price": 207_000}, {"order_type": "stop", "limit_price": 207_000}):
    try:
        OrderIntent("005930", kw.pop("side", "BUY"), 1, 230_000, **kw)
        errs.append(False)
    except OrderIntentError:
        errs.append(True)
bal = AccountBalance(10_000_000, 10_000_000, [])
g = GuardConfig(max_order_amount=207_000)
d_lim = check_intent(limit("005930", 207_000), config=g, now=datetime(2026, 10, 14, 9, 10), phase=MarketPhase.REGULAR,
                     balance=bal, positions={}, prices={}, blocked_symbols=frozenset())
d_mkt = check_intent(OrderIntent("005930", "BUY", 1, 207_000), config=g, now=datetime(2026, 10, 14, 9, 10),
                     phase=MarketPhase.REGULAR, balance=bal, positions={}, prices={}, blocked_symbols=frozenset())
check("0) 지정가는 매수만·호가 단위 맞는 양의 정수만(207,050·가격 없음·시장가에 가격·모르는 유형 거부), "
      "한도 금액은 지정가×수량(시장가 여유 1% 없음 — 같은 금액 시장가는 상한 초과)",
      all(errs) and limit("005930", 207_000).notional == 207_000 and d_lim.allowed
      and (d_mkt.allowed, d_mkt.code) == (False, "ORDER_AMOUNT_LIMIT"))

# 1. 미체결 지정가 → 09:30 취소 → 확정 → 계좌 신규 매수 다시 허용
b1 = LimitBroker()
s1, sm1, m1 = run("unfilled", b1, [(time(9, 10), limit("005930", 207_000)),
                                   (time(9, 20), OrderIntent("000660", "BUY", 1, 180_000)),
                                   (time(10, 0), OrderIntent("000660", "BUY", 1, 180_000))])
o1 = b1.orders[0]
check("1-a) 지정가 주문: 키움 요청에 limit·207,000 그대로, 미체결 중(09:20)엔 계좌 신규 매수 보류",
      (o1["type"], o1["price"], o1["qty"]) == ("limit", 207_000, 1)
      and (sm1.denied_codes.get("ACCOUNT_RECOVERY_REQUIRED", 0) + sm1.denied_codes.get("ACCOUNT_ORDER_UNRESOLVED", 0)
           + sum(v for k, v in sm1.denied_codes.items() if "PENDING" in k)) >= 1)
check("1-b) 09:30에 이 실행이 낸 미체결 지정가만 취소 — 폴링이 여러 번이어도 취소 요청은 한 번(잔량 1주)",
      b1.cancel_calls == [(o1["oid"], "005930", 1)] and sm1.cancels_requested == 1)
journal = [json.loads(x) for x in (Path(s1.storage.state_file).with_name("buy_cancels.jsonl")).read_text(
    encoding="utf-8").splitlines()]
check("1-c) 다음 체결조회에서 '취소 접수 + 원주문 행 없음 + 잔고 0' → CANCEL_CONFIRMED, 취소 기록(감사용)에 남음",
      [j["event"] for j in journal] == ["CANCEL_REQUESTED", "CANCEL_CONFIRMED"]
      and any("[BUY_CANCEL_CONFIRMED]" in m for m in m1))
check("1-d) 확정 뒤 10:00 다른 종목 매수는 전송됨(복구 차단 풀림), 원장에는 아무 체결도 없음, 마감 검증 정상",
      [o["symbol"] for o in b1.orders] == ["005930", "000660"]
      and [e for e in FillLedgerStore(s1.storage.fill_ledger_file).load() if e.symbol == "005930"] == [])

# 2. 진입 마감 이후 지정가 의도 거부
b2 = LimitBroker()
_, sm2, _ = run("late", b2, [(time(9, 31), limit("005930", 207_000))], until=time(9, 40))
check("2) 09:30 이후 지정가 매수 의도는 ENTRY_WINDOW_CLOSED — 전송 없음",
      b2.orders == [] and sm2.denied_codes.get("ENTRY_WINDOW_CLOSED") == 1)

# 3. 취소 직전 체결(경합): 09:29:30에 체결 → 09:30 취소 요청 시점엔 이미 끝난 주문이면 취소하지 않음 / 체결 근거로 기록
b3 = LimitBroker()
s3, sm3, m3 = run("filled", b3, [(time(9, 10), limit("005930", 207_000))], until=time(9, 45),
                  events=[(time(9, 29, 30), lambda: b3.fill(b3.orders[0]["oid"], "005930", 1, 206_500))])
ev3 = FillLedgerStore(s3.storage.fill_ledger_file).load()
evid3 = FillEvidenceStore(evidence_path(s3.storage.fill_ledger_file)).load()
check("3) 취소 전에 체결된 지정가: 취소 요청 없음, 원장 BUY 1건 = ka10076 체결가 206,500(BROKER_FILL), "
      "수수료 근거 기록 1건",
      b3.cancel_calls == [] and len(ev3) == 1 and (ev3[0].price, ev3[0].price_source) == (206_500, "BROKER_FILL")
      and len(evid3) == 1 and evid3[0].commission == 720 and evid3[0].origin == "AUTO")

# 4. 취소 요청과 체결이 겹침: 취소는 접수됐지만(orphan 상태) 체결 목록에 전량 체결 → 정상 체결로 확정
b4 = LimitBroker()
b4.cancel_script = ["accept_but_filled"]


def _race():
    b4.fill(b4.orders[0]["oid"], "005930", 1, 207_000)


s4, sm4, m4 = run("race", b4, [(time(9, 10), limit("005930", 207_000))], until=time(9, 45),
                  events=[(time(9, 30, 30), _race)])
ev4 = FillLedgerStore(s4.storage.fill_ledger_file).load()
check("4) 취소 접수 직후 체결이 드러남(경합) → 매수 체결로 기록(BROKER_FILL 207,000), 취소 확정으로 처리하지 않음",
      len(b4.cancel_calls) == 1 and len(ev4) == 1 and ev4[0].price_source == "BROKER_FILL"
      and not any("[BUY_CANCEL_CONFIRMED]" in m for m in m4))

# 5. 취소 거절(주문은 그대로 미체결) → 최대 3회, 그 뒤 사람 확인 / 계좌 신규 매수 계속 보류
b5 = LimitBroker()
b5.cancel_script = ["reject"] * 5
_, sm5, m5 = run("reject", b5, [(time(9, 10), limit("005930", 207_000)),
                                (time(10, 0), OrderIntent("000660", "BUY", 1, 180_000))])
check("5) 취소 거절이 이어지면 3회까지만 요청, 주문 미체결이면 orphan 유지 → 다른 종목 신규 매수 계속 차단(전송 없음)",
      len(b5.cancel_calls) == 3 and [o["symbol"] for o in b5.orders] == ["005930"]
      and sm5.denied_codes.get("ACCOUNT_RECOVERY_REQUIRED", 0) >= 1)

# 6. 취소 응답 불명 → 다시 보내지 않음, 목록에 없어도 확정하지 않음(사람 확인)
b6 = LimitBroker()
b6.cancel_script = ["ambiguous"]
b6_hide = lambda: b6.oso.__setitem__("005930", [])     # noqa: E731 — 실제로는 취소됐을 수도 있는 상황
_, sm6, m6 = run("ambiguous", b6, [(time(9, 10), limit("005930", 207_000))], until=time(10, 0),
                 events=[(time(9, 30, 30), b6_hide)])
check("6) 취소 응답 불명: 한 번만 보내고, 원주문이 목록에서 사라져도 확정 안 함 → [BUY_CANCEL_UNRESOLVED]·orphan 유지",
      len(b6.cancel_calls) == 1 and any("[BUY_CANCEL_AMBIGUOUS]" in m for m in m6)
      and any("[BUY_CANCEL_UNRESOLVED]" in m for m in m6) and not any("[BUY_CANCEL_CONFIRMED]" in m for m in m6))

# 7. 부분 체결(2주 중 1주) 뒤 취소 → 부분 체결 행은 미실측 → 사람 확인, 원장에는 체결 1주(지정가 추정)
b7 = LimitBroker()
s7, sm7, m7 = run("partial", b7, [(time(9, 10), limit("005930", 207_000, qty=2))], until=time(10, 0),
                  events=[(time(9, 20), lambda: b7.fill(b7.orders[0]["oid"], "005930", 1, 207_000, partial_of=2))])
ev7 = FillLedgerStore(s7.storage.fill_ledger_file).load()
check("7) 부분 체결 뒤 취소: 잔량 1주만 취소 요청, 확정하지 않고 사람 확인, 원장은 체결된 1주만(지정가 추정 — 주문 전량이 아니라 "
      "체결조회 근거 안 붙임)",
      b7.cancel_calls and b7.cancel_calls[0][2] == 1 and any("[BUY_CANCEL_UNRESOLVED]" in m for m in m7)
      and len(ev7) == 1 and ev7[0].quantity == 1 and ev7[0].price_source in ("BROKER_AVG", "ORDER_ESTIMATE"))

# 8. 신규 매수 중단 중에도 취소는 함
b8 = LimitBroker()
_, sm8, _ = run("halted", b8, [(time(9, 10), limit("005930", 207_000))], until=time(9, 45),
                events=[(time(9, 15), lambda: None)],
                before=lambda s: request_buy_halt(s.storage.commands_dir, reason="test", by="test",
                                                  now=datetime.combine(DAY, time(8, 0))))
check("8) 신규 매수 중단이면 지정가 매수 자체가 안 나감(BUY_HALTED) — 취소할 것도 없음",
      b8.orders == [] and sm8.denied_codes.get("BUY_HALTED") == 1)

b8c = LimitBroker()
s8c_dir = {}


def _halt_now():
    request_buy_halt(s8c_dir["s"].storage.commands_dir, reason="장중 중단", by="test", now=datetime.combine(DAY, time(9, 20)))


_, sm8c, m8c = run("halt_mid", b8c, [(time(9, 10), limit("005930", 207_000))], until=time(9, 45),
                   events=[(time(9, 20), _halt_now)], before=lambda s: s8c_dir.setdefault("s", s))
check("8-b) 주문 뒤 신규 매수 중단(09:20)이 반영돼도 09:30 미체결 지정가 취소는 그대로 함",
      len(b8c.cancel_calls) == 1 and any("[BUY_CANCEL_CONFIRMED]" in m for m in m8c))

# 9. 시장가 매도 체결가: 첫 조회 429 → 추정가로 기록, 다음 폴링에 근거 기록만 추가(원장 사건 하나)
b9 = LimitBroker()
b9.positions["005930"] = 1


def _seed(s):
    from domain.position.fill_event import FillEvent
    FillLedgerStore(s.storage.fill_ledger_file).append(FillEvent(
        "seed", "BUY", "005930", 1, 229_000, "BROKER_FILL", date(2026, 10, 13), datetime(2026, 10, 13, 12, 7)))


class SellBroker(LimitBroker):
    def place_order(self, order):
        r = super().place_order(order)
        if order.side.value == "SELL":
            self.fill(r.order_id, order.symbol, order.quantity, 229_500, side="SELL")
        return r


b9 = SellBroker()
b9.positions["005930"] = 1
b9.fill_fail = 1
s9, sm9, m9 = run("sell", b9, [(time(9, 40), OrderIntent("005930", "SELL", 1, 229_000))], until=time(9, 50),
                  before=_seed)
ev9 = [e for e in FillLedgerStore(s9.storage.fill_ledger_file).load() if e.kind == "SELL"]
evid9 = FillEvidenceStore(evidence_path(s9.storage.fill_ledger_file)).load()
check("9) 매도 체결조회 첫 시도 실패(429) → 원장은 추정가 229,000으로 한 번만, 다음 폴링 재확인으로 실제 229,500·수수료·세금 "
      "근거만 추가(AUTO_RETRY)",
      len(ev9) == 1 and (ev9[0].price, ev9[0].price_source) == (229_000, "ORDER_ESTIMATE")
      and len(evid9) == 1 and (evid9[0].price, evid9[0].tax, evid9[0].origin) == (229_500, 400, "AUTO_RETRY")
      and evid9[0].event_id == ev9[0].event_id and b9.fill_calls == 2)

# 10. 재시작: 이전 실행의 미체결 지정가 — 새 실행은 자기 주문이 아니므로 취소하지 않음(ERROR 복원·사람 확인)
b10 = LimitBroker()
d10 = TMP / "restart"
d10.mkdir()
s10 = build_minimal_settings(str(d10))
clk = Clock(datetime.combine(DAY, time(9, 0)))
run_session(s10, b10, Mock(), strategy=Strat([(time(9, 10), limit("005930", 207_000))]), clock=clk, sleep=clk.sleep,
            should_stop=lambda: clk.t >= datetime.combine(DAY, time(9, 15)))
clk2 = Clock(datetime.combine(DAY, time(9, 16)))
lg10 = Mock()
sm10 = run_session(s10, b10, lg10, strategy=Strat([]), clock=clk2, sleep=clk2.sleep,
                   should_stop=lambda: clk2.t >= datetime.combine(DAY, time(9, 45)))
check("10) 재시작 뒤엔 이전 실행의 지정가 매수를 자동 취소하지 않음(사람 확인 경로 유지)",
      b10.cancel_calls == [] and len(b10.orders) == 1)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
