# -*- coding: utf-8 -*-
"""9단계: 전략 없는(스크립트 주문만) 다일 장애 통합 검증.

같은 저장 경로·같은 가짜 브로커로 4거래일을 이어서 돌립니다(가짜 시계, 실제 주문 없음).

  D1 9/28(월) 매수 20주 → 마감 VERIFIED
  D2 9/29(화) 보유만. 상태 파일 저장 실패 → STATUS_WRITE_FAILED(종료코드 2), 보고서는 정상
  D3 9/30(수) 6주 분할매도(주문 조회 FILLED로 종료) → 4주 매도 접수 직후 장중 중지(미체결)
             → 중지 중 4주 체결 → 재시작: ERROR 복원, 전략의 재매도·매수 차단
             → 복구 명령(현재 recovery_id) 확보 1회 실패 → 30초 뒤 재확보·적용
             → 잔고 API 장애 구간(주문 없음) → 마감: 원장 14 vs 잔고 10 → NEEDS_REVIEW
  (마감 후 사람이 재시작 구간 체결 4주를 원장에 정정 기록)
  D4 10/1(목) 정상 종목(000660) 매수 의도가 잔고 API 장애 구간에 걸림 → 장애 중 전략 호출·주문 0,
             복구 후 첫 폴링에서 1회 실행 → 마감 VERIFIED, 원장 = 잔고
  종료코드는 운영 main()으로 확인 (0, 2, 2, 0)

완료 기준 (GPT 9단계 제안)
  - 중복 주문 0건, 중복 체결 기록 0건
  - 원장·잔고 정합 (VERIFIED인 날)
  - 불확실한 주문은 사람 확인 전까지 차단 유지
  - 보고서·상태 파일·종료코드 일관
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, ".")

from app.main import run_session
from domain.models import AccountBalance, BrokerOrderStatus, Position
from domain.position.fill_event import FillEvent
from domain.service.lot_ledger import apply_events
from domain.strategy.interface import OrderIntent
from infra.market_data.quote_source import StaticQuoteSource
from infra.storage.fill_ledger import FillLedgerStore
from infra.storage.tracked_order_journal import TrackedOrderJournalStore
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


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.now += timedelta(seconds=s)


class DayBroker(ScriptedBroker):
    """접수된 주문은 다음 잔고 조회에서 체결(hold에 든 주문번호는 보류). 체결되면 주문 조회 FILLED."""

    def __init__(self):
        super().__init__()
        self.prices = {"005930": 70_000, "000660": 180_000}
        self.place_times: list[datetime] = []
        self.avg: dict[str, int] = {}
        self.pending: list[tuple] = []
        self.hold: set[str] = set()
        self.fail_windows: list[tuple[datetime, datetime]] = []
        self.clock = None
        self.balance_calls = 0

    def place_order(self, order):
        if self.clock:
            self.place_times.append(self.clock())
        res = super().place_order(order)
        if res.accepted:
            self.pending.append((order.symbol, order.side.value, order.quantity, res.order_id))
            self.status[res.order_id] = BrokerOrderStatus.OPEN
        return res

    def fill(self, oid):
        for item in list(self.pending):
            sym, side, qty, o = item
            if o != oid:
                continue
            cur = self.positions.get(sym, 0)
            px = self.prices[sym]
            if side == "BUY":
                self.avg[sym] = (cur * self.avg.get(sym, px) + qty * px) // (cur + qty)
                self.positions[sym] = cur + qty
            else:
                self.positions[sym] = cur - qty
            self.status[oid] = BrokerOrderStatus.FILLED
            self.pending.remove(item)

    def get_account_balance(self):
        if self.clock and any(a <= self.clock() < b for a, b in self.fail_windows):
            raise RuntimeError("kiwoom request failed: http=500")
        self.balance_calls += 1
        for _, _, _, oid in list(self.pending):
            if oid not in self.hold:
                self.fill(oid)
        return AccountBalance(100_000_000, 100_000_000, [
            Position(s, q, self.avg.get(s, self.prices.get(s, 10_000))) for s, q in self.positions.items() if q > 0])


class Script:
    """시각별 동작: OrderIntent를 내거나, 부수 동작(callable)을 실행. 전략 호출 기록."""
    strategy_id = "it9"

    def __init__(self, items):
        self.items = list(items)
        self.seen_blocked: list[frozenset] = []
        self.tick_times: list[datetime] = []

    def on_session_start(self, ctx):
        pass

    def on_session_end(self, ctx):
        pass

    def on_tick(self, ctx):
        self.seen_blocked.append(ctx.blocked_symbols)
        self.tick_times.append(ctx.now)
        out = []
        for item in list(self.items):
            t, what = item
            if ctx.now.time() >= t:
                self.items.remove(item)
                r = what(ctx) if callable(what) else what
                if isinstance(r, OrderIntent):
                    out.append(r)
        return out


TMP = tempfile.mkdtemp()
SETTINGS = build_minimal_settings(TMP)
BROKER = DayBroker()
QUOTES = StaticQuoteSource({"005930": 70_000, "000660": 180_000})
REPORTS = Path(SETTINGS.storage.reports_dir)
CMDS = Path(SETTINGS.storage.commands_dir)


def run(day: date, start: time, script, *, stop_at: datetime | None = None, extra=None):
    clock = Clock(datetime.combine(day, start))
    BROKER.clock = clock
    logger = Mock()
    ctx = extra or _nullctx()
    with ctx:
        s = run_session(SETTINGS, BROKER, logger, strategy=script, clock=clock, sleep=clock.sleep,
                        should_stop=(lambda: stop_at is not None and clock.now >= stop_at), quote_source=QUOTES)
    return s, logger, clock


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def logs(logger, level):
    return [str(c.args[0]) for c in getattr(logger, level).call_args_list]


def status_of(d: date) -> dict | None:
    p = REPORTS / f"session_status_{d.isoformat()}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def report_of(d: date) -> str:
    return (REPORTS / f"daily_report_{d.isoformat()}.md").read_text(encoding="utf-8")


def ledger():
    return apply_events(FillLedgerStore(SETTINGS.storage.fill_ledger_file).load())


def exit_code(summary) -> int:
    return 2 if summary.needs_attention else 0


def consistent(summary, d: date, *, status_expected=True) -> bool:
    """보고서의 마감 검증 줄 = 세션 판정 = 상태 파일(있다면), 종료코드 규칙과 일치."""
    rep = report_of(d)
    line_ok = f"마감 검증: {summary.close_check}" in rep
    st = status_of(d)
    if status_expected:
        file_ok = st is not None and st["close_check"] == summary.close_check and st["run_id"] == summary.run_id
    else:
        file_ok = st is None
    return line_ok and file_ok


D1, D2, D3, D4 = date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)

# ── D1: 매수 → 보유 ────────────────────────────────────────────
s1, lg1, _ = run(D1, time(8, 50), Script([(time(10, 0), OrderIntent("005930", "BUY", 20, 70_000, reason="entry"))]))
check("D1-1) 매수 1회 전송·체결, 원장 20주 = 잔고 20주",
      BROKER.place_calls == [("005930", "BUY", 20)] and ledger().position("005930").quantity == 20
      and BROKER.positions["005930"] == 20)
check("D1-2) 마감 VERIFIED, 종료코드 0, 보고서·상태 파일 일치",
      s1.close_check == "VERIFIED" and exit_code(s1) == 0 and consistent(s1, D1))

# ── D2: 보유만, 상태 파일 저장 실패 ─────────────────────────────
real_replace = os.replace


def failing_replace(src, dst, *a, **k):
    if "session_status_" in str(dst):
        raise PermissionError("locked")
    return real_replace(src, dst, *a, **k)


class _patch_replace:
    def __enter__(self):
        os.replace = failing_replace

    def __exit__(self, *a):
        os.replace = real_replace
        return False


s2, lg2, _ = run(D2, time(8, 50), Script([]), extra=_patch_replace())
rep2 = report_of(D2)
check("D2-1) 주문 0건, 원장 20 = 잔고 20, 보고서 보유 거래일 1",
      len(BROKER.place_calls) == 1 and "| 005930 | 20 | 70,000 | 2026-09-28 | 1 |" in rep2)
check("D2-2) 상태 파일 저장 실패 → STATUS_WRITE_FAILED·종료코드 2, 상태 파일 없음(이전 결과로 오인 불가)",
      s2.close_issues == ["STATUS_WRITE_FAILED"] and exit_code(s2) == 2 and status_of(D2) is None)
check("D2-3) [9-A] 보고서도 최종 판정 NEEDS_REVIEW·STATUS_WRITE_FAILED 표시 (VERIFIED로 남지 않음)",
      consistent(s2, D2, status_expected=False) and "⚠ 마감 검증: NEEDS_REVIEW — STATUS_WRITE_FAILED" in rep2
      and "마감 검증: VERIFIED" not in report_of(D2))

# ── D3: 분할매도 → 장중 중지 → 재시작 → 복구 → API 장애 → 마감 ─────
calls_before_d3 = len(BROKER.place_calls)


def hold_next_order(ctx):
    BROKER.hold.add(f"{BROKER.seq + 1:07d}")      # 다음 주문은 체결 보류(재시작 전 미체결)
    return OrderIntent("005930", "SELL", 4, 70_000, reason="second partial")


s3a, lg3a, c3a = run(D3, time(8, 50), Script([
    (time(10, 0), OrderIntent("005930", "SELL", 6, 71_000, reason="partial exit")),
    (time(10, 20), hold_next_order),
]), stop_at=datetime(2026, 9, 30, 10, 30))
first_sell = BROKER.place_calls[calls_before_d3]
held_oid = f"{BROKER.seq:07d}"
check("D3-1) 6주 분할매도: 주문 조회 FILLED 후 종료, 원장 14 = 잔고 14",
      first_sell == ("005930", "SELL", 6) and ledger().position("005930").quantity == 14
      and any(r.quantity == 6 for r in ledger().realized))
check("D3-2) 4주 매도 접수·미체결 상태에서 장중 중지 → STOPPED/NOT_RUN, 저널에 남음",
      s3a.status == "STOPPED" and s3a.close_check == "NOT_RUN" and BROKER.place_calls[-1] == ("005930", "SELL", 4)
      and "005930" in TrackedOrderJournalStore(SETTINGS.storage.tracked_order_journal_file).load_all())

# 중지 중 체결
BROKER.hold.clear()
BROKER.fill(held_oid)
check("D3-3) (준비) 중지 중 4주 체결 → 잔고 10", BROKER.positions["005930"] == 10)

calls_before_restart = len(BROKER.place_calls)
claim_fail = {"on": False}
real_path_replace = Path.replace


def path_replace(self, target):
    if claim_fail["on"] and "processing" in str(target):
        raise PermissionError("locked")
    return real_path_replace(self, target)


def write_ack(ctx):
    rec = json.loads((CMDS / "recovery_required.json").read_text(encoding="utf-8"))
    item = next(i for i in rec["items"] if i["symbol"] == "005930" and i["kind"] == "ERROR")
    (CMDS / "ack_error_005930.json").write_text(json.dumps(
        {"recovery_id": item["recovery_id"], "broker_quantity": 10, "note": "HTS 체결 4주 확인"}), encoding="utf-8")
    claim_fail["on"] = True                      # 첫 확보 시도는 잠금으로 실패
    return None


def unlock(ctx):
    claim_fail["on"] = False
    return None


script3 = Script([
    (time(10, 32), OrderIntent("005930", "SELL", 4, 70_000, reason="retry after restart")),   # ERROR → 차단
    (time(10, 33), OrderIntent("005930", "BUY", 1, 70_000, reason="buy during recovery")),     # 차단
    (time(10, 40), write_ack),
    (time(10, 41), unlock),
    (time(11, 2), OrderIntent("005930", "BUY", 1, 70_000, reason="after outage")),           # 불일치 → 차단
])
BROKER.fail_windows = [(datetime(2026, 9, 30, 11, 0), datetime(2026, 9, 30, 11, 5))]
with patch.object(Path, "replace", path_replace):
    s3, lg3, c3 = run(D3, time(10, 31), script3)
BROKER.fail_windows = []
crit3 = " ".join(logs(lg3, "critical"))
check("D3-4) 재시작: 미해결 주문 ERROR 복원 + 새 recovery_id 발급",
      "[STARTUP_ORDER_RECOVERY] 005930" in crit3 and "[RECOVERY_REQUIRED] 005930 | ERROR" in crit3)
check("D3-5) 재시작 후 사람 확인 전 재매도·매수 전송 0회 (중복 주문 없음)",
      len(BROKER.place_calls) == calls_before_restart)
check("D3-5b) 차단 경로: 재매도는 주문 실행부 ERROR 게이트, 매수는 금액 모르는 미해결 주문으로 보류",
      any("SELL 미접수 | BLOCK_SELL_ERROR_STATE" in m for m in logs(lg3, "warning"))
      and s3.denied_codes.get("PENDING_AMOUNT_UNKNOWN") == 1)
check("D3-6) 복구 명령 확보 1회 실패(CRITICAL) → 30초 뒤 재확보 성공 → 1회 적용",
      crit3.count("명령 확보 실패") == 1 and any("명령 확보 재시도 성공" in m for m in logs(lg3, "warning"))
      and len(list((CMDS / "processed").glob("ack_error_005930.*.json"))) == 1)
check("D3-7) API 장애 구간: 잔고 실패 기록, 그 사이 주문 없음", s3.balance_failures >= 5)
check("D3-8) 재시작 구간 체결은 자동 기록되지 않음 → 원장 14 vs 잔고 10 → 불일치 종목 매수도 차단",
      ledger().position("005930").quantity == 14 and s3.denied_codes.get("SYMBOL_BLOCKED", 0) >= 1)
check("D3-9) 마감 NEEDS_REVIEW(RECONCILE_MISMATCH), 종료코드 2, 보고서 불일치 표시·상태 파일 일치",
      s3.close_check == "NEEDS_REVIEW" and "RECONCILE_MISMATCH" in s3.close_issues and exit_code(s3) == 2
      and "QTY_MISMATCH 005930" in report_of(D3) and consistent(s3, D3))

# 마감 후 사람이 정정 기록 (HTS에서 확인한 재시작 구간 체결)
FillLedgerStore(SETTINGS.storage.fill_ledger_file).append(FillEvent(
    event_id=f"MANUAL|{D3:%Y%m%d}|SELL|005930|{held_oid}|4", kind="SELL", symbol="005930", quantity=4,
    price=70_000, price_source="ORDER_ESTIMATE", trade_date=D3, occurred_at=datetime(2026, 9, 30, 16, 0),
    order_id=held_oid, note="재시작 구간 체결 — HTS 확인 후 수동 정정"))

# ── D4: 정상 종목 매수가 잔고 API 장애 구간에 걸림 → VERIFIED ─────────
BROKER.fail_windows = [(datetime(2026, 10, 1, 10, 0), datetime(2026, 10, 1, 10, 5))]
calls_before_d4 = len(BROKER.place_calls)
script4 = Script([(time(10, 2), OrderIntent("000660", "BUY", 1, 180_000, reason="during outage"))])
s4, lg4, _ = run(D4, time(9, 58), script4)
BROKER.fail_windows = []
outage = (datetime(2026, 10, 1, 10, 0), datetime(2026, 10, 1, 10, 5))
buy_time = BROKER.place_times[calls_before_d4]
check("D4-1) [9-B] 장애 구간에는 전략 호출 0회(오래된 잔고로 판단하지 않음)",
      not any(outage[0] <= t < outage[1] for t in script4.tick_times) and s4.balance_failures == 5)
check("D4-2) [9-B] 정상 종목 000660 매수는 장애 중 전송 0회, 복구 후 첫 폴링(10:05)에 정확히 1회",
      BROKER.place_calls[calls_before_d4:] == [("000660", "BUY", 1)] and buy_time == datetime(2026, 10, 1, 10, 5)
      and min(t for t in script4.tick_times if t >= outage[1]) == buy_time)
check("D4-3) 정정 후 원장 = 잔고(005930 10, 000660 1), 마감 VERIFIED·종료코드 0, 보고서·상태 파일 일치",
      ledger().position("005930").quantity == 10 and ledger().position("000660").quantity == 1
      and BROKER.positions["000660"] == 1 and s4.close_check == "VERIFIED" and exit_code(s4) == 0
      and consistent(s4, D4))

# ── 전 기간 불변식 ─────────────────────────────────────────────
events = FillLedgerStore(SETTINGS.storage.fill_ledger_file).load()
ids = [e.event_id for e in events]
check("ALL-1) 중복 체결 기록 0건 (event_id 유일), 사건 = 매수 20·매도 6·4(정정)",
      len(ids) == len(set(ids)) and [(e.symbol, e.kind, e.quantity) for e in events]
      == [("005930", "BUY", 20), ("005930", "SELL", 6), ("005930", "SELL", 4), ("000660", "BUY", 1)])
check("ALL-2) 전 기간 주문 = 매수 20, 매도 6, 매도 4, 000660 매수 1 (중복 주문 0건)",
      BROKER.place_calls == [("005930", "BUY", 20), ("005930", "SELL", 6), ("005930", "SELL", 4),
                             ("000660", "BUY", 1)])
check("ALL-3) 실현 10주, 005930 보유 10주 — 원장·잔고 정합", sum(r.quantity for r in ledger().realized) == 10
      and BROKER.positions["005930"] == 10)
rep1_again = __import__("app.reports", fromlist=["x"]).generate_daily_report(
    SETTINGS, D1, today=D4).read_text(encoding="utf-8")
check("ALL-4) 과거(D1) 보고서 재생성해도 D1 보유 20주 그대로", "| 005930 | 20 | 70,000 | 2026-09-28 |" in rep1_again)
check("ALL-5) 미해결 주문·저널 없음, 복구 목록 비어 있음",
      TrackedOrderJournalStore(SETTINGS.storage.tracked_order_journal_file).load_all() == {}
      and json.loads((CMDS / "recovery_required.json").read_text(encoding="utf-8"))["items"] == [])

# 종료코드: 운영 main()에 각 날의 세션 결과를 넣어 확인 (마지막에 — main()이 logging.shutdown 호출)
import app.main as app_main  # noqa: E402

orig_async_main = app_main.async_main
codes = []
try:
    for summ in (s1, s2, s3, s4):
        async def _fake(check_only=False, _s=summ):
            return _s
        app_main.async_main = _fake
        codes.append(app_main.main())
finally:
    app_main.async_main = orig_async_main
check("ALL-6) 운영 main() 종료코드 D1~D4 = 0, 2, 2, 0", codes == [0, 2, 2, 0])

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
