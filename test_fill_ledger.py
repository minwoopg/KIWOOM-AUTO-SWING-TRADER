# -*- coding: utf-8 -*-
"""체결 원장(저장) + 로트 계산 회귀 테스트 (스윙 분리 4라운드, 2026-09-28)."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, ".")

from domain.cost_model import CostModel
from domain.position.fill_event import FillEvent, FillEventError
from domain.service.lot_ledger import LotMatchError, apply_events, net_pnl
from infra.storage.fill_ledger import FillLedgerCorruptError, FillLedgerStore
from utils.trading_calendar import TradingCalendar

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


T0 = datetime(2026, 9, 21, 9, 30)


def ev(eid, kind, qty, price, day, *, sym="005930", src=None, minute=0):
    d = date.fromisoformat(day)
    src = src or ("BROKER_AVG" if kind == "OPENING" else "BROKER_FILL")
    return FillEvent(event_id=eid, kind=kind, symbol=sym, quantity=qty, price=price,
                     price_source=src, trade_date=d,
                     occurred_at=datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=minute))


def store():
    return FillLedgerStore(f"{tempfile.mkdtemp()}/fills.jsonl")


# ── 1. 사건 검증 ─────────────────────────────────────────────
check("1-1) 정상 사건 생성", ev("a", "BUY", 10, 70000, "2026-09-21").quantity == 10)
check("1-2) 수량 0 거부", raises(lambda: ev("a", "BUY", 0, 70000, "2026-09-21"), FillEventError))
check("1-3) 가격 실수 거부", raises(lambda: ev("a", "BUY", 1, 70000.5, "2026-09-21"), FillEventError))
check("1-4) 알 수 없는 kind 거부", raises(lambda: ev("a", "SHORT", 1, 1, "2026-09-21"), FillEventError))
check("1-5) 알 수 없는 가격 출처 거부", raises(lambda: ev("a", "BUY", 1, 1, "2026-09-21", src="GUESS"), FillEventError))
check("1-6) OPENING은 잔고 평균단가 출처만", raises(lambda: ev("a", "OPENING", 1, 1, "2026-09-21", src="BROKER_FILL"),
                                          FillEventError))
check("1-7) trade_date에 datetime 거부", raises(lambda: FillEvent("a", "BUY", "005930", 1, 1, "BROKER_FILL",
                                                           datetime(2026, 9, 21), T0), FillEventError))

# ── 2. 저장소 ────────────────────────────────────────────────
s = store()
e1 = ev("o1:10", "BUY", 10, 70000, "2026-09-21")
check("2-1) 첫 기록 True", s.append(e1) is True)
check("2-2) 같은 사건 재기록은 무시(False) — 재시작 후 중복 기록 방지", s.append(e1) is False)
check("2-3) 같은 id·다른 내용은 예외",
      raises(lambda: s.append(ev("o1:10", "BUY", 11, 70000, "2026-09-21")), FillLedgerCorruptError))
s.append(ev("o2:5", "SELL", 5, 72000, "2026-09-23", src="ORDER_ESTIMATE"))
loaded = s.load()
check("2-4) 기록 순서대로 복원", [e.event_id for e in loaded] == ["o1:10", "o2:5"])
check("2-5) 왕복 후 값 동일", loaded[1] == ev("o2:5", "SELL", 5, 72000, "2026-09-23", src="ORDER_ESTIMATE"))
check("2-6) 파일 없으면 빈 목록", store().load() == [])

# 끊긴 마지막 줄 (쓰는 중 중단)
s = store()
s.append(e1)
with s.path.open("a", encoding="utf-8") as fh:
    fh.write('{"v": 1, "event_id": "o2:5", "kind": "SE')
check("2-7) 끊긴 꼬리는 격리하고 앞의 사건은 유지", [e.event_id for e in s.load()] == ["o1:10"])
check("2-8) 격리 파일 생성", len(s.torn_tail_paths) == 1 and s.torn_tail_paths[0].exists())
check("2-9) 원본에서 끊긴 꼬리 제거 후 정상 추가 가능",
      s.append(ev("o3:1", "BUY", 1, 1000, "2026-09-22")) and [e.event_id for e in s.load()] == ["o1:10", "o3:1"])

# 중간 손상
s = store()
s.path.write_text(json.dumps(e1.to_dict()) + "\n{broken}\n" + json.dumps(
    ev("x", "BUY", 1, 1, "2026-09-22").to_dict()) + "\n", encoding="utf-8")
check("2-10) 중간 줄 손상 → FillLedgerCorruptError", raises(s.load, FillLedgerCorruptError))
s = store()
s.path.write_text(json.dumps(e1.to_dict()) + "\n{broken}\n", encoding="utf-8")
check("2-11) 개행으로 끝난 마지막 줄 손상도 예외(끊긴 쓰기가 아님)", raises(s.load, FillLedgerCorruptError))
s = store()
d2 = e1.to_dict()
d2["price"] = 1
s.path.write_text(json.dumps(e1.to_dict()) + "\n" + json.dumps(d2) + "\n", encoding="utf-8")
check("2-12) 파일 안에 같은 id·다른 내용 → 예외", raises(s.load, FillLedgerCorruptError))
s = store()
d3 = e1.to_dict()
d3["v"] = 2
s.path.write_text(json.dumps(d3) + "\n", encoding="utf-8")
check("2-13) 스키마 버전 불일치 → 예외", raises(s.load, FillLedgerCorruptError))

# ── 3. 로트 계산 ─────────────────────────────────────────────
events = [
    ev("b1", "BUY", 100, 10000, "2026-09-21"),
    ev("b2", "BUY", 50, 11000, "2026-09-22"),
    ev("s1", "SELL", 120, 12000, "2026-09-23"),        # 분할 청산 1차 (로트 2개에 걸침)
    ev("s2", "SELL", 20, 9000, "2026-09-28", src="ORDER_ESTIMATE"),  # 여러 날 뒤 2차
]
r = apply_events(events)
check("3-1) 매도 1건이 로트 2개에 걸쳐 매칭", [(m.quantity, m.buy_event_id) for m in r.realized
                                         if m.sell_event_id == "s1"] == [(100, "b1"), (20, "b2")])
check("3-2) 여러 날에 걸친 분할 청산이 막히지 않음(단타 계산기의 이월 1회 제한 없음)",
      [m.sell_event_id for m in r.realized] == ["s1", "s1", "s2"])
p = r.position("005930")
check("3-3) 남은 수량 10주, 원가 = 11000원 로트", p.quantity == 10 and p.cost_basis == 110000)
check("3-4) 남은 로트의 진입일 = 9/22", p.first_entry_date == date(2026, 9, 22))
check("3-5) 실현손익(9/23) = 100×2000 + 20×1000", r.realized_gross_pnl(date(2026, 9, 23), date(2026, 9, 23)) == 220000)
check("3-6) 실현손익(9/28) = 20×(9000-11000)", r.realized_gross_pnl(date(2026, 9, 28), date(2026, 9, 28)) == -40000)
check("3-7) 추정가가 섞인 매칭 표시", [m.is_estimate for m in r.realized] == [False, False, True])
check("3-8) 평가손익", r.unrealized_gross_pnl({"005930": 12000}) == {"005930": 10000})
check("3-9) 가격 없는 종목은 평가손익에서 제외", r.unrealized_gross_pnl({}) == {})

r = apply_events(events[:2] + [ev("s1", "SELL", 150, 12000, "2026-09-23")])
check("3-10) 전량 청산 후 보유 없음", r.position("005930") is None and r.positions() == {})
check("3-11) 매도 > 보유 로트 → LotMatchError",
      raises(lambda: apply_events([ev("b1", "BUY", 10, 1, "2026-09-21"), ev("s1", "SELL", 11, 1, "2026-09-22")]),
             LotMatchError))
check("3-12) 매수보다 먼저 온 매도(시간순) → LotMatchError",
      raises(lambda: apply_events([ev("s1", "SELL", 1, 1, "2026-09-21"), ev("b1", "BUY", 1, 1, "2026-09-22")]),
             LotMatchError))
r = apply_events([ev("s1", "SELL", 5, 12000, "2026-09-22", minute=10),
                  ev("o", "OPENING", 5, 10000, "2026-09-22", minute=0)])
check("3-13) 기록 순서와 무관하게 발생 시각 순으로 적용 (OPENING → SELL)",
      r.realized[0].buy_event_id == "o" and r.position("005930") is None)
r = apply_events([ev("b1", "BUY", 1, 100, "2026-09-21", sym="A"), ev("b2", "BUY", 2, 200, "2026-09-21", sym="B"),
                  ev("s1", "SELL", 1, 150, "2026-09-22", sym="A")])
check("3-14) 종목별로 따로 계산", list(r.positions()) == ["B"] and r.realized[0].symbol == "A")

# ── 4. 보유 거래일수 / 비용 ──────────────────────────────────
cal = TradingCalendar.load()
r = apply_events([ev("b1", "BUY", 10, 10000, "2026-09-23")])
p = r.position("005930")
check("4-1) 9/23 진입 → 10/6 기준 보유 6거래일 (9/28·29·30·10/1·2·6, 추석·10/5 제외)",
      cal.trading_days_between(p.first_entry_date, date(2026, 10, 6)) == 6)
cm = CostModel(0.0, 0.35, 0.90).validate()
r = apply_events([ev("b1", "BUY", 100, 10000, "2026-09-21"), ev("s1", "SELL", 100, 11000, "2026-09-23")])
check("4-2) 비용 차감(stress 0.90% × 매수원금 100만)", abs(net_pnl(r.realized, cm, "stress") - (100000 - 9000)) < 1e-6)
check("4-3) gross 시나리오는 차감 없음", net_pnl(r.realized, cm, "gross") == 100000)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
