# -*- coding: utf-8 -*-
"""M1 수동 1주 주문 도구 (M0 커밋 3, GPT 지시서 2026-10-13).

가짜 브로커·가짜 시계·임시 폴더. 키움 서버 호출 없음 — 실제 모의 주문 실측은 사용자 PC에서.
"""
from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from domain.models import AccountBalance, BrokerOrderStatus, Position  # noqa: E402
from domain.service.lot_ledger import apply_events  # noqa: E402
from infra.broker.response_recorder import RecordingPaperKiwoomBroker, redact  # noqa: E402
from infra.storage.execution_guard import order_path_locks  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402
from tools import paper_order as PO  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="paper_order_"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class Sim(ScriptedBroker):
    """접수 뒤 다음 잔고 조회에서 체결, 주문 조회 FILLED."""

    def __init__(self):
        super().__init__()
        self.fill = True
        self.extra: dict[str, int] = {}

    def place_order(self, order):
        res = super().place_order(order)
        if res.accepted and self.fill:
            q = self.positions.get(order.symbol, 0)
            self.positions[order.symbol] = q + (order.quantity if order.side.value == "BUY" else -order.quantity)
            self.status[res.order_id] = BrokerOrderStatus.FILLED
        return res

    def get_account_balance(self):
        pos = {**self.positions, **self.extra}
        return AccountBalance(100_000_000, 100_000_000, [Position(s, q, 10_000) for s, q in pos.items() if q > 0])


def env(name):
    d = TMP / name
    d.mkdir()
    return build_minimal_settings(str(d))


def run(settings, argv, broker, at, **kw):
    clk = Clock(at)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = PO.main(argv, settings=settings, broker=broker, clock=clk, sleep=clk.sleep, logger=Mock(), **kw)
    return rc, buf.getvalue(), clk


def reg(settings):
    return PO.load_registry(settings)


T = datetime(2026, 10, 14, 10, 0)

# 1. 실행 모드
s1 = env("mode")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc1 = PO.main(["buy-one", "005930", "--command-id", "m1-a"], settings=s1)
check("1) MOCK_LOCAL 설정에서는 M1 도구 거부(키움 모의투자 KIWOOM_PAPER만), 기록 없음",
      rc1 == 2 and "KIWOOM_PAPER" in buf.getvalue() and reg(s1) == [])

# 2. 미리보기
s2 = env("preview")
b2 = Sim()
rc2, out2, _ = run(s2, ["buy-one", "005930", "--command-id", "m1-prev"], b2, T)
check("2) --execute 없으면 미리보기 — 브로커 호출·주문·명령 기록 없음",
      rc2 == 0 and "미리보기" in out2 and b2.place_calls == [] and reg(s2) == [])

# 3. 매수 → 체결 → 원장, 같은 명령 ID 재실행 차단, 매도 → 원장 0
s3 = env("roundtrip")
b3 = Sim()
rc3, out3, _ = run(s3, ["buy-one", "005930", "--command-id", "m1-buy", "--execute"], b3, T)
ev3 = FillLedgerStore(s3.storage.fill_ledger_file).load()
r3 = reg(s3)
check("3) buy-one --execute: 1주 시장가 1회 전송 → 체결 → 원장 BUY 1주 기록 → FILLED, 명령 기록 STARTED·FINISHED(주문번호)",
      rc3 == 0 and b3.place_calls == [("005930", "BUY", 1)] and [(e.kind, e.quantity) for e in ev3] == [("BUY", 1)]
      and [r["event"] for r in r3] == ["STARTED", "FINISHED"] and r3[1]["status"] == "FILLED" and r3[1]["order_id"])
rc3b, out3b, _ = run(s3, ["buy-one", "005930", "--command-id", "m1-buy", "--execute"], b3, T + timedelta(minutes=5))
check("4) 같은 명령 ID는 결과와 상관없이 다시 실행하지 않음 — 추가 전송 없음",
      rc3b == 2 and "이미 기록" in out3b and len(b3.place_calls) == 1 and len(reg(s3)) == 2)
rc3c, out3c, _ = run(s3, ["sell-held", "005930", "--command-id", "m1-sell", "--execute"], b3, T + timedelta(minutes=10))
led3 = apply_events(FillLedgerStore(s3.storage.fill_ledger_file).load())
check("5) sell-held --execute: 원장 보유 1주 매도 → 주문 조회 FILLED → 원장 0주(당일 왕복)",
      rc3c == 0 and b3.place_calls[-1] == ("005930", "SELL", 1) and led3.positions() == {} and reg(s3)[-1]["status"] == "FILLED")
rc3d, out3d, _ = run(s3, ["sell-held", "005930", "--command-id", "m1-sell2", "--execute"], b3, T + timedelta(minutes=15))
check("6) 원장 보유가 없으면 sell-held 거부(이 프로그램이 기록한 보유만 팜) — 기록·전송 없음",
      rc3d == 2 and "원장 보유 0주" in out3d and len(b3.place_calls) == 2 and not any(r["command_id"] == "m1-sell2" for r in reg(s3)))

# 7. 시간대 밖 → 안전 한도 차단
s7 = env("window")
b7 = Sim()
rc7, out7, _ = run(s7, ["buy-one", "005930", "--command-id", "m1-early", "--execute"], b7, datetime(2026, 10, 14, 9, 2))
check("7) 09:02(신규 주문 시간대 밖) → 안전 한도 DENIED(OUTSIDE_ORDER_WINDOW), 전송 없음, 결과 기록",
      rc7 == 1 and b7.place_calls == [] and reg(s7)[-1]["status"] == "DENIED" and "OUTSIDE_ORDER_WINDOW" in reg(s7)[-1]["reason"])

# 8. 체결 없음 → 시간 안에 종료 확인 못함, 재전송 없음
s8 = env("nofill")
b8 = Sim()
b8.fill = False
rc8, out8, clk8 = run(s8, ["buy-one", "005930", "--command-id", "m1-nofill", "--execute", "--max-wait", "60"], b8, T)
check("8) 체결이 안 되면 최대 대기(60초) 뒤 UNRESOLVED — 재전송 없음, 원장 기록 없음, 사람 확인 안내",
      rc8 == 1 and b8.place_calls == [("005930", "BUY", 1)] and reg(s8)[-1]["status"] == "UNRESOLVED"
      and FillLedgerStore(s8.storage.fill_ledger_file).load() == [] and "재전송하지 않음" in out8)
rc8b, out8b, _ = run(s8, ["buy-one", "005930", "--command-id", "m1-after", "--execute"], b8, T + timedelta(minutes=5))
check("9) 그 뒤 새 명령 ID도 기동 점검에서 미해결 주문 흔적으로 REFUSED — 추가 전송 없음",
      rc8b == 1 and len(b8.place_calls) == 1 and reg(s8)[-1]["status"] == "REFUSED" and "미해결" in reg(s8)[-1]["reason"])

# 10. 응답 불명
s10 = env("ambiguous")
b10 = Sim()
b10.next_result = ["ambiguous"]
rc10, out10, _ = run(s10, ["buy-one", "005930", "--command-id", "m1-amb", "--execute"], b10, T)
check("10) 응답 불명(타임아웃) → NOT_ACCEPTED·ambiguous 기록, 재전송 없음",
      rc10 == 1 and len(b10.place_calls) == 1 and reg(s10)[-1]["status"] == "NOT_ACCEPTED" and reg(s10)[-1]["ambiguous"])

# 11. 원장 밖 보유가 있는 계좌
s11 = env("foreign")
b11 = Sim()
b11.extra = {"000660": 3}
rc11, out11, _ = run(s11, ["buy-one", "005930", "--command-id", "m1-foreign", "--execute"], b11, T)
check("11) 계좌에 원장 밖 보유가 있으면(장부 대조 불일치) REFUSED — 전송 없음",
      rc11 == 1 and b11.place_calls == [] and reg(s11)[-1]["status"] == "REFUSED" and "장부 대조" in reg(s11)[-1]["reason"])

# 12. 잠금
s12 = env("lock")
with order_path_locks(s12):
    rc12, out12, _ = run(s12, ["buy-one", "005930", "--command-id", "m1-lock", "--execute"], Sim(), T)
check("12) 실행기·정정 도구가 잠금을 잡고 있으면 거부 — 기록 없음", rc12 == 2 and "잠금" in out12 and reg(s12) == [])

# 13. 응답 원문 비밀값 제거
s13 = env("redact")
p13 = dataclasses.replace(s13, broker=dataclasses.replace(
    s13.broker, use_mock=False, base_url="https://mockapi.kiwoom.com", app_key="APPKEY123456", secret_key="SECRET999999",
    account_number="81234567", account_scope_id="acct-swing"))
rb = RecordingPaperKiwoomBroker(p13.broker)
rb.access_token = "TOKENabcdef123"
rb.recorded = [
    {"api_id": "", "path": "/oauth2/token", "status": 200, "headers": {}, "body": {"token": "TOKENabcdef123", "return_code": 0}},
    {"api_id": "kt10000", "path": "/api/dostk/ordr", "status": 200, "headers": {"api-id": "kt10000"},
     "body": {"ord_no": "0012345", "return_msg": "계좌 81234567 매수주문 완료", "return_code": 0}},
    {"api_id": "kt00018", "path": "/api/dostk/acnt", "status": 200, "headers": {},
     "body": {"acnt_no": "81234567", "acnt_evlt_remn_indv_tot": [{"stk_cd": "A005930", "rmnd_qty": "1"}]}},
]
files = rb.save_fixtures(TMP / "fx")
blob = "".join(f.read_text(encoding="utf-8") for f in files)
check("13) 응답 원문 저장: 토큰·앱키·계좌번호는 어디에 있든 가림, 주문번호·종목·잔고 목록은 남김",
      len(files) == 3 and "TOKENabcdef123" not in blob and "81234567" not in blob and "0012345" in blob
      and "A005930" in blob and "rmnd_qty" in blob
      and redact({"x": "APPKEY123456"}, rb.secrets()) == {"x": "***"})

# 14. 첫 잔고 조회 429 → 다시 조회(주문 의도 전이라 중복 없음) → 정상 체결
class Flaky(Sim):
    def __init__(self):
        super().__init__()
        self.balance_calls = 0
        self.fail_on = {3}            # 기동 점검 1회 + 시세 뒤 첫 폴링 조회에서 429

    def get_account_balance(self):
        self.balance_calls += 1
        if self.balance_calls in self.fail_on:
            raise RuntimeError("kiwoom request failed: api_id=kt00001, http=429, body={'return_code': 5}")
        return super().get_account_balance()


s14 = env("flaky")
b14 = Flaky()
b14.fail_on = {2}
rc14, out14, _ = run(s14, ["buy-one", "005930", "--command-id", "m1-flaky", "--execute"], b14, T)
s14b = env("flaky_all")
b14b = Flaky()
b14b.fail_on = {2, 3, 4}
rc14b, out14b, _ = run(s14b, ["buy-one", "005930", "--command-id", "m1-flaky2", "--execute"], b14b, T)
check("14) 시험 직전 잔고 조회가 429면 간격을 두고 다시 조회(최대 3번) → 정상 체결·전송 1회, 계속 실패하면 REFUSED에 실제 오류(429) 표시·전송 0",
      rc14 == 0 and b14.place_calls == [("005930", "BUY", 1)] and reg(s14)[-1]["status"] == "FILLED"
      and rc14b == 1 and b14b.place_calls == [] and reg(s14b)[-1]["status"] == "REFUSED"
      and "429" in reg(s14b)[-1]["reason"] and "3회" in reg(s14b)[-1]["reason"])

# 15. check-orders — 읽기 전용 주문·체결 조회
class Q(Sim):
    def __init__(self):
        super().__init__()
        self.q = []

    def _fetch_open_orders_raw(self, sym):
        self.q.append(("ka10075", sym))
        return []

    def _fetch_fill_history_raw(self, sym):
        self.q.append(("ka10076", sym))
        return [{"ord_no": "0112293", "stk_cd": "005930", "io_tp_nm": "+매수", "ord_stt": "체결", "ord_qty": "1",
                 "cntr_qty": "1", "oso_qty": "0", "cntr_pric": "229000", "trde_tp": "시장가"},
                {"ord_no": "0112348", "stk_cd": "005930", "io_tp_nm": "-매도", "ord_stt": "체결", "ord_qty": "1",
                 "cntr_qty": "1", "oso_qty": "0", "cntr_pric": "229000", "trde_tp": "시장가"}]


s15 = env("check")
PO.append_registry(s15, {"command_id": "a", "event": "FINISHED", "order_id": "0112293", "symbol": "005930",
                         "at": "2026-10-14T10:00:05"})
PO.append_registry(s15, {"command_id": "b", "event": "FINISHED", "order_id": "0112348", "symbol": "005930",
                         "at": "2026-10-14T10:01:05"})
before15 = (Path(s15.storage.state_file).parent / "paper_orders.jsonl").read_bytes()
b15 = Q()
rc15, out15, _ = run(s15, ["check-orders"], b15, T)
b15b = Q()
rc15b, out15b, _ = run(s15, ["check-orders", "--order", "0099999:005930"], b15b, T)
check("15) check-orders: 그날 기록의 주문번호를 종목당 ka10075·ka10076 한 번씩 조회 → 주문번호별 FILLED·체결가, "
      "주문·원장·기록 변경 없음, 없는 주문번호는 다른 주문에 붙이지 않음(UNKNOWN, 종료 1)",
      rc15 == 0 and b15.q == [("ka10075", "005930"), ("ka10076", "005930")] and b15.place_calls == []
      and "0112293 005930 FILLED BUY 요청 1 체결 1 @ 229000" in out15 and "0112348 005930 FILLED SELL" in out15
      and (Path(s15.storage.state_file).parent / "paper_orders.jsonl").read_bytes() == before15
      and FillLedgerStore(s15.storage.fill_ledger_file).load() == []
      and rc15b == 1 and "0099999 005930 UNKNOWN" in out15b)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
