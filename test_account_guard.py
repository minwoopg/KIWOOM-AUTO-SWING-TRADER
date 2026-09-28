# -*- coding: utf-8 -*-
"""계좌 안전 한도 + 전략 인터페이스 회귀 테스트 (스윙 분리 6라운드, 2026-09-28)."""
from __future__ import annotations

import sys
from datetime import date, datetime, time

sys.path.insert(0, ".")

from domain.models import AccountBalance, Position
from domain.risk.account_guard import GuardConfig, check_intent
from domain.service.lot_ledger import PositionSummary
from domain.strategy.interface import NullStrategy, OrderIntent, OrderIntentError
from utils.trading_calendar import MarketPhase

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
    except Exception:
        return False
    return False


CFG = GuardConfig(max_positions=2, max_order_amount=1_000_000, max_total_exposure=2_000_000, min_cash_buffer=100_000)
T = datetime(2026, 9, 28, 10, 0)


def pos(sym, qty, price):
    return PositionSummary(sym, qty, qty * price, date(2026, 9, 21), date(2026, 9, 21), 1, False)


def decide(intent, *, now=T, phase=MarketPhase.REGULAR, cash=5_000_000, positions=None, broker=(), blocked=(),
           prices=None, cfg=CFG):
    bal = AccountBalance(cash, cash, [Position(s, q, a) for s, q, a in broker])
    return check_intent(intent, config=cfg, now=now, phase=phase, balance=bal,
                        positions=positions or {}, prices=prices or {}, blocked_symbols=frozenset(blocked))


buy = lambda sym="005930", q=10, p=50_000: OrderIntent(sym, "BUY", q, p, reason="t")
sell = lambda sym="005930", q=10, p=50_000, forced=False: OrderIntent(sym, "SELL", q, p, reason="t", forced=forced)

# ── 1. 주문 의도 검증 ─────────────────────────────────────────
check("1-1) 잘못된 side 거부", raises(lambda: OrderIntent("005930", "SHORT", 1, 1), OrderIntentError))
check("1-2) 수량 0 거부", raises(lambda: OrderIntent("005930", "BUY", 0, 1), OrderIntentError))
check("1-3) forced 매수 거부", raises(lambda: OrderIntent("005930", "BUY", 1, 1, forced=True), OrderIntentError))
check("1-4) NullStrategy는 주문 없음", NullStrategy().on_tick(None) == [])

# ── 2. 시간·단계 ─────────────────────────────────────────────
check("2-1) 정상 매수 허용", decide(buy()).allowed)
check("2-2) 09:04 신규 주문 차단", decide(buy(), now=datetime(2026, 9, 28, 9, 4)).code == "OUTSIDE_ORDER_WINDOW")
check("2-3) 15:15 신규 주문 차단", decide(buy(), now=datetime(2026, 9, 28, 15, 15)).code == "OUTSIDE_ORDER_WINDOW")
check("2-4) 시간대 밖이라도 강제 매도는 허용",
      decide(sell(forced=True), now=datetime(2026, 9, 28, 15, 18), positions={"005930": pos("005930", 10, 50_000)}).allowed)
check("2-5) 장 시작 전 차단", decide(buy(), phase=MarketPhase.PRE_OPEN).code == "OUTSIDE_SESSION")
check("2-6) 종가 단일가: 일반 주문 차단, 강제 매도 허용",
      decide(sell(), phase=MarketPhase.CLOSING_AUCTION, positions={"005930": pos("005930", 10, 1)}).code == "OUTSIDE_SESSION"
      and decide(sell(forced=True), now=datetime(2026, 9, 28, 15, 22), phase=MarketPhase.CLOSING_AUCTION,
                 positions={"005930": pos("005930", 10, 1)}).allowed)

# ── 3. 종목 차단 ─────────────────────────────────────────────
check("3-1) 장부 불일치 종목 매수 차단", decide(buy(), blocked={"005930"}).code == "SYMBOL_BLOCKED")
check("3-2) 장부 불일치 종목은 강제 매도도 차단(사람 확인 먼저)",
      decide(sell(forced=True), blocked={"005930"}, positions={"005930": pos("005930", 10, 1)}).code == "SYMBOL_BLOCKED")
cfg_allow = GuardConfig(max_positions=2, max_order_amount=1_000_000, max_total_exposure=2_000_000,
                        min_cash_buffer=0, allowed_symbols=("000660",))
check("3-3) 허용 목록 밖 매수 차단", decide(buy(), cfg=cfg_allow).code == "SYMBOL_NOT_ALLOWED")

# ── 4. 금액·수량 한도 ────────────────────────────────────────
check("4-1) 1회 주문 금액 상한", decide(buy(q=21)).code == "ORDER_AMOUNT_LIMIT")
two = {"A": pos("A", 1, 10_000), "B": pos("B", 1, 10_000)}
check("4-2) 보유 종목 수 상한(신규 종목)", decide(buy(), positions=two).code == "MAX_POSITIONS")
check("4-3) 이미 보유한 종목 추가 매수는 종목 수 한도 대상 아님",
      decide(buy("A"), positions=two).allowed)
check("4-4) 잔고에만 있는 종목도 보유 수에 포함", decide(buy(), broker=[("X", 1, 1), ("Y", 1, 1)]).code == "MAX_POSITIONS")
big = {"A": pos("A", 30, 50_000)}  # 150만
check("4-5) 총 노출 상한 (150만 + 50만 = 200만 허용 / 51만 초과)",
      decide(buy("A", 10), positions=big).allowed and decide(buy("A", 11), positions=big).code == "TOTAL_EXPOSURE_LIMIT")
check("4-6) 현재가가 있으면 평가액 기준",
      decide(buy("A", 10), positions=big, prices={"A": 60_000}).code == "TOTAL_EXPOSURE_LIMIT")
check("4-7) 현금 버퍼", decide(buy(q=10), cash=550_000).code == "CASH_BUFFER")

# ── 5. 매도 ─────────────────────────────────────────────────
check("5-1) 원장 보유보다 많이 팔기 차단",
      decide(sell(q=11), positions={"005930": pos("005930", 10, 1)}).code == "SELL_EXCEEDS_LEDGER")
check("5-2) 원장에 없는 종목 매도 차단", decide(sell()).code == "SELL_EXCEEDS_LEDGER")
check("5-3) 매도에는 금액·종목 수 한도 미적용",
      decide(sell(q=100, p=100_000), positions={"005930": pos("005930", 100, 1)}, cash=0).allowed)

# ── 6. 설정 검증 ─────────────────────────────────────────────
check("6-1) 시작 ≥ 끝 거부", raises(lambda: GuardConfig(new_orders_start=time(15, 0), new_orders_end=time(9, 0)), ValueError))
check("6-2) 1회 상한 > 총 노출 상한 거부",
      raises(lambda: GuardConfig(max_order_amount=5, max_total_exposure=4), ValueError))
check("6-3) 0 이하 한도 거부", raises(lambda: GuardConfig(max_positions=0), ValueError))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
