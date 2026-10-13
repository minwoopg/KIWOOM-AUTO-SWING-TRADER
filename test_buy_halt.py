# -*- coding: utf-8 -*-
"""신규 매수 중단 명령·실행기 상태 파일 (M3). 가짜 브로커·가짜 시계·임시 폴더, API 호출 없음."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.main import run_session  # noqa: E402
from domain.models import AccountBalance, BrokerOrderStatus, Position  # noqa: E402
from domain.strategy.interface import OrderIntent  # noqa: E402
from infra.storage.runner_status import halt_path, read_json, request_buy_halt, status_path  # noqa: E402
from testing_helpers import ScriptedBroker, build_minimal_settings  # noqa: E402
from tools import buy_halt as BH  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="buy_halt_"))


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class Sim(ScriptedBroker):
    def place_order(self, order):
        res = super().place_order(order)
        if res.accepted:
            q = self.positions.get(order.symbol, 0)
            self.positions[order.symbol] = q + (order.quantity if order.side.value == "BUY" else -order.quantity)
            self.status[res.order_id] = BrokerOrderStatus.FILLED
        return res

    def get_account_balance(self):
        return AccountBalance(100_000_000, 100_000_000,
                              [Position(s, q, 10_000) for s, q in self.positions.items() if q > 0])


class Script:
    strategy_id = "script"

    def __init__(self, items):
        self.items = list(items)

    def on_session_start(self, ctx):
        pass

    def on_session_end(self, ctx):
        pass

    def on_tick(self, ctx):
        out = []
        for t, fn in list(self.items):
            if ctx.now.time() >= t:
                self.items.remove((t, fn))
                r = fn(ctx)
                if r is not None:
                    out.append(r)
        return out


# 1. 요청 중복
s1 = build_minimal_settings(str(TMP / "req"))
c1, r1 = request_buy_halt(s1.storage.commands_dir, reason="점검", by="dashboard", now=datetime(2026, 10, 14, 9, 50))
c2, r2 = request_buy_halt(s1.storage.commands_dir, reason="또", by="dashboard", now=datetime(2026, 10, 14, 9, 51))
check("1) 신규 매수 중단 요청은 없을 때만 생성 — 중복 클릭은 같은 요청을 돌려줌(새 요청 없음)",
      c1 and not c2 and r1["request_id"] == r2["request_id"] and r2["reason"] == "점검")

# 2. 실행기 반영: 매수 차단, 매도 유지, 상태 파일
s2 = build_minimal_settings(str(TMP / "run"))
br = Sim()
br.positions = {"000660": 5}
from domain.position.fill_event import FillEvent  # noqa: E402
from infra.storage.fill_ledger import FillLedgerStore  # noqa: E402
from datetime import date  # noqa: E402
FillLedgerStore(s2.storage.fill_ledger_file).append(FillEvent("e0", "BUY", "000660", 5, 10_000, "BROKER_FILL",
                                                              date(2026, 10, 13), datetime(2026, 10, 13, 10, 0)))
from domain.position.swing_state import PositionMeta  # noqa: E402
from infra.storage.swing_state_store import SwingStateStore  # noqa: E402
st_store = SwingStateStore(s2.storage.state_file)
st0, hp0 = st_store.load()
st0.upsert_position_meta(PositionMeta("000660", strategy_id="script", origin="ORDER"))
st_store.save(st0, hp0)
snap = {}


def halt_now(ctx):
    request_buy_halt(s2.storage.commands_dir, reason="대시보드 시험", by="dashboard", now=ctx.now)
    return None


def look(ctx):
    snap["status"] = read_json(status_path(s2))[1]
    snap["view"] = BH.halt_view(s2)
    return None


def resume(ctx):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        snap["resume_rc"] = BH.main(["resume", "--note", "확인", "--confirm"], settings=s2, now=ctx.now)
    return None


clk = Clock(datetime(2026, 10, 14, 9, 50))
lg = Mock()
strat = Script([
    (time(10, 0), halt_now),
    (time(10, 2), look),
    (time(10, 3), lambda ctx: OrderIntent("005930", "BUY", 1, 70_000, reason="halt 중 매수")),
    (time(10, 4), lambda ctx: OrderIntent("000660", "SELL", 5, 10_000, reason="halt 중 정상 매도", forced=True)),
    (time(10, 6), resume),
    (time(10, 8), lambda ctx: OrderIntent("005930", "BUY", 1, 70_000, reason="재개 뒤 매수")),
])
summ = run_session(s2, br, lg, strategy=strat, clock=clk, sleep=clk.sleep,
                   should_stop=lambda: clk.t >= datetime(2026, 10, 14, 10, 15))
v = snap.get("view") or {}
st = snap.get("status") or {}
check("2) 요청 → 다음 폴링에서 실행기 반영(상태 파일 applied_at·같은 request_id), 화면 판정 '요청'과 '반영'을 구분",
      v.get("state") == "APPLIED" and v["request"]["requested_at"] == "2026-10-14T10:00:00"
      and st["buy_halt"]["applied_at"] == "2026-10-14T10:01:00" and st["buy_halt"]["request_id"] == v["request"]["request_id"])
check("3) 중단 중 매수는 BUY_HALTED로 전송 0, 정상 보유 매도는 전송·체결 — 재개(사람 --confirm) 뒤 매수 전송",
      summ.denied_codes.get("BUY_HALTED") == 1 and br.place_calls == [("000660", "SELL", 5), ("005930", "BUY", 1)]
      and snap.get("resume_rc") == 0 and not halt_path(s2.storage.commands_dir).exists()
      and len(list((Path(s2.storage.commands_dir) / "processed").glob("halt_new_buys.*.json"))) == 1)
final = read_json(status_path(s2))[1]
check("4) 상태 파일: run_id·pid·heartbeat·단계·실행 모드·코드 버전·전략 연결 여부·보유·카운터, 끝나면 FINISHED 단계",
      final["run_id"] == summ.run_id and final["stage"].startswith("FINISHED") and final["execution_mode"] == "MOCK_LOCAL"
      and final["strategy_connected"] is True and "HEAD" in final["code_version"] and final["heartbeat_at"]
      and isinstance(final["positions"], list) and final["counters"]["orders_sent"] == 2 and final["buy_halt"] is None)

# 5. 반영 전(실행기 없음) → 요청됨
s5 = build_minimal_settings(str(TMP / "pending"))
request_buy_halt(s5.storage.commands_dir, reason="x", by="cli", now=datetime(2026, 10, 14, 9, 0))
v5 = BH.halt_view(s5)
Path(s5.storage.commands_dir, "halt_new_buys.json").write_text("{깨짐", encoding="utf-8")
from infra.storage.runner_status import read_buy_halt  # noqa: E402
st5, _ = read_buy_halt(s5.storage.commands_dir)
check("5) 실행기가 아직 반영하지 않으면 '요청됨', 요청 파일이 깨져 있어도 중단으로 취급(안전 쪽)",
      v5["state"] == "REQUESTED" and v5["runner_status"] == "MISSING" and st5 == "UNREADABLE")

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
