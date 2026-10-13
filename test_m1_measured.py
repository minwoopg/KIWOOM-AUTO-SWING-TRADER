# -*- coding: utf-8 -*-
"""키움 모의 실측 응답(2026-10-13, tests/fixtures/m1_20261013)으로 판정·취소 확정·체결 근거 (M3 A2).

API 호출 없음. 임시 폴더만 씀.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, ".")

from domain.models import BrokerOrderStatus  # noqa: E402
from domain.position.fill_event import FillEvent  # noqa: E402
from domain.service.buy_cancel import (  # noqa: E402
    CANCEL_CONFIRMED, FILLED_BEFORE_CANCEL, STILL_OPEN, UNRESOLVED, CancelRecord, assess_buy_cancel,
)
from domain.service.fill_evidence import (  # noqa: E402
    FillDetail, FillEvidenceRecord, apply_fill_evidence, record_for_event,
)
from domain.service.lot_ledger import apply_events  # noqa: E402
from infra.broker import execution_mode as EM  # noqa: E402
from infra.broker.fill_detail import extract_fill_detail  # noqa: E402
from infra.broker.kiwoom_order_status import build_order_status_evidence, derive_broker_order_status  # noqa: E402
from infra.storage.fill_evidence import (  # noqa: E402
    FillEvidenceConflictError, FillEvidenceStore,
)

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


FX = Path("tests/fixtures/m1_20261013")


def body(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))["body"]


OSO_BEFORE = body("before_cancel_ka10075.json")["oso"]
CNTR_BEFORE = body("before_cancel_ka10076.json")["cntr"]
CANCEL = body("cancel_kt10003.json")
OSO_AFTER = body("after_cancel_ka10075.json")["oso"]
CNTR_AFTER = body("after_cancel_ka10076.json")["cntr"]
BAL_AFTER = body("after_cancel_kt00018.json")
ORDER = body("limit_order_kt10000.json")
TMP = Path(tempfile.mkdtemp(prefix="m1_measured_"))

# 1. 실측 판정
bo_open = derive_broker_order_status("0148913", "005930", OSO_BEFORE, CNTR_BEFORE)
ev_after = build_order_status_evidence("0148913", "005930", OSO_AFTER, CNTR_AFTER)
check("1) 실측: 지정가 접수 직후 0148913은 OPEN(접수·미체결 1·지정가 207,000·trde_tp 보통), 취소 뒤엔 "
      "미체결·체결 목록 어디에도 없어 UNKNOWN(행 0개) — '취소됨'을 판정 함수가 스스로 말하지 않음",
      ORDER["ord_no"] == "0148913" and bo_open.status == BrokerOrderStatus.OPEN and bo_open.open_quantity == 1
      and OSO_BEFORE[0]["ord_pric"] == "207000" and bo_open.order_type_raw == "보통"
      and ev_after.broker_order.status == BrokerOrderStatus.UNKNOWN
      and not ev_after.matched_oso_entries and not ev_after.matched_cntr_entries
      and BAL_AFTER["acnt_evlt_remn_indv_tot"] == [])


# 2. 취소 응답 해석 — 실측 본문을 그대로 돌려주는 가짜 _post
class _Resp:
    def __init__(self, b):
        self.body = b


class PaperStub(EM.PaperKiwoomBroker):
    def __init__(self, post_body=None, cntr=None):     # 설정 검사 생략(응답 해석만 확인)
        self.config = None
        self.session = EM.PaperOnlySession()
        self._post_body = post_body
        self._cntr = cntr
        self.posted = []

    def _assert_paper(self):
        pass

    def _post(self, **kw):
        self.posted.append(kw)
        return _Resp(self._post_body)

    def _fetch_fill_history_raw(self, symbol):
        return list(self._cntr or [])


stub = PaperStub(CANCEL)
cr = stub.cancel_order("0148913", "005930", 1)
check("2) 취소 kt10003 실측 응답 → 접수(accepted)·취소 주문 0148928·원주문 0148913, 요청은 잔량 1주 명시",
      cr.accepted and not cr.is_ambiguous and cr.cancel_order_id == "0148928"
      and cr.body["base_orig_ord_no"] == "0148913" and stub.posted[0]["api_id"] == "kt10003"
      and stub.posted[0]["payload"]["cncl_qty"] == "1" and stub.posted[0]["payload"]["orig_ord_no"] == "0148913")


def rec(**kw) -> CancelRecord:
    base = dict(symbol="005930", order_id="0148913", quantity=1, base_quantity=0, requested_at=datetime(2026, 10, 13, 14, 5),
                accepted=True, cancel_order_id="0148928", body=dict(CANCEL))
    base.update(kw)
    return CancelRecord(**base)


a_ok = assess_buy_cancel(rec(), ev_after, 0)
a_open = assess_buy_cancel(rec(), build_order_status_evidence("0148913", "005930", OSO_BEFORE, CNTR_BEFORE), 0)
check("3) 취소 확정 = 우리 취소 접수(원주문번호 일치) + 원주문 행 없음 + 잔고 그대로 → CANCEL_CONFIRMED, "
      "취소 반영 전(미체결 그대로)은 STILL_OPEN",
      a_ok.verdict == CANCEL_CONFIRMED and "0148928" in a_ok.detail and a_open.verdict == STILL_OPEN)

neg = {
    "응답 불명": assess_buy_cancel(rec(accepted=False, ambiguous=True), ev_after, 0),
    "거절": assess_buy_cancel(rec(accepted=False), ev_after, 0),
    "원주문번호 다름": assess_buy_cancel(rec(body={**CANCEL, "base_orig_ord_no": "0148999"}), ev_after, 0),
    "잔고 변화": assess_buy_cancel(rec(), ev_after, 1),
    "다른 주문 조회": assess_buy_cancel(rec(order_id="0148914"), ev_after, 0),
}
partial_row = {**OSO_BEFORE[0], "ord_qty": "2", "oso_qty": "1", "cntr_qty": "1"}
neg["부분 체결 행"] = assess_buy_cancel(rec(quantity=2), build_order_status_evidence("0148913", "005930", [],
                                       [{**partial_row, "ord_stt": "체결"}]), 0)
check("4) 목록에 없다는 것만으로는 확정 안 함: 취소 응답 불명·거절·원주문번호 불일치·잔고 변화·다른 주문·"
      "부분 체결(미실측 행) → 모두 UNRESOLVED",
      all(v.verdict == UNRESOLVED for v in neg.values()))

filled_cntr = [{**CNTR_AFTER[1], "ord_no": "0148913", "cntr_pric": "206500", "ord_pric": "207000",
                "trde_tp": "보통"}]
a_fill = assess_buy_cancel(rec(), build_order_status_evidence("0148913", "005930", [], filled_cntr), 1)
check("5) 취소보다 체결이 먼저(전량 체결 행) → FILLED — 정상 체결 경로로 처리", a_fill.verdict == FILLED_BEFORE_CANCEL)

# 6. 체결 근거
d_sell = extract_fill_detail("0112348", "005930", CNTR_AFTER)
d_buy = extract_fill_detail("112293", "005930", CNTR_AFTER)
check("6) ka10076 실측 행 → 주문별 실제 체결가·수수료·세금: 0112348 매도 229,000·수수료 800·세금 457, "
      "0112293 매수 229,000·수수료 800·세금 0 (앞자리 0 없는 주문번호도 같은 주문)",
      d_sell and (d_sell.side, d_sell.quantity, d_sell.price, d_sell.commission, d_sell.tax) == ("SELL", 1, 229000, 800, 457)
      and d_buy and (d_buy.side, d_buy.price, d_buy.commission, d_buy.tax) == ("BUY", 229000, 800, 0))
none_cases = [
    extract_fill_detail("0148913", "005930", CNTR_AFTER),                       # 취소된 주문 — 행 없음
    extract_fill_detail("0112348", "000660", CNTR_AFTER),                       # 종목 다름
    extract_fill_detail("0112348", "005930", CNTR_AFTER + [dict(CNTR_AFTER[0])]),   # 같은 주문 2행(미실측)
    extract_fill_detail("0148913", "005930", [{**partial_row, "ord_stt": "체결"}]),   # 부분 체결
    extract_fill_detail("0112348", "005930", [{**CNTR_AFTER[0], "cntr_pric": "abc"}]),  # 해석 불가 가격
    extract_fill_detail("", "005930", CNTR_AFTER),
]
check("7) 체결 근거를 만들지 않는 경우: 행 없음·종목 다름·같은 주문 2행·부분 체결·가격 해석 불가·주문번호 없음 → None",
      all(x is None for x in none_cases))
stub2 = PaperStub(cntr=CNTR_AFTER)
check("8) PaperKiwoomBroker.get_fill_detail은 ka10076만 읽어 같은 결과(주문 없음)",
      stub2.get_fill_detail("0112348", "005930") == d_sell and not stub2.posted)

# 9. 원장 겹치기
T = date(2026, 10, 13)
buy = FillEvent("acct-swing|20261013|BUY|005930|0112293|1", "BUY", "005930", 1, 229_000, "BROKER_AVG", T,
                datetime(2026, 10, 13, 12, 7, 50), order_id="0112293")
sell = FillEvent("acct-swing|20261013|SELL|005930|0112348|1", "SELL", "005930", 1, 228_750, "ORDER_ESTIMATE", T,
                 datetime(2026, 10, 13, 12, 8, 1), order_id="0112348")
now = datetime(2026, 10, 13, 15, 0)
r_sell = record_for_event(sell, d_sell, origin="CONFIRM_TOOL", now=now)
r_buy = record_for_event(buy, d_buy, origin="CONFIRM_TOOL", now=now)
r_wrong = record_for_event(sell, d_buy, origin="CONFIRM_TOOL", now=now)
ov = apply_fill_evidence([buy, sell], [r_buy, r_sell])
eff = {e.kind: e for e in ov.events}
check("9) 원장 겹치기: 추정 매도 228,750 → 실측 229,000(BROKER_FILL), 수량·보유 계산은 그대로, 방향이 다른 근거는 만들지 않음",
      r_wrong is None and eff["SELL"].price == 229_000 and eff["SELL"].price_source == "BROKER_FILL"
      and "ORDER_ESTIMATE 228,750" in eff["SELL"].note and eff["BUY"].price == 229_000 and not ov.mismatched
      and apply_events(ov.events).positions() == apply_events([buy, sell]).positions())
bad = FillEvidenceRecord(sell.event_id, "0112348", "005930", "SELL", 2, 229_000, 800, 457, "ka10076", "CONFIRM_TOOL", now)
ov2 = apply_fill_evidence([buy, sell], [bad])
check("10) 수량이 원장과 다른 근거는 적용 안 하고 mismatched로 보고(원장 가격 유지)",
      [e.price for e in ov2.events] == [229_000, 228_750] and ov2.mismatched and not ov2.applied)

# 11. 근거 저장소
st = FillEvidenceStore(TMP / "fill_evidence.jsonl")
first = st.append(r_sell)
dup = st.append(FillEvidenceRecord(**{**r_sell.__dict__, "origin": "AUTO_RETRY", "recorded_at": datetime(2026, 10, 14)}))
try:
    st.append(FillEvidenceRecord(**{**r_sell.__dict__, "price": 230_000}))
    conflict = False
except FillEvidenceConflictError:
    conflict = True
with (TMP / "fill_evidence.jsonl").open("a", encoding="utf-8") as fh:
    fh.write('{"v": 1, "event_id": "torn')
loaded_torn = st.load()
st.append(r_buy)
torn_files = list(TMP.glob("fill_evidence.jsonl.torn-*"))
check("11) 근거 저장소: 같은 내용은 한 번만, 다른 가격은 덮어쓰지 않고 충돌, 끊긴 마지막 줄은 읽을 때 건너뛰고 다음 기록 전에 "
      ".torn 파일로 격리",
      first and dup is False and conflict and len(loaded_torn) == 1 and len(st.load()) == 2 and len(torn_files) == 1)
ro = FillEvidenceStore(TMP / "none" / "x.jsonl", read_only=True)
check("12) 읽기 전용 열기는 폴더를 만들지 않음(관리 창·보고서)", ro.load() == [] and not (TMP / "none").exists())

# 13. FillRecorder — 체결조회 행이 주문과 다르면 쓰지 않음
from domain.models import AccountBalance, Position  # noqa: E402
from domain.service.fill_recorder import FillRecorder  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402


def recorder_case(name, detail):
    led = FillLedgerStore(str(TMP / name / "ledger.jsonl"))
    ev_store = FillEvidenceStore(TMP / name / "fill_evidence.jsonl")
    fr = FillRecorder(led, scope="acct-swing", fill_lookup=lambda oid, sym: detail, evidence_store=ev_store)
    fr.track("005930", "SELL", "0112348", 1, 228_750, base_quantity=1, base_cost=229_000, order_date=T)
    fr.observe(AccountBalance(1, 1, []), trade_date=T, now=now)
    return led.load(), ev_store.load()


ok_l, ok_e = recorder_case("ok", d_sell)
wrong_side, we = recorder_case("side", FillDetail("0112348", "005930", "BUY", 1, 229_000, 800, 0))
wrong_qty, wq = recorder_case("qty", FillDetail("0112348", "005930", "SELL", 2, 229_000, 800, 457))
check("13) FillRecorder: 전량 체결 행이 맞으면 원장에 실제 체결가(BROKER_FILL)·근거 기록, 방향·수량이 다른 행은 쓰지 않고 추정가",
      (ok_l[0].price, ok_l[0].price_source) == (229_000, "BROKER_FILL") and ok_e[0].tax == 457
      and (wrong_side[0].price, wrong_side[0].price_source) == (228_750, "ORDER_ESTIMATE") and not we
      and (wrong_qty[0].price, wrong_qty[0].price_source) == (228_750, "ORDER_ESTIMATE") and not wq)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
