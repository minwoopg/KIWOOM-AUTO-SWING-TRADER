# -*- coding: utf-8 -*-
"""포지션 장부 대조 회귀 테스트 (스윙 분리 4라운드, 2026-09-28)."""
from __future__ import annotations

import sys
from datetime import date, datetime

sys.path.insert(0, ".")

from domain.models import AccountBalance, Position
from domain.position.fill_event import FillEvent
from domain.position.position_book import (
    AVG_PRICE_GAP, LEDGER_ONLY, META_MISSING, META_ORPHAN, QTY_MISMATCH, UNTRACKED_HOLDING,
    opening_events_from_balance, reconcile,
)
from domain.position.swing_state import PositionMeta
from domain.service.lot_ledger import apply_events

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


def buy(sym, qty, price, day="2026-09-21", eid=None, src="BROKER_FILL"):
    d = date.fromisoformat(day)
    return FillEvent(eid or f"{sym}:{qty}:{day}", "BUY", sym, qty, price, src, d,
                     datetime.combine(d, datetime.min.time()))


def bal(*positions):
    return AccountBalance(10_000_000, 10_000_000, [Position(s, q, a) for s, q, a in positions])


# ── 1. 일치 ─────────────────────────────────────────────────
ledger = apply_events([buy("005930", 10, 70000)])
r = reconcile(ledger, bal(("005930", 10, 70000)), {"005930": PositionMeta("005930")})
check("1-1) 원장·잔고·메타가 맞으면 어긋남 없음", r.ok and r.issues == [])

# ── 2. 수량 관련 (blocking) ───────────────────────────────────
r = reconcile(ledger, bal(("005930", 7, 70000)), {"005930": PositionMeta("005930")})
check("2-1) 수량 불일치 → QTY_MISMATCH, blocking", r.symbols_with(QTY_MISMATCH) == ["005930"] and not r.ok)
r = reconcile(ledger, bal(), {"005930": PositionMeta("005930")})
check("2-2) 원장만 보유 → LEDGER_ONLY, blocking", r.symbols_with(LEDGER_ONLY) == ["005930"] and not r.ok)
r = reconcile(apply_events([]), bal(("000660", 5, 180000)), {})
check("2-3) 잔고만 보유(HTS 수동매매 등) → UNTRACKED_HOLDING, blocking",
      r.symbols_with(UNTRACKED_HOLDING) == ["000660"] and not r.ok)
r = reconcile(ledger, bal(("005930", 7, 70000)), {"005930": PositionMeta("005930")}, orders_in_flight=True)
check("2-4) 주문 진행 중이면 수량 불일치는 참고(blocking 아님)", r.symbols_with(QTY_MISMATCH) == ["005930"] and r.ok)

# ── 3. 참고 항목 ─────────────────────────────────────────────
r = reconcile(ledger, bal(("005930", 10, 70000)), {})
check("3-1) 메타 없음 → META_MISSING (참고)", r.symbols_with(META_MISSING) == ["005930"] and r.ok)
r = reconcile(apply_events([]), bal(), {"005930": PositionMeta("005930")})
check("3-2) 메타만 남음 → META_ORPHAN (참고)", r.symbols_with(META_ORPHAN) == ["005930"] and r.ok)
est = apply_events([buy("005930", 10, 70000, src="ORDER_ESTIMATE")])
r = reconcile(est, bal(("005930", 10, 71000)), {"005930": PositionMeta("005930")})
check("3-3) 평균단가 차이 >0.5% → AVG_PRICE_GAP (참고, 추정가 포함 표시)",
      r.symbols_with(AVG_PRICE_GAP) == ["005930"] and r.ok and "추정가 포함" in r.lines()[0])
r = reconcile(ledger, bal(("005930", 10, 70200)), {"005930": PositionMeta("005930")})
check("3-4) 허용 오차 이내 차이는 보고 안 함", r.issues == [])

# ── 4. 기존 보유분 인수 도우미 ─────────────────────────────────
b = bal(("000660", 5, 180000), ("005930", 3, 0))
evs = opening_events_from_balance(b, ["000660"], date(2026, 9, 28), note="HTS 확인 후 인수")
check("4-1) OPENING 사건: 잔고 수량·평균단가·BROKER_AVG",
      len(evs) == 1 and evs[0].kind == "OPENING" and evs[0].quantity == 5 and evs[0].price == 180000
      and evs[0].price_source == "BROKER_AVG")
check("4-2) 인수 후 대조하면 일치", reconcile(apply_events(evs), bal(("000660", 5, 180000)),
                                     {"000660": PositionMeta("000660", origin="ADOPTED")}).issues == [])
for sym, label in (("999999", "4-3) 잔고에 없는 종목 인수 거부"), ("005930", "4-4) 평균단가 0이면 인수 거부")):
    try:
        opening_events_from_balance(b, [sym], date(2026, 9, 28))
        check(label, False)
    except ValueError:
        check(label, True)
check("4-5) 같은 날 같은 종목 인수는 같은 event_id (중복 기록 방지)",
      opening_events_from_balance(b, ["000660"], date(2026, 9, 28))[0].event_id == evs[0].event_id)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
