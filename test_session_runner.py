# -*- coding: utf-8 -*-
"""하루 수명주기 회귀 테스트 (스윙 분리 6라운드, 2026-09-28).

가짜 시계로 하루 전체(장전 대기 → 장중 폴링 → 마감 후 대조 → 종료)를 돌립니다.
테스트 전용 전략(ScriptStrategy)이 정해진 시각에 주문 의도를 내고,
시뮬레이션 브로커가 다음 잔고 조회 때 체결을 반영합니다.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.main import run_session
from domain.models import AccountBalance, OrderResult, Position
from domain.position.lifecycle import PositionLifecycle as L
from domain.service.lot_ledger import apply_events
from domain.position.position_book import opening_events_from_balance
from domain.strategy.interface import OrderIntent
from infra.market_data.quote_source import StaticQuoteSource
from infra.storage.fill_ledger import FillLedgerStore
from infra.storage.swing_state_store import SwingStateStore
from testing_helpers import ScriptedBroker, build_minimal_settings

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


class SimBroker(ScriptedBroker):
    """주문 접수 후 다음 잔고 조회에서 체결을 반영. fill_ratio로 부분체결 흉내."""

    def __init__(self, prices=None):
        super().__init__()
        self.cash = 100_000_000
        self.prices = prices or {"005930": 70_000, "000660": 180_000}
        self.avg: dict[str, int] = {}
        self.pending: list[tuple] = []
        self.fill_ratio = 1.0
        self.fail_balance_until: datetime | None = None
        self.clock = None

    def place_order(self, order):
        res = super().place_order(order)
        if res.accepted:
            self.pending.append((order.symbol, order.side.value, order.quantity))
        return res

    def get_account_balance(self):
        if self.fail_balance_until and self.clock and self.clock() < self.fail_balance_until:
            raise RuntimeError("kiwoom request failed: http=500")
        still = []
        for sym, side, qty in self.pending:
            q = int(qty * self.fill_ratio)
            cur = self.positions.get(sym, 0)
            px = self.prices.get(sym, 10_000)
            if side == "BUY":
                total = cur * self.avg.get(sym, px) + q * px
                self.positions[sym] = cur + q
                if cur + q:
                    self.avg[sym] = total // (cur + q)
            else:
                self.positions[sym] = cur - q
            if q < qty:
                still.append((sym, side, qty - q)) if self.fill_ratio == 1.0 else None
        self.pending = still
        return AccountBalance(self.cash, self.cash, [
            Position(s, q, self.avg.get(s, self.prices.get(s, 10_000))) for s, q in self.positions.items() if q > 0])


class ScriptStrategy:
    strategy_id = "script"

    def __init__(self, script):
        self.script = list(script)        # [(time, intent or callable)]
        self.started = 0
        self.ended = 0
        self.ticks = 0
        self.raise_at: time | None = None

    def on_session_start(self, ctx):
        self.started += 1

    def on_tick(self, ctx):
        self.ticks += 1
        if self.raise_at and ctx.now.time() >= self.raise_at:
            self.raise_at = None
            raise ValueError("strategy bug")
        out = []
        for item in list(self.script):
            t, intent = item
            if ctx.now.time() >= t:
                self.script.remove(item)
                out.append(intent(ctx) if callable(intent) else intent)
        return out

    def on_session_end(self, ctx):
        self.ended += 1


def day(start, strategy=None, broker=None, stop_at=None, settings_fn=None, quote_source=None):
    tmp = tempfile.mkdtemp()
    settings = build_minimal_settings(tmp)
    if settings_fn:
        settings = settings_fn(settings)
    clock = FakeClock(start)
    broker = broker or SimBroker()
    broker.clock = clock
    logger = Mock()
    summary = run_session(settings, broker, logger, strategy=strategy, clock=clock, sleep=clock.sleep,
                          should_stop=(lambda: stop_at is not None and clock.now >= stop_at),
                          quote_source=quote_source)
    return summary, settings, broker, logger, clock


def logs(logger, level="info"):
    return [str(c.args[0]) for c in getattr(logger, level).call_args_list]


# ── 1. 휴장일 ────────────────────────────────────────────────
s, st, br, lg, _ = day(datetime(2026, 10, 5, 8, 50))
check("1-1) 휴장일(10/5) → CLOSED_DAY, 브로커 조회 없음", s.status == "CLOSED_DAY" and br.place_calls == [] and s.ticks == 0)
check("1-2) 휴장일에는 리포트를 만들지 않음", not Path(st.storage.reports_dir).exists())

# ── 2. 전략 없는 하루 ─────────────────────────────────────────
s, st, br, lg, clk = day(datetime(2026, 9, 28, 8, 50))
check("2-1) 하루 완료, 잔고 조회 실패 없음", s.status == "COMPLETED" and s.balance_failures == 0)
check("2-2) 장중 60초 간격 폴링 (09:00~15:30 ≈ 390회 + 마감 후 1회)", 385 <= s.ticks <= 395)
check("2-3) 주문 없음", br.place_calls == [] and s.orders_sent == 0)
check("2-4) 마감 후 곧바로 종료 (미해결 없음)", clk.now < datetime(2026, 9, 28, 15, 32))
saved = SwingStateStore(st.storage.state_file).load()[0]
check("2-5) 거래일 기록 저장", saved.last_session_date == "2026-09-28")
check("2-6) 요약 로그 남김", any("[SESSION_SUMMARY]" in m for m in logs(lg)))

# ── 3. 매수 → 체결 기록 → 매도 → 청산 ────────────────────────
strat = ScriptStrategy([
    (time(9, 2), OrderIntent("005930", "BUY", 10, 70_000, reason="too early")),
    (time(10, 0), OrderIntent("005930", "BUY", 10, 70_000, reason="entry", context={"note": "t"})),
    (time(11, 0), lambda ctx: OrderIntent("005930", "SELL", ctx.positions["005930"].quantity, 71_000, reason="exit")),
])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 8, 50), strategy=strat)
check("3-1) 09:02 매수는 신규 주문 시간대 밖이라 차단", s.denied_codes.get("OUTSIDE_ORDER_WINDOW") == 1)
check("3-2) 매수·매도 각 1회 전송·접수", br.place_calls == [("005930", "BUY", 10), ("005930", "SELL", 10)]
      and s.orders_accepted == 2)
events = FillLedgerStore(st.storage.fill_ledger_file).load()
check("3-3) 원장에 BUY 10 (잔고 평균단가) + SELL 10 (주문가 추정)",
      [(e.kind, e.quantity, e.price_source) for e in events]
      == [("BUY", 10, "BROKER_AVG"), ("SELL", 10, "ORDER_ESTIMATE")])
led = apply_events(events)
check("3-4) 청산 후 보유 없음, 실현손익 기록", led.positions() == {} and led.realized[0].gross_pnl == 10_000)
check("3-5) 최종 장부 대조 일치", s.final_reconcile is not None and s.final_reconcile.ok)
saved = SwingStateStore(st.storage.state_file).load()[0]
check("3-6) 청산 후 포지션 메타 정리, 주문 의도 없음",
      saved.positions == {} and saved.unresolved_order_intents == {})
check("3-7) 전략 시작·종료 훅 각 1회", strat.started == 1 and strat.ended == 1)
rows = Path(st.storage.trade_log_file).read_text(encoding="utf-8")
check("3-8) 거래 로그에 전략 ID 기록", "script" in rows)
rep = Path(st.storage.reports_dir) / "daily_report_2026-09-28.md"
check("3-9) 하루 끝에 일일 리포트 생성(실현손익·세션 요약 포함)", rep.exists()
      and "| 당일 실현손익 (비용 전) | +10,000 ⚠추정가 포함 |" in rep.read_text(encoding="utf-8")
      and "### 세션 요약" in rep.read_text(encoding="utf-8"))

# ── 4. 매수 보유 중 메타 생성 ────────────────────────────────
strat = ScriptStrategy([(time(10, 0), OrderIntent("000660", "BUY", 5, 180_000, reason="hold"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 8, 50), strategy=strat)
saved = SwingStateStore(st.storage.state_file).load()[0]
check("4-1) 보유 종목 메타 생성(전략 ID 기록)", saved.positions["000660"].strategy_id == "script")
check("4-2) 장 마감 후에도 보유 유지, 대조 일치", s.final_reconcile.ok
      and apply_events(FillLedgerStore(st.storage.fill_ledger_file).load()).position("000660").quantity == 5)

# 다음 날 이어서 기동 — 같은 파일로
tmp_state = st
clock2 = FakeClock(datetime(2026, 9, 29, 8, 55))
br.clock = clock2
s2 = run_session(tmp_state, br, Mock(), strategy=ScriptStrategy([]), clock=clock2, sleep=clock2.sleep)
check("4-3) 다음 거래일 재기동: 원장·잔고·메타 그대로 일치", s2.status == "COMPLETED" and s2.final_reconcile.ok)

# ── 5. 미체결로 끝나는 주문 ───────────────────────────────────
br = SimBroker()
br.fill_ratio = 0.0
strat = ScriptStrategy([(time(14, 0), OrderIntent("005930", "BUY", 10, 70_000, reason="no fill"))])
s, st, br, lg, clk = day(datetime(2026, 9, 28, 13, 55), strategy=strat, broker=br)
check("5-1) 미체결 주문이 남으면 마감 후 대조 한계(15:45)까지 기다림", clk.now >= datetime(2026, 9, 28, 15, 45))
check("5-2) 미해결 상태로 종료 보고", s.unresolved_at_end)
check("5-3) 원장에 체결 기록 없음", FillLedgerStore(st.storage.fill_ledger_file).load() == [])
check("5-4) 마감 후 추적 중 주문 CRITICAL", any("마감 후에도 추적 중" in m for m in logs(lg, "critical")))

# ── 6. 잔고 조회 실패 ─────────────────────────────────────────
br = SimBroker()
br.fail_balance_until = datetime(2026, 9, 28, 10, 10)
strat = ScriptStrategy([(time(10, 0), OrderIntent("005930", "BUY", 10, 70_000, reason="during outage"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=strat, broker=br)
check("6-1) 잔고 실패 폴링은 주문 없음 → 복구 후 첫 폴링에서 주문", s.balance_failures >= 10
      and br.place_calls == [("005930", "BUY", 10)])
check("6-2) 연속 실패 5회부터 CRITICAL", any("잔고 조회 실패 5회" in m for m in logs(lg, "critical")))

# ── 7. 전략 예외 ─────────────────────────────────────────────
strat = ScriptStrategy([])
strat.raise_at = time(10, 0)
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=strat)
check("7-1) 전략 예외는 CRITICAL 후 계속 진행", s.status == "COMPLETED"
      and any("[STRATEGY_ERROR]" in m for m in logs(lg, "critical")))

# ── 8. 중지 요청 ─────────────────────────────────────────────
s, st, br, lg, clk = day(datetime(2026, 9, 28, 9, 58), stop_at=datetime(2026, 9, 28, 11, 0))
check("8-1) 중지 요청 → STOPPED, 1초 안에 멈춤", s.status == "STOPPED" and clk.now <= datetime(2026, 9, 28, 11, 0, 1))

# ── 9. HTS 수동 매수 종목은 자동 매매 차단 ─────────────────────
br = SimBroker()
br.positions["000660"] = 3          # 원장에 없는 보유
br.avg["000660"] = 180_000
strat = ScriptStrategy([(time(10, 0), OrderIntent("000660", "BUY", 1, 180_000, reason="x"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=strat, broker=br)
check("9-1) 원장에 없는 보유 → 매수 SYMBOL_BLOCKED", s.denied_codes.get("SYMBOL_BLOCKED") == 1 and br.place_calls == [])
check("9-2) 최종 대조 불일치 보고", s.final_reconcile is not None and not s.final_reconcile.ok)

# ── 10. 원장 손상 → 신규 주문 중단 ─────────────────────────────
def corrupt_at_1030(ctx):
    Path(ledger_path[0]).write_text("{broken}\n", encoding="utf-8")
    return OrderIntent("005930", "BUY", 1, 70_000, reason="after corrupt")


ledger_path = [None]


def capture(settings):
    ledger_path[0] = settings.storage.fill_ledger_file
    return settings


strat = ScriptStrategy([(time(10, 30), corrupt_at_1030),
                        (time(11, 0), OrderIntent("005930", "BUY", 1, 70_000, reason="later"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 10, 28), strategy=strat, settings_fn=capture)
check("10-1) 원장 손상 감지 → 신규 주문 중단 (프로세스 중단 없이)", "체결 원장" in s.halted_reason
      and s.status == "COMPLETED")
check("10-2) 중단 후 전략 주문은 전송되지 않음", ("005930", "BUY", 1) not in br.place_calls[1:]
      and len(br.place_calls) <= 1)

# ── 11. 8-B F1: 다른 종목 미해결 주문 중에도 불일치 종목 매도 차단 ──
class DriftBroker(SimBroker):
    """10:10부터 005930 잔고가 10주로 줄어듦 (HTS 수동 매도 흉내)."""

    def get_account_balance(self):
        if self.clock and self.clock() >= datetime(2026, 9, 28, 10, 10):
            self.positions["005930"] = 10
        return super().get_account_balance()


def seed_005930_100(settings):
    FillLedgerStore(settings.storage.fill_ledger_file).append(opening_events_from_balance(
        AccountBalance(0, 0, [Position("005930", 100, 70_000)]), ["005930"],
        datetime(2026, 9, 25).date(), note="test")[0])
    return settings


br = DriftBroker()
br.positions["005930"] = 100
br.avg["005930"] = 70_000
br.fill_ratio = 0.0                  # 000660 매수는 체결되지 않고 미해결로 남음
strat = ScriptStrategy([(time(10, 0), OrderIntent("000660", "BUY", 1, 180_000, reason="pending B")),
                        (time(10, 20), OrderIntent("005930", "SELL", 100, 70_000, reason="sell A"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=strat, broker=br, settings_fn=seed_005930_100,
                       quote_source=StaticQuoteSource({"005930": 70_000}))
check("11-1) 준비: 000660 매수 전송(미체결)", br.place_calls[:1] == [("000660", "BUY", 1)])
check("11-2) [F1 재현] 원장 100·잔고 10인 005930 100주 매도는 전송 0회",
      ("005930", "SELL", 100) not in br.place_calls and s.denied_codes.get("SYMBOL_BLOCKED") == 1)
check("11-3) 005930 불일치는 BLOCK으로 기록",
      any("[BLOCK] QTY_MISMATCH 005930" in m for m in logs(lg, "critical")))

# ── 12. 8-B F3: 보유 종목 현재가로 노출 평가 ─────────────────────
def seed_005930_30(settings):
    FillLedgerStore(settings.storage.fill_ledger_file).append(opening_events_from_balance(
        AccountBalance(0, 0, [Position("005930", 30, 50_000)]), ["005930"],
        datetime(2026, 9, 25).date(), note="test")[0])
    return settings


def held_broker():
    b = SimBroker()
    b.positions["005930"] = 30
    b.avg["005930"] = 50_000
    return b


buy_b = lambda: ScriptStrategy([(time(10, 0), OrderIntent("000660", "BUY", 1, 180_000, reason="B"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=buy_b(), broker=held_broker(),
                       settings_fn=seed_005930_30)
check("12-1) 현재가 조회 수단 없음 + 보유 있음 → PRICE_UNKNOWN, 전송 0회",
      s.denied_codes.get("PRICE_UNKNOWN") == 1 and br.place_calls == [])
qs = StaticQuoteSource({"005930": 70_000})
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=buy_b(), broker=held_broker(),
                       settings_fn=seed_005930_30, quote_source=qs)
check("12-2) 현재가 있으면 허용, 보유 종목만 한 번 조회", br.place_calls == [("000660", "BUY", 1)]
      and qs.requests == [("005930",)])


def low_limit(settings):
    import dataclasses
    seed_005930_30(settings)
    return dataclasses.replace(settings, guard=dataclasses.replace(settings.guard, max_total_exposure=2_000_000,
                                                                   max_order_amount=1_000_000))


s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=buy_b(), broker=held_broker(),
                       settings_fn=low_limit, quote_source=StaticQuoteSource({"005930": 70_000}))
check("12-3) 원가 150만이지만 현재가 평가 210만 → 한도 200만 초과로 차단",
      s.denied_codes.get("TOTAL_EXPOSURE_LIMIT") == 1 and br.place_calls == [])

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
