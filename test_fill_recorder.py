# -*- coding: utf-8 -*-
"""체결 → 원장 자동 기록 회귀 테스트 (스윙 분리 6라운드, 2026-09-28)."""
from __future__ import annotations

import sys
import tempfile
from datetime import date, datetime
from unittest.mock import Mock

sys.path.insert(0, ".")

from domain.models import AccountBalance, Position
from domain.service.fill_recorder import FillRecorder
from domain.service.lot_ledger import apply_events
from infra.storage.fill_ledger import FillLedgerStore

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1



def raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except BaseException:
        return False
    return False


D = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 10, 0)


def bal(*ps):
    return AccountBalance(10_000_000, 10_000_000, [Position(s, q, a) for s, q, a in ps])


def new():
    store = FillLedgerStore(f"{tempfile.mkdtemp()}/fills.jsonl")
    return FillRecorder(store, logger=Mock()), store


# ── 1. 매수 부분체결 → 전량 ───────────────────────────────────
rec, store = new()
rec.track("005930", "BUY", "0001", 100, 70_000, base_quantity=0, base_cost=0)
check("1-1) 잔고 변화 없으면 기록 없음", rec.observe(bal(), trade_date=D, now=NOW) == [])
ev = rec.observe(bal(("005930", 40, 70_100)), trade_date=D, now=NOW)
check("1-2) 40주 체결 → BUY 40주, 원가 = 잔고 평균단가(BROKER_AVG)",
      len(ev) == 1 and ev[0].quantity == 40 and ev[0].price == 70_100 and ev[0].price_source == "BROKER_AVG")
check("1-3) event_id = 범위|주문거래일|방향|종목|주문번호|누적수량", ev[0].event_id == "default|20260928|BUY|005930|0001|40")
ev = rec.observe(bal(("005930", 100, 70_160)), trade_date=D, now=NOW)
# 나머지 60주 원가 = (70160*100 - 70100*40) / 60 = 70200
check("1-4) 나머지 60주 원가를 평균단가로 역산", ev[0].quantity == 60 and ev[0].price == 70_200)
check("1-5) 전량 체결 후 추적 종료", "005930" not in rec.tracked)
led = apply_events(store.load())
check("1-6) 원장 원가 = 잔고 평균단가 × 수량", led.position("005930").cost_basis == 70_160 * 100)
check("1-7) 같은 잔고로 다시 관찰해도 중복 기록 없음", rec.observe(bal(("005930", 100, 70_160)), trade_date=D, now=NOW) == [])

# ── 2. 기존 보유분 위에 추가 매수 ─────────────────────────────
rec, store = new()
rec.track("005930", "BUY", "0002", 50, 60_000, base_quantity=100, base_cost=100 * 50_000)
ev = rec.observe(bal(("005930", 150, 53_333)), trade_date=D, now=NOW)
check("2-1) 증가분 50주만 기록, 원가 역산 ≈ 59,999", ev[0].quantity == 50 and abs(ev[0].price - 59_999) <= 1)

# ── 3. 매도 ─────────────────────────────────────────────────
rec, store = new()
rec.track("047040", "SELL", "0003", 353, 9_000, base_quantity=353, base_cost=353 * 10_000)
ev = rec.observe(bal(("047040", 343, 10_000)), trade_date=D, now=NOW)
check("3-1) 10주 매도 체결 → SELL 10주, 주문가 추정(ORDER_ESTIMATE)",
      ev[0].kind == "SELL" and ev[0].quantity == 10 and ev[0].price_source == "ORDER_ESTIMATE" and ev[0].price == 9_000)
ev = rec.observe(bal(), trade_date=D, now=NOW)
check("3-2) 잔고 0 → 나머지 343주", ev[0].quantity == 343 and ev[0].event_id.endswith("|SELL|047040|0003|353"))

# ── 4. 방향이 반대거나 요청을 넘는 변화 ────────────────────────
rec, store = new()
rec.track("005930", "BUY", "0004", 10, 70_000, base_quantity=50, base_cost=50 * 70_000)
check("4-1) 매수 추적 중 수량 감소는 기록하지 않음", rec.observe(bal(("005930", 40, 70_000)), trade_date=D, now=NOW) == [])
ev = rec.observe(bal(("005930", 70, 70_000)), trade_date=D, now=NOW)
check("4-2) 요청(10주)을 넘는 증가는 10주까지만, 원가는 추정치로",
      ev[0].quantity == 10 and ev[0].price_source == "ORDER_ESTIMATE")

# ── 5. 추적하지 않은 종목·주문번호 없음 ──────────────────────
rec, store = new()
check("5-1) 추적 주문이 없으면 잔고가 바뀌어도 기록 없음(HTS 수동 매매는 대조에서 드러남)",
      rec.observe(bal(("000660", 5, 1)), trade_date=D, now=NOW) == [] and store.load() == [])
rec.track("005930", "BUY", " ", 10, 70_000, base_quantity=0, base_cost=0)
check("5-2) 주문번호 없으면 추적하지 않고 CRITICAL", "005930" not in rec.tracked and rec.logger.critical.called)

# ── 8-C (F4): 다일 체결 식별 ────────────────────────────────
from domain.position.fill_event import FillEvent  # noqa: E402

store = FillLedgerStore(f"{tempfile.mkdtemp()}/fills.jsonl")
r1 = FillRecorder(store, logger=Mock(), scope="acct-swing")
r1.track("005930", "BUY", "0000001", 10, 70_000, base_quantity=0, base_cost=0, order_date=D)
e1 = r1.observe(bal(("005930", 10, 70_000)), trade_date=D, now=NOW)
r2 = FillRecorder(store, logger=Mock(), scope="acct-swing")      # 다음 날 새 프로세스
D2 = date(2026, 9, 29)
r2.track("005930", "BUY", "0000001", 10, 71_000, base_quantity=10, base_cost=700_000, order_date=D2)
e2 = r2.observe(bal(("005930", 20, 70_500)), trade_date=D2, now=datetime(2026, 9, 29, 10, 0))
check("8-1) [F4 재현] 다른 거래일 같은 주문번호·누적수량 → 서로 다른 사건으로 둘 다 기록",
      len(e1) == 1 and len(e2) == 1 and e1[0].event_id != e2[0].event_id and len(store.load()) == 2)
r2.observe(bal(("005930", 20, 70_500)), trade_date=D2, now=datetime(2026, 9, 29, 11, 0))
check("8-2) 같은 거래의 재조회(다음 폴링)는 중복 0건", len(store.load()) == 2)
conflict = FillEvent(event_id=e2[0].event_id, kind="BUY", symbol="005930", quantity=9, price=1,
                     price_source="ORDER_ESTIMATE", trade_date=D2, occurred_at=NOW)
check("8-3) 같은 식별자·다른 내용은 계속 차단", raises(lambda: store.append(conflict), Exception))
store2 = FillLedgerStore(f"{tempfile.mkdtemp()}/fills.jsonl")
ra, rb = FillRecorder(store2, logger=Mock(), scope="acct-a"), FillRecorder(store2, logger=Mock(), scope="acct-b")
for r in (ra, rb):
    r.track("005930", "BUY", "0000001", 10, 70_000, base_quantity=0, base_cost=0, order_date=D)
check("8-4) 계좌 범위가 다르면 다른 사건", ra.observe(bal(("005930", 10, 70_000)), trade_date=D, now=NOW)[0].event_id
      != rb.observe(bal(("005930", 10, 70_000)), trade_date=D, now=NOW)[0].event_id)
check("8-5) 범위 비었거나 '|' 포함 거부", raises(lambda: FillRecorder(store, scope=" "), ValueError)
      and raises(lambda: FillRecorder(store, scope="a|b"), ValueError))
check("8-6) order_date 없으면 관측 거래일로", FillRecorder(store, scope="s").event_id(
    type("O", (), {"order_date": None, "side": "SELL", "symbol": "000660", "order_id": "9"})(), 3, D)
      == "s|20260928|SELL|000660|9|3")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
