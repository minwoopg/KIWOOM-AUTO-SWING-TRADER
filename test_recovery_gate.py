# -*- coding: utf-8 -*-
"""복구 이중 차단·계좌 잠금·데이터 모드 표식·원장 최소 정정 (M0 커밋 2, GPT 지시서 2026-10-13).

브로커는 테스트용 가짜, 경로는 모두 임시 폴더. API 호출 없음.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.main import StartupBlockedError, run_session, run_startup_checks  # noqa: E402
from domain.models import AccountBalance, Position  # noqa: E402
from domain.position.fill_event import FillEvent  # noqa: E402
from domain.risk.account_guard import GuardConfig, check_intent  # noqa: E402
from domain.service.lot_ledger import apply_events  # noqa: E402
from domain.strategy.interface import OrderIntent  # noqa: E402
from infra.broker import execution_mode as EM  # noqa: E402
from infra.storage import execution_guard as EG  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402
from infra.storage.ledger_review import LedgerReviewStore, review_path  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402
from tools import ledger_review as LR  # noqa: E402
from utils.trading_calendar import MarketPhase  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="recovery_gate_"))


def fresh(name: str):
    d = TMP / name
    d.mkdir()
    return build_minimal_settings(str(d))


# 1. 안전 한도 — 순수 함수
bal = AccountBalance(10_000_000, 10_000_000, [Position("005930", 10, 70_000)])
kw = dict(config=GuardConfig(), now=datetime(2026, 10, 14, 10, 0), phase=MarketPhase.REGULAR, balance=bal,
          positions=apply_events([FillEvent("e1", "BUY", "005930", 10, 70_000, "BROKER_FILL", date(2026, 10, 13),
                                            datetime(2026, 10, 13, 10, 0))]).positions(),
          prices={"005930": 70_000}, blocked_symbols=frozenset())
d_sell = check_intent(OrderIntent("005930", "SELL", 10, 70_000, forced=True), recovery_symbols={"005930"}, **kw)
d_buy = check_intent(OrderIntent("000660", "BUY", 1, 180_000), recovery_symbols={"005930"}, **kw)
d_ok = check_intent(OrderIntent("000660", "BUY", 1, 180_000), **kw)
d_sell_ok = check_intent(OrderIntent("005930", "SELL", 10, 70_000, forced=True), recovery_symbols={"000660"}, **kw)
check("1) 안전 한도: ERROR·orphan 종목은 강제 매도까지 RECOVERY_REQUIRED, 계좌의 신규 매수 전부 ACCOUNT_RECOVERY_REQUIRED, "
      "복구 필요 종목이 아닌 보유의 매도는 허용",
      (d_sell.allowed, d_sell.code) == (False, "RECOVERY_REQUIRED")
      and (d_buy.allowed, d_buy.code) == (False, "ACCOUNT_RECOVERY_REQUIRED") and d_ok.allowed and d_sell_ok.allowed)


# 2. 하루 수명주기 — 응답 불명 주문 → ERROR → 다른 종목 매수도 안전 한도에서 차단
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


s2 = fresh("runner")
br = ScriptedBroker()
br.next_result = ["ambiguous"]
clk = Clock(datetime(2026, 10, 14, 9, 50))
lg = Mock()
summ = run_session(s2, br, lg, strategy=Strat([(time(10, 0), OrderIntent("005930", "BUY", 1, 70_000)),
                                               (time(10, 5), OrderIntent("000660", "BUY", 1, 180_000)),
                                               (time(10, 6), OrderIntent("005930", "BUY", 1, 70_000))]),
                   clock=clk, sleep=clk.sleep, should_stop=lambda: clk.t >= datetime(2026, 10, 14, 10, 30))
check("2) 응답 불명 매수로 ERROR가 된 뒤: 같은 종목은 RECOVERY_REQUIRED, 다른 종목 신규 매수는 ACCOUNT_RECOVERY_REQUIRED — "
      "전송은 처음 1회뿐",
      br.place_calls == [("005930", "BUY", 1)] and summ.denied_codes.get("ACCOUNT_RECOVERY_REQUIRED") == 1
      and summ.denied_codes.get("RECOVERY_REQUIRED") == 1)

# 3. 계좌 라벨 잠금
s3 = fresh("lock")
inner = None
with EG.order_path_locks(s3):
    try:
        with EG.order_path_locks(s3):
            inner = "acquired"
    except RuntimeError as exc:
        inner = str(exc)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc3 = LR.main(["note", "005930", "--reason", "x"], settings=s3)
check("3) 주문 경로 잠금(상태 파일 + 계좌 라벨): 잡혀 있으면 실행기·정정 도구 모두 거부",
      "중복 실행 차단" in str(inner) and rc3 == 2 and "돌고 있음" in buf.getvalue()
      and not review_path(s3.storage.fill_ledger_file).exists())

# 4. 데이터 폴더 실행 모드 표식
s4 = fresh("marker")
a1 = EG.decide_data_mode(s4, "MOCK_LOCAL", ledger_empty=True, unresolved=False)
EG.commit_data_mode(s4, "MOCK_LOCAL", a1)
a2 = EG.decide_data_mode(s4, "MOCK_LOCAL", ledger_empty=False, unresolved=True)
a3 = EG.decide_data_mode(s4, "KIWOOM_PAPER", ledger_empty=True, unresolved=False)
EG.commit_data_mode(s4, "KIWOOM_PAPER", a3)
m4 = EG.read_marker(s4)
errs = []
for le, un in ((False, False), (True, True)):
    try:
        EG.decide_data_mode(s4, "MOCK_LOCAL", ledger_empty=le, unresolved=un)
        errs.append(None)
    except EG.DataModeMismatchError as exc:
        errs.append(str(exc))
check("4) 실행 모드 표식: 처음 CREATE, 같으면 SAME, 원장 비고 미해결 없을 때만 SWITCH(이력 남김), "
      "원장 기록·미해결 주문이 있으면 다른 모드로 시작 거부",
      (a1, a2, a3) == ("CREATE", "SAME", "SWITCH") and m4["mode"] == "KIWOOM_PAPER"
      and m4["history"][0]["mode"] == "MOCK_LOCAL" and all(e and "섞지 않음" in e for e in errs))


class PaperOK(EM.PaperKiwoomBroker):
    def authenticate(self):
        self.access_token = "t"

    def get_account_balance(self):
        return AccountBalance(1_000_000, 1_000_000, [])


s5 = fresh("startup")
FillLedgerStore(s5.storage.fill_ledger_file).append(
    FillEvent("e1", "BUY", "005930", 1, 70_000, "BROKER_FILL", date(2026, 10, 13), datetime(2026, 10, 13, 10, 0)))
EG.commit_data_mode(s5, "MOCK_LOCAL", "CREATE")
p5 = dataclasses.replace(s5, broker=dataclasses.replace(
    s5.broker, use_mock=False, base_url="https://mockapi.kiwoom.com", app_key="k", secret_key="s",
    account_number="12345678", account_scope_id="acct-swing"))


async def _ns(_s):
    return None


try:
    asyncio.run(run_startup_checks(p5, PaperOK(p5.broker), Mock(), sleep=_ns))
    r5 = "started"
except StartupBlockedError as exc:
    r5 = str(exc)
check("5) 기동: MOCK_LOCAL에서 원장이 쌓인 데이터 폴더로 KIWOOM_PAPER 시작 → 거부(표식 그대로)",
      "섞지 않음" in r5 and EG.read_marker(s5)["mode"] == "MOCK_LOCAL")

# 6. 원장 최소 정정 도구
s6 = fresh("review")
NOW = datetime(2026, 10, 14, 16, 0)
args = ["correct", "005930", "--side", "BUY", "--qty", "1", "--price", "251000", "--trade-date", "2026-10-14",
        "--order-id", "0012345", "--evidence", "HTS 체결 1주", "--reason", "재시작 중 체결 누락"]


def lr(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = LR.main(argv, settings=s6, now=NOW)
    return rc, buf.getvalue()


rc_prev, out_prev = lr(args)
ledger6 = FillLedgerStore(s6.storage.fill_ledger_file)
prev_ok = rc_prev == 0 and "미리보기" in out_prev and ledger6.load() == []
rc_ok, out_ok = lr(args + ["--confirm"])
ev6 = ledger6.load()
rv6 = LedgerReviewStore(review_path(s6.storage.fill_ledger_file)).load()
rc_dup, out_dup = lr(args + ["--confirm"])
rc_sell, out_sell = lr(["correct", "005930", "--side", "SELL", "--qty", "2", "--price", "250000", "--trade-date",
                        "2026-10-14", "--order-id", "0012346", "--evidence", "e", "--reason", "r", "--confirm"])
rc_note, _ = lr(["note", "005930", "--reason", "HTS와 일치"])
rv6b = LedgerReviewStore(review_path(s6.storage.fill_ledger_file)).load()
check("6) 정정: 미리보기는 아무것도 쓰지 않음, --confirm은 BROKER_FILL 체결 1건 추가 + 검토 기록(근거·사유·전후 수량·계좌 라벨)",
      prev_ok and rc_ok == 0 and len(ev6) == 1 and ev6[0].price_source == "BROKER_FILL"
      and ev6[0].order_id == "0012345" and "MANUAL_CORRECTION" in ev6[0].note
      and len(rv6) == 1 and rv6[0]["action"] == "FILL_CORRECTION" and rv6[0]["ledger_qty_before"] == 0
      and rv6[0]["ledger_qty_after"] == 1 and rv6[0]["account_scope"] and rv6[0]["evidence"] == "HTS 체결 1주")
check("7) 같은 주문번호·수량 정정은 두 번 들어가지 않음, 원장 보유보다 큰 매도 정정 거부, 메모는 원장을 바꾸지 않음",
      rc_dup == 2 and "이미 원장에" in out_dup and rc_sell == 2 and "원장 보유 1주" in out_sell
      and rc_note == 0 and len(ledger6.load()) == 1 and [r["action"] for r in rv6b] == ["FILL_CORRECTION", "NOTE"])
Path(s6.storage.fill_ledger_file).write_text('{"broken": \n{"x":1}\n', encoding="utf-8")
rc_bad, out_bad = lr(["status"])
check("8) 원장 손상이면 정정 도구도 거부(2) — 자동 복구하지 않음", rc_bad == 2 and "신뢰할 수 없음" in out_bad)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
