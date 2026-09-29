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
from domain.models import AccountBalance, BrokerOrderStatus, OrderResult, Position
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

REPO_COMMANDS = Path(__file__).resolve().parent / "commands"


def _snapshot_repo_commands():
    if not REPO_COMMANDS.exists():
        return None
    return sorted((str(p.relative_to(REPO_COMMANDS)), p.read_bytes() if p.is_file() else b"")
                  for p in REPO_COMMANDS.rglob("*"))


_REPO_COMMANDS_BEFORE = _snapshot_repo_commands()


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
        self.fail_balance_windows: list[tuple[datetime, datetime]] = []
        self.clock = None

    def place_order(self, order):
        res = super().place_order(order)
        if res.accepted:
            self.pending.append((order.symbol, order.side.value, order.quantity, res.order_id))
        return res

    def get_account_balance(self):
        if self.fail_balance_until and self.clock and self.clock() < self.fail_balance_until:
            raise RuntimeError("kiwoom request failed: http=500")
        if self.fail_balance_windows and self.clock and any(a <= self.clock() < b for a, b in self.fail_balance_windows):
            raise RuntimeError("kiwoom request failed: http=500")
        still = []
        for sym, side, qty, oid in self.pending:
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
                still.append((sym, side, qty - q, oid)) if self.fill_ratio == 1.0 else None
            else:
                self.status[oid] = BrokerOrderStatus.FILLED   # 8-E: 주문 조회 FILLED 증거
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

# ── 13. 8-C: 분할청산 하루 + 체결 식별자 ─────────────────────────
strat = ScriptStrategy([(time(10, 0), OrderIntent("005930", "BUY", 20, 70_000, reason="entry")),
                        (time(11, 0), OrderIntent("005930", "SELL", 6, 71_000, reason="partial exit"))])
s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), strategy=strat,
                       quote_source=StaticQuoteSource({"005930": 70_000}))
events = FillLedgerStore(st.storage.fill_ledger_file).load()
led = apply_events(events)
check("13-1) 20주 매수 → 6주 분할청산: 원장 보유 14주, 실현 6주, 주문 2건 전송",
      led.position("005930") is not None and led.position("005930").quantity == 14
      and sum(r.quantity for r in led.realized) == 6 and len(br.place_calls) == 2)
check("13-2) 마감 시 미해결 주문 없음, 장부 대조 일치", not s.unresolved_at_end and s.final_reconcile.ok)
saved = SwingStateStore(st.storage.state_file).load()[0]
check("13-3) 보유 메타 유지(분할청산은 청산 아님)", "005930" in saved.positions)
check("13-4) 체결 event_id에 계좌범위·거래일·방향·종목 포함",
      all(e.event_id.count("|") == 5 and "|20260928|" in e.event_id and "|005930|" in e.event_id for e in events))

# ── 14. 8-D (F5): 마감 검증 ─────────────────────────────────────
import app.main as app_main  # noqa: E402

s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 10))
status = json.loads((Path(st.storage.reports_dir) / "session_status_2026-09-28.json").read_text(encoding="utf-8"))
check("14-1) 정상 하루 → COMPLETED + VERIFIED, 마감 후 잔고 시각 기록, 상태 파일",
      s.status == "COMPLETED" and s.close_check == "VERIFIED" and s.final_balance_at >= datetime(2026, 9, 28, 15, 30)
      and status["close_check"] == "VERIFIED" and status["report"] == "OK" and status["final_reconcile_ok"] is True)

br = SimBroker()
br.fail_balance_windows = [(datetime(2026, 9, 28, 15, 30), datetime(2026, 9, 28, 17, 0))]
s, st, br, lg, clk = day(datetime(2026, 9, 28, 15, 10), broker=br)
check("14-2) [F5 재현] 장중 대조는 성공, 마감 후 잔고 실패 → COMPLETED지만 NEEDS_REVIEW",
      s.status == "COMPLETED" and s.close_check == "NEEDS_REVIEW"
      and "FINAL_BALANCE_FAILED" in s.close_issues and "FINAL_RECONCILE_MISSING" in s.close_issues)
check("14-3) 장중의 이전 대조를 최종 결과로 쓰지 않음", s.final_reconcile is None and s.final_reconcile_at is None)
check("14-4) 제한 횟수(3회) 재시도 후 종료", sum("[SESSION_CLOSE] 마감 후 잔고·대조 미확보" in m
                                              for m in logs(lg, "warning")) == 3)
check("14-5) NEEDS_REVIEW는 CRITICAL, 요약·상태 파일에 반영",
      any("마감 검증 NEEDS_REVIEW" in m for m in logs(lg, "critical"))
      and any(l.startswith("마감 검증: NEEDS_REVIEW") for l in s.lines())
      and json.loads((Path(st.storage.reports_dir) / "session_status_2026-09-28.json")
                     .read_text(encoding="utf-8"))["close_check"] == "NEEDS_REVIEW")

br = SimBroker()
br.fail_balance_windows = [(datetime(2026, 9, 28, 15, 30), datetime(2026, 9, 28, 15, 30, 20))]
s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 29), broker=br)
check("14-6) 마감 직후 일시 실패 → 재시도에서 회복하면 VERIFIED", s.close_check == "VERIFIED"
      and s.final_balance_at is not None)

s, st, br, lg, _ = day(datetime(2026, 9, 28, 9, 58), stop_at=datetime(2026, 9, 28, 11, 0))
check("14-7) 마감 전 중지 → STOPPED + NOT_RUN(검증 성공으로 보이지 않음)",
      s.status == "STOPPED" and s.close_check == "NOT_RUN")

br = SimBroker()
br.positions["000660"] = 3
br.avg["000660"] = 180_000
s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 10), broker=br)
check("14-8) 마감 대조 불일치 → NEEDS_REVIEW(RECONCILE_MISMATCH)", s.close_check == "NEEDS_REVIEW"
      and s.close_issues == ["RECONCILE_MISMATCH"])

orig_kb, orig_mk = app_main.KiwoomBroker, app_main._make_daily_bar_updater
try:
    app_main.KiwoomBroker = SimBroker
    app_main._make_daily_bar_updater = lambda *a, **k: (lambda d: "1/1종목 실패 ['005930']")

    def bars_on(settings):
        import dataclasses
        return dataclasses.replace(settings, session=dataclasses.replace(settings.session,
                                                                         update_daily_bars_after_close=True))
    s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 10), settings_fn=bars_on)
    check("14-9) 일봉 갱신 실패 → NEEDS_REVIEW(DAILY_BARS_FAILED), 결과 문구 기록",
          s.close_check == "NEEDS_REVIEW" and s.close_issues == ["DAILY_BARS_FAILED"] and "005930" in s.daily_bars)
    app_main._make_daily_bar_updater = lambda *a, **k: (lambda d: "")
    s, *_ = day(datetime(2026, 9, 28, 15, 10), settings_fn=bars_on)
    check("14-10) 일봉 갱신 성공 → OK, VERIFIED", s.daily_bars == "OK" and s.close_check == "VERIFIED")
finally:
    app_main.KiwoomBroker, app_main._make_daily_bar_updater = orig_kb, orig_mk

import app.reports as app_reports  # noqa: E402
orig_gen = app_reports.generate_daily_report
try:
    def boom(*a, **k):
        raise OSError("disk full")
    app_reports.generate_daily_report = boom
    s, st, *_ = day(datetime(2026, 9, 28, 15, 10))
    check("14-11) 리포트 생성 실패 → NEEDS_REVIEW(REPORT_FAILED), 상태 파일은 남음",
          s.report.startswith("FAILED") and s.close_issues == ["REPORT_FAILED"]
          and json.loads((Path(st.storage.reports_dir) / "session_status_2026-09-28.json")
                         .read_text(encoding="utf-8"))["close_check"] == "NEEDS_REVIEW")
finally:
    app_reports.generate_daily_report = orig_gen

s, *_ = day(datetime(2026, 9, 27, 9, 0))
check("14-12) 휴장일은 마감 검증 대상 아님", s.status == "CLOSED_DAY" and s.close_check == "")

# ── 15. 8-E R2·R3: 마감 보고서·상태 저장 ──────────────────────────
br = SimBroker()
br.fail_balance_windows = [(datetime(2026, 9, 28, 15, 30), datetime(2026, 9, 28, 17, 0))]
s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 29), broker=br)
rep = (Path(st.storage.reports_dir) / "daily_report_2026-09-28.md").read_text(encoding="utf-8")
check("15-1) [R2 재현] 15:29 성공·마감 후 실패 → 보고서에 '일치' 없이 최종 대조 미확보·마지막 잔고 시각 표시",
      s.close_check == "NEEDS_REVIEW" and "원장·잔고·메타 일치" not in rep
      and "마감 최종 대조 미확보" in rep and "15:29:00" in rep and "마감 검증에 사용 불가" in rep)
check("15-2) 보고서 운영 상태에 마감 검증 결과 표시", "⚠ 마감 검증: NEEDS_REVIEW — FINAL_BALANCE_FAILED" in rep)
s, st, *_ = day(datetime(2026, 9, 28, 15, 10))
rep = (Path(st.storage.reports_dir) / "daily_report_2026-09-28.md").read_text(encoding="utf-8")
status = json.loads((Path(st.storage.reports_dir) / "session_status_2026-09-28.json").read_text(encoding="utf-8"))
check("15-3) 정상 마감: 보고서 '마감 검증: VERIFIED'·일치, 상태 파일에 run_id·생성 시각",
      "- 마감 검증: VERIFIED" in rep and "원장·잔고·메타 일치" in rep
      and status["run_id"] == s.run_id and len(s.run_id) == 12 and status["generated_at"])

import os as _os  # noqa: E402
real_replace = _os.replace


def failing_replace(src, dst, *a, **k):
    if "session_status_" in str(dst):
        raise PermissionError("locked")
    return real_replace(src, dst, *a, **k)


def seed_old_status(settings):
    d = Path(settings.storage.reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_status_2026-09-28.json").write_text('{"close_check": "VERIFIED", "run_id": "old"}', encoding="utf-8")
    return settings


_os.replace = failing_replace
try:
    s, st, br, lg, _ = day(datetime(2026, 9, 28, 15, 10), settings_fn=seed_old_status)
finally:
    _os.replace = real_replace
d = Path(st.storage.reports_dir)
check("15-4) [R3 재현] 상태 파일 교체 실패 → STATUS_WRITE_FAILED, VERIFIED로 끝나지 않음",
      "STATUS_WRITE_FAILED" in s.close_issues and s.close_check == "NEEDS_REVIEW" and s.needs_attention)
check("15-5) 이전 실행 상태 파일은 치워 이번 결과로 오인되지 않음, 임시 파일 없음",
      not (d / "session_status_2026-09-28.json").exists() and list(d.glob("session_status_*.tmp")) == [])
check("15-6) 실패는 CRITICAL로 run_id와 함께 기록", any("상태 파일 저장 실패" in m and s.run_id in m
                                                        for m in logs(lg, "critical")))

orig_gen = app_reports.generate_daily_report
try:
    app_reports.generate_daily_report = boom
    s, *_ = day(datetime(2026, 9, 28, 9, 58), stop_at=datetime(2026, 9, 28, 11, 0))
finally:
    app_reports.generate_daily_report = orig_gen
check("15-7) 마감 전 중지(NOT_RUN)에서도 리포트 실패는 사유에 누적 → 종료 코드 2 대상",
      s.close_check == "NOT_RUN" and "REPORT_FAILED" in s.close_issues and s.needs_attention)


# ── 16. 8-G: 테스트가 운영용 commands/를 건드리지 않음 ─────────────
s, st, *_ = day(datetime(2026, 9, 28, 15, 10))
check("16-1) 세션은 설정의 commands_dir(임시 폴더)를 사용, 기동 시 빈 복구 목록 기록",
      json.loads((Path(st.storage.commands_dir) / "recovery_required.json").read_text(encoding="utf-8"))["items"] == [])
check("16-2) 테스트 전후 저장소의 commands/ 내용 동일", _snapshot_repo_commands() == _REPO_COMMANDS_BEFORE)


# 종료 코드 (마지막에 — main()이 logging.shutdown을 부름)
orig_am = app_main.async_main
try:
    from types import SimpleNamespace

    async def fake_review(check_only=False):
        return SimpleNamespace(close_check="NEEDS_REVIEW", close_issues=["FINAL_BALANCE_FAILED"])

    async def fake_ok(check_only=False):
        return SimpleNamespace(close_check="VERIFIED", close_issues=[])

    async def fake_notrun_issue(check_only=False):
        return SimpleNamespace(close_check="NOT_RUN", close_issues=["REPORT_FAILED"])

    async def fake_crash(check_only=False):
        raise RuntimeError("boom")
    codes = []
    for fn in (fake_ok, fake_review, fake_crash, fake_notrun_issue):
        app_main.async_main = fn
        codes.append(app_main.main())
    check("14-13) 종료 코드: VERIFIED 0 / NEEDS_REVIEW 2 / 예외 1 / 중지+문제 기록 2", codes == [0, 2, 1, 2])
finally:
    app_main.async_main = orig_am

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
