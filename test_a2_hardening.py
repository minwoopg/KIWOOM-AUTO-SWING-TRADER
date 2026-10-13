# -*- coding: utf-8 -*-
"""A2 보완 (GPT 검토 2026-10-13, main 0aea3ca) — R1 주문번호 검사, R2 근거 저장 재시도, 취소 응답 식별,
R3 번들 재현, R4 표시. 임시 폴더만, API 호출 없음."""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.models import AccountBalance  # noqa: E402
from domain.position.fill_event import FillEvent  # noqa: E402
from domain.service.buy_cancel import CANCEL_CONFIRMED, UNRESOLVED, CancelRecord, assess_buy_cancel  # noqa: E402
from domain.service.fill_evidence import FillDetail, FillEvidenceRecord, apply_fill_evidence  # noqa: E402
from domain.service.fill_recorder import FillRecorder  # noqa: E402
from infra.broker.kiwoom_order_status import build_order_status_evidence  # noqa: E402
from infra.storage.fill_evidence import FillEvidenceStore  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="a2_hardening_"))
T = date(2026, 10, 13)
T0 = datetime(2026, 10, 13, 12, 8)
GOOD = FillDetail("0112348", "005930", "SELL", 1, 229_000, 800, 457)


class Lookup:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, oid, sym):
        self.calls.append((oid, sym))
        a = self.answers.pop(0) if self.answers else None
        if isinstance(a, Exception):
            raise a
        return a


class FlakyStore(FillEvidenceStore):
    def __init__(self, path, fails):
        super().__init__(path)
        self.fails = fails
        self.tries = 0

    def append(self, rec):
        self.tries += 1
        if self.fails > 0:
            self.fails -= 1
            raise OSError("디스크 잠김")
        return super().append(rec)


def recorder(name, lookup, store=None):
    led = FillLedgerStore(str(TMP / name / "ledger.jsonl"))
    store = store if store is not None else FillEvidenceStore(TMP / name / "fill_evidence.jsonl")
    fr = FillRecorder(led, scope="acct-swing", fill_lookup=lookup, evidence_store=store)
    return fr, led, store


def sell(fr, oid="0112348", sym="005930", base=1):
    fr.track(sym, "SELL", oid, 1, 228_750, base_quantity=base, base_cost=229_000 * base, order_date=T)


def bal(**held):
    from domain.models import Position
    return AccountBalance(1, 1, [Position(s, q, 229_000) for s, q in held.items() if q > 0])


# R1. 다른 주문번호의 같은 종목·방향·수량 근거 → 원장에 쓰지 않음
lk = Lookup([FillDetail("9999999", "005930", "SELL", 1, 999_999, 1, 1)])
fr1, led1, st1 = recorder("r1", lk)
sell(fr1)
fr1.observe(bal(), trade_date=T, now=T0)
e1 = led1.load()
check("R1) 다른 주문번호(9999999) 근거는 원장에 쓰지 않음 — 추정가 228,750(ORDER_ESTIMATE) 그대로, 근거 기록 없음, "
      "재확인 목록에 남음",
      len(e1) == 1 and (e1[0].price, e1[0].price_source) == (228_750, "ORDER_ESTIMATE") and st1.load() == []
      and list(fr1.unconfirmed) == [e1[0].event_id] and len(lk.calls) == 1)
lk1b = Lookup([FillDetail("112348", "005930", "SELL", 1, 229_000, 800, 457)])
fr1b, led1b, _ = recorder("r1b", lk1b)
sell(fr1b)
fr1b.observe(bal(), trade_date=T, now=T0)
check("R1-b) 앞자리 0만 다른 같은 주문번호는 같은 주문 — BROKER_FILL", led1b.load()[0].price_source == "BROKER_FILL")

# R2. 근거 저장 실패 → 원장 사건은 하나, 근거는 저장 재시도(조회 다시 안 함), 이후 성공
lk2 = Lookup([GOOD])
fl = FlakyStore(TMP / "r2" / "fill_evidence.jsonl", fails=2)
fr2, led2, _ = recorder("r2", lk2, fl)
sell(fr2)
t = T0
fr2.observe(bal(), trade_date=T, now=t)
pend_after_first = list(fr2.pending_saves)
for i in range(8):
    t += timedelta(seconds=60)
    fr2.observe(bal(), trade_date=T, now=t)
check("R2-a) 첫 저장 실패: 원장은 BROKER_FILL 한 건, 근거는 저장 재시도 목록 → 간격을 늘려 다시 저장, 3번째에 성공, "
      "체결조회는 처음 한 번뿐",
      [e.price_source for e in led2.load()] == ["BROKER_FILL"] and pend_after_first and fr2.pending_saves == {}
      and len(fl.load()) == 1 and fl.load()[0].tax == 457 and len(lk2.calls) == 1 and fl.tries == 3)

# R2-b. 저장이 계속 실패 → 상한에서 포기, 그동안 다른 사건의 체결조회 재확인은 막히지 않음
lk3 = Lookup([GOOD, RuntimeError("429"), FillDetail("0112400", "000660", "SELL", 1, 180_500, 700, 300)])
fl3 = FlakyStore(TMP / "r2b" / "fill_evidence.jsonl", fails=10_000)
fr3, led3, _ = recorder("r2b", lk3, fl3)
sell(fr3)
fr3.track("000660", "SELL", "0112400", 1, 180_000, base_quantity=1, base_cost=180_000, order_date=T)
t = T0
fr3.observe(bal(), trade_date=T, now=t)                # 005930 조회 성공·저장 실패, 000660은 조회 한도로 미뤄짐
saw_other = False
for i in range(240):                                  # 간격 최대 15분 × 상한 10회 — 4시간 안에 끝
    t += timedelta(seconds=60)
    fr3.observe(bal(), trade_date=T, now=t)
    saw_other = saw_other or any(c[1] == "000660" for c in lk3.calls)
check("R2-b) 저장 실패가 계속되면 상한(10회)에서 포기(원장 사건 중복 없음), 그동안 다른 종목 체결가 재확인은 진행",
      fr3.pending_saves == {} and fl3.tries >= FillRecorder.SAVE_RETRY_LIMIT and saw_other
      and len(led3.load()) == 2 and all(c != ("0112348", "005930") for c in lk3.calls[1:]))

# R2-c. 조회 실패는 간격을 늘려 재시도, 상한 뒤 포기
lk4 = Lookup([RuntimeError("429")] * 20)
fr4, led4, st4 = recorder("r2c", lk4)
sell(fr4)
t = T0
times = []
for i in range(40):
    n = len(lk4.calls)
    fr4.observe(bal(), trade_date=T, now=t)
    if len(lk4.calls) > n:
        times.append(t)
    t += timedelta(seconds=60)
gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
check("R2-c) 체결조회 실패 재확인은 간격이 늘어나고(60→120→240…) 5회에서 멈춤, 원장은 추정가 한 건",
      len(times) == 5 and gaps == sorted(gaps) and gaps[-1] > gaps[0] and fr4.unconfirmed == {}
      and len(led4.load()) == 1)

# 취소 응답 식별
FX = Path("tests/fixtures/m1_20261013")
body = json.loads((FX / "cancel_kt10003.json").read_text(encoding="utf-8"))["body"]
oso_after = json.loads((FX / "after_cancel_ka10075.json").read_text(encoding="utf-8"))["body"]["oso"]
cntr_after = json.loads((FX / "after_cancel_ka10076.json").read_text(encoding="utf-8"))["body"]["cntr"]
ev_after = build_order_status_evidence("0148913", "005930", oso_after, cntr_after)


def rec(b):
    return CancelRecord(symbol="005930", order_id="0148913", quantity=1, base_quantity=0, requested_at=T0,
                        accepted=True, cancel_order_id="0148928", body=b)


ok = assess_buy_cancel(rec(dict(body)), ev_after, 0)
bad = {
    "원주문번호 없음": {k: v for k, v in body.items() if k != "base_orig_ord_no"},
    "원주문번호 0": {**body, "base_orig_ord_no": "0000000"},
    "취소 수량 없음": {k: v for k, v in body.items() if k != "cncl_qty"},
    "취소 수량 0": {**body, "cncl_qty": "000000000000"},
    "취소 수량 다름": {**body, "cncl_qty": "000000000002"},
}
verdicts = {k: assess_buy_cancel(rec(v), ev_after, 0) for k, v in bad.items()}
check("C) 취소 응답 식별: 실측 응답은 확정, 원주문번호 누락·0, 취소 수량 누락·0·요청 잔량과 다름은 모두 미확정",
      ok.verdict == CANCEL_CONFIRMED and all(v.verdict == UNRESOLVED for v in verdicts.values())
      and "원주문번호" in verdicts["원주문번호 없음"].detail and "취소 수량" in verdicts["취소 수량 다름"].detail)

# R3. 번들 — 보정 가격 재현
from app.reports import export_bundle  # noqa: E402
from testing_helpers import build_minimal_settings  # noqa: E402

d5 = TMP / "bundle"
d5.mkdir()
s5 = build_minimal_settings(str(d5))
led5 = FillLedgerStore(s5.storage.fill_ledger_file)
buy = FillEvent("acct|20261013|BUY|005930|0112293|1", "BUY", "005930", 1, 229_000, "BROKER_AVG", T,
                datetime(2026, 10, 13, 12, 7, 50), order_id="0112293")
sl = FillEvent("acct|20261013|SELL|005930|0112348|1", "SELL", "005930", 1, 228_750, "ORDER_ESTIMATE", T,
               datetime(2026, 10, 13, 12, 8, 1), order_id="0112348")
led5.append(buy)
led5.append(sl)
FillEvidenceStore(Path(s5.storage.fill_ledger_file).with_name("fill_evidence.jsonl")).append(FillEvidenceRecord(
    sl.event_id, "0112348", "005930", "SELL", 1, 229_000, 800, 457, "ka10076", "CONFIRM_TOOL", T0))
Path(s5.storage.state_file).with_name("buy_cancels.jsonl").write_text(
    json.dumps({"event": "CANCEL_CONFIRMED", "order_id": "0148913"}, ensure_ascii=False) + "\n", encoding="utf-8")
zp = export_bundle(s5, T, root=Path("."), out_dir=TMP / "exports")
z = zipfile.ZipFile(zp)
ev_z = [FillEvent.from_dict(json.loads(x)) for x in z.read("fill_ledger.jsonl").decode().splitlines() if x]
rec_z = [FillEvidenceRecord.from_dict(json.loads(x)) for x in z.read("fill_evidence.jsonl").decode().splitlines() if x]
replay = apply_fill_evidence(ev_z, rec_z).events
live = apply_fill_evidence(led5.load(), FillEvidenceStore(
    Path(s5.storage.fill_ledger_file).with_name("fill_evidence.jsonl")).load()).events
man = json.loads(z.read("manifest.json"))
check("R3) 표준 번들에 체결 근거·취소 감사 포함, 번들만으로 보정 매도가 229,000 재현(운영 자료와 같음), manifest에 해시",
      [e.price for e in replay] == [e.price for e in live] == [229_000, 229_000]
      and "CANCEL_CONFIRMED" in z.read("buy_cancels.jsonl").decode()
      and "sha256" in man["files"]["fill_evidence.jsonl"] and not man["files"]["fill_evidence.jsonl"]["empty"])

# R3-b / R4. 관리 창 — 다운로드 번들, 주문 탭 취소·체결가 확인, 실제 비용 후 손익
from config.settings import load_settings  # noqa: E402
from dashboard import data as D  # noqa: E402
from dashboard.sample_data import build  # noqa: E402

NOW = datetime(2026, 10, 14, 10, 30)
R6 = build(TMP / "dash", "normal", NOW)
S6 = load_settings(R6 / "config/settings.yaml")
P6 = D.paths_from_settings(S6, R6)
FillEvidenceStore(P6.fill_ledger.with_name("fill_evidence.jsonl")).append(FillEvidenceRecord(
    "acct-swing|20261013|BUY|005930|0112293|1", "0112293", "005930", "BUY", 1, 229_000, 800, 0, "ka10076",
    "CONFIRM_TOOL", NOW))
FillEvidenceStore(P6.fill_ledger.with_name("fill_evidence.jsonl")).append(FillEvidenceRecord(
    "acct-swing|20261013|SELL|005930|0112348|1", "0112348", "005930", "SELL", 1, 229_000, 800, 457, "ka10076",
    "CONFIRM_TOOL", NOW))
st = json.loads(P6.runner_status.read_text(encoding="utf-8"))
st["buy_cancels"] = [{"symbol": "005930", "order_id": "0148913", "quantity": 1, "accepted": True, "ambiguous": False,
                      "cancel_order_id": "0148928", "message": "", "attempts": 1, "resolved": "",
                      "requested_at": "2026-10-14T09:30:05"}]
st["fill_evidence"] = {"lookup_pending": [{"event_id": "x", "symbol": "000660", "side": "SELL", "order_id": "0112400",
                                           "attempts": 2, "next_at": "2026-10-14T10:32:00"}],
                       "save_pending": [], "last_error": "000660 SELL 0112400: 체결조회 실패 RuntimeError: 429"}
P6.runner_status.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
snap = D.snapshot(P6, S6, NOW)
o = snap["orders"]
real = snap["costs"].data["realized"][0]
zb = zipfile.ZipFile(io.BytesIO(D.download_bundle(P6, NOW.date(), D.Redactor(S6), {})))
check("R4) 주문 탭: '미구현' 문구 없음, 지정가 취소 '접수 — 확정 대기', 체결가 미확인(조회 2/5)·최근 문제 표시",
      not any("미구현" in n for n in o.notes) and any(r["결과"].startswith("취소 접수") for r in o.data)
      and any("체결가·비용 미확인" in r["결과"] for r in o.data) and any("429" in n for n in o.notes))
check("R4-b) 실현 손익: 양쪽 근거가 있으면 실제 비용 2,057원과 비용 후 손익 표시(모델 추정과 구분)",
      real["실제 비용"] == 2_057 and real["손익(실제 비용 후)"] == real["손익(비용 전)"] - 2_057)
check("R3-b) 관리 창 다운로드 zip에도 원장·체결 근거 포함",
      {"data/fill_ledger.jsonl", "data/fill_evidence.jsonl"} <= set(zb.namelist()))

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok_ in results if ok_)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
