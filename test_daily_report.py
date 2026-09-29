# -*- coding: utf-8 -*-
"""일일 리포트·번들·마스킹 회귀 테스트 (스윙 분리 7라운드, 2026-09-28)."""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, ".")

from app.reports import export_bundle, generate_daily_report
from domain.cost_model import CostModel
from domain.models import AccountBalance, Position
from domain.position.fill_event import FillEvent
from domain.position.position_book import reconcile
from domain.position.swing_state import PositionMeta, SwingState
from domain.service.lot_ledger import apply_events
from infra.market_data.daily_bar_store import DailyBarStore
from domain.market_data.daily_bar import DailyBar
from infra.reporting.daily_report import ReportInputs, build_daily_report, write_report
from infra.reporting.masking import mask_json, mask_text
from infra.storage.fill_ledger import FillLedgerStore
from infra.storage.swing_state_store import SwingStateStore
from testing_helpers import build_minimal_settings
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


cal = TradingCalendar.load()
D = date(2026, 9, 28)


def ev(eid, kind, sym, qty, price, day, src="BROKER_FILL", hh=10):
    d = date.fromisoformat(day)
    return FillEvent(eid, kind, sym, qty, price, src, d, datetime(d.year, d.month, d.day, hh, 0), order_id=eid)


EVENTS = [
    ev("b1", "BUY", "005930", 10, 70_000, "2026-09-22", src="BROKER_AVG"),
    ev("b2", "BUY", "000660", 5, 180_000, "2026-09-23", src="BROKER_AVG"),
    ev("s1", "SELL", "005930", 4, 72_000, "2026-09-28", src="ORDER_ESTIMATE"),
]
CM = CostModel(0.0, 0.35, 0.90).validate()

# ── 1. 리포트 본문 ───────────────────────────────────────────
text = build_daily_report(ReportInputs(
    trade_date=D, events=EVENTS,
    metas={"005930": PositionMeta("005930", strategy_id="s1", stop_price=65_000)},
    closes={"005930": (D, 71_000)},
    unresolved_intents=["000660"], journal_symbols=[],
    reconcile=reconcile(apply_events(EVENTS), AccountBalance(1, 1, [Position("005930", 6, 70_000)]),
                        {"005930": PositionMeta("005930")}),
    session_lines=["거래일 2026-09-28 | 상태 COMPLETED"],
    generated_at=datetime(2026, 9, 28, 15, 40)), calendar=cal, cost_model=CM)
check("1-1) 제목에 날짜·요일", "# 스윙 일일 리포트 — 2026-09-28 (월)" in text)
check("1-2) 당일 실현 = 4×2,000 = +8,000, 추정가 표시", "| 당일 실현손익 (비용 전) | +8,000 ⚠추정가 포함 |" in text)
check("1-3) Stress 비용 차감 = 8,000 − 280,000×0.9%", "| 당일 실현손익 (Stress 비용 차감) | +5,480 |" in text)
check("1-4) 보유 행: 수량·평균단가·첫 진입일·보유 거래일(9/22→9/28 = 2)·종가·평가손익·손절가·전략",
      "| 005930 | 6 | 70,000 | 2026-09-22 | 2 | 71,000 (2026-09-28) | +6,000 | 65,000 | s1 |" in text)
check("1-5) 종가 없는 종목은 평가 제외 안내", "평가 제외(완성 일봉 없음): 000660" in text
      and "| 000660 | 5 |" in text and "메타 없음" in text)
check("1-6) 당일 체결 표에 가격 출처", "| 10:00:00 | 005930 | SELL | 4 | 72,000 | ORDER_ESTIMATE | s1 |" in text)
check("1-7) 실현 매칭에 보유 거래일(2)과 추정 표시", "| 005930 | 4 | 2026-09-22 | 70,000 | 72,000 | +8,000 | 2 | 예 |" in text)
check("1-8) 미해결 주문 경고", "⚠ 미해결 주문" in text and "000660" in text)
check("1-9) 장부 대조 불일치 표시(원장 000660 vs 잔고 없음)", "LEDGER_ONLY 000660" in text)
check("1-10) 세션 요약 포함", "### 세션 요약" in text and "상태 COMPLETED" in text)
check("1-11) 비용 기준 설명", "비용 기준:" in text)
empty = build_daily_report(ReportInputs(trade_date=D, events=[], balance_available=False), calendar=cal)
check("1-12) 빈 원장: 보유·체결·실현 모두 '없음', 잔고 없이 생성 표시",
      empty.count("보유 없음") == 1 and "## 당일 체결 (원장)\n\n없음" in empty and "잔고 없이 생성" in empty)

# ── 2. 저장 ─────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
p = write_report(tmp / "reports", D, text)
check("2-1) reports/daily_report_<날짜>.md", p.name == "daily_report_2026-09-28.md" and p.read_text(encoding="utf-8") == text)
check("2-2) 임시 파일 없음", list((tmp / "reports").glob("*.tmp")) == [])

# ── 3. 파일에서 조립 (generate_daily_report) ─────────────────
tmp = tempfile.mkdtemp()
settings = build_minimal_settings(tmp)
store = FillLedgerStore(settings.storage.fill_ledger_file)
for e in EVENTS:
    store.append(e)
st = SwingState()
st.upsert_position_meta(PositionMeta("005930", strategy_id="s1"))
SwingStateStore(settings.storage.state_file).save(st)
DailyBarStore(settings.market_data.daily_bars_dir).save(
    "005930", [DailyBar(date(2026, 9, 23), 70_000, 71_000, 69_000, 70_500, 100),
               DailyBar(D, 70_500, 71_500, 70_000, 71_000, 100)],
    adjusted=True, source="t", fetched_at="t", completed_through=D)
path = generate_daily_report(settings, D, balance=AccountBalance(1, 1, [Position("005930", 6, 70_000),
                                                                     Position("000660", 5, 180_000)]), today=D)
body = path.read_text(encoding="utf-8")
check("3-1) 원장·상태·일봉에서 리포트 생성", "| 005930 | 6 | 70,000 |" in body and "71,000 (2026-09-28)" in body)
check("3-2) 잔고를 주면 장부 대조 포함(일치 — 참고 사항만)", "장부 대조" in body and "QTY_MISMATCH" not in body)
past = generate_daily_report(settings, date(2026, 9, 23), today=D).read_text(encoding="utf-8")
check("3-3) 지난 날짜는 그날 이하 완성 종가로 평가", "70,500 (2026-09-23)" in past)

# ── 4. 마스킹 ────────────────────────────────────────────────
check("4-1) 앱키·토큰·계좌 형태 가림",
      mask_text('appkey=AK123 {"token": "T0K"} acct 1234567890 Bearer abcdefghij') ==
      'appkey=*** {"token": "***"} acct *** Bearer ***')
check("4-2) 종목코드·주문번호(7자리)·날짜는 보존", mask_text("005930 0001234 20260928") == "005930 0001234 20260928")
check("4-3) JSON은 키 기준으로만 가림(구조·주문번호 보존)",
      mask_json({"secretkey": "x", "order_id": "1234567890", "n": 1}) == {"secretkey": "***", "order_id": "1234567890", "n": 1})

# ── 5. 번들 ─────────────────────────────────────────────────
log = Path(settings.storage.app_log_file)
log.parent.mkdir(parents=True, exist_ok=True)
log.write_text("2026-09-27 10:00:00 | INFO | 전날 줄\n2026-09-28 10:00:00 | INFO | appkey=SECRETAPPKEY 오늘\n"
               "2026-09-28 10:01:00 | INFO | 계좌 1234567890\n", encoding="utf-8")
Path(str(log) + ".1").write_text("2026-09-28 09:00:00 | INFO | 순환된 오늘 줄\n", encoding="utf-8")
trades = Path(settings.storage.trade_log_file)
trades.write_text("timestamp,symbol\n2026-09-27T10:00:00,A\n2026-09-28T10:00:00,005930\n", encoding="utf-8")
zp = export_bundle(settings, D, root=Path("."), out_dir=Path(tmp) / "exports")
z = zipfile.ZipFile(zp)
names = set(z.namelist())
check("5-1) 번들 파일 목록", {"app.log", "trades.csv", "fill_ledger.jsonl", "state.json", "manifest.json",
                            "daily_report_2026-09-28.md"} <= names)
applog = z.read("app.log").decode("utf-8")
check("5-2) 그날 줄만(순환 파일 포함)", "전날 줄" not in applog and "순환된 오늘 줄" in applog and applog.count("\n") == 2)
check("5-3) 앱키·계좌번호 가림", "SECRETAPPKEY" not in applog and "1234567890" not in applog)
check("5-4) CSV는 헤더 + 그날 행", z.read("trades.csv").decode("utf-8") == "timestamp,symbol\n2026-09-28T10:00:00,005930")
man = json.loads(z.read("manifest.json"))
check("5-5) manifest에 날짜·해시·빈 파일 표시", man["trade_date"] == "2026-09-28"
      and all("sha256" in v for v in man["files"].values()) and man["files"]["tracked_order_journal.json"]["empty"])
check("5-6) 원장은 JSON 구조 유지", all(json.loads(l) for l in z.read("fill_ledger.jsonl").decode().splitlines()))
check("5-7) 임시 zip 없음", not list((Path(tmp) / "exports").glob("*.tmp")))

# ── 6. 8-D (F6): 과거 리포트는 기준일까지의 원장만 ───────────────
F6_EVENTS = [ev("fb", "BUY", "005930", 10, 70_000, "2026-09-28", src="BROKER_AVG"),
             ev("fs", "SELL", "005930", 10, 71_000, "2026-09-29", src="ORDER_ESTIMATE")]
r = build_daily_report(ReportInputs(trade_date=D, events=F6_EVENTS, closes={"005930": (D, 70_500)},
                                    balance_available=False, historical=True,
                                    generated_at=datetime(2026, 9, 30, 9, 0)), calendar=cal)
check("6-1) [F6 재현] 9/29 매도 후 9/28 재생성 → 9/28 보유 10주 유지",
      "| 005930 | 10 | 70,000 | 2026-09-28 |" in r and "| 보유 종목 | 1 |" in r)
check("6-2) 누적 실현손익에 이후 매도 미반영(0)", "| 누적 실현손익 (비용 전) | +0 |" in r)
check("6-3) 이후 사건 제외 안내·과거 재생성 표시", "기준일 이후 체결 사건 1건은 반영하지 않음" in r and "과거 날짜 재생성" in r)
r_meta = build_daily_report(ReportInputs(trade_date=D, events=F6_EVENTS, historical=True,
                                         metas={"005930": PositionMeta("005930", strategy_id="now", stop_price=1)},
                                         unresolved_intents=["005930"], balance_available=False), calendar=cal)
check("6-4) 과거 재생성에는 생성 시점 메타·미해결 주문을 그날 값처럼 넣지 않음",
      "| now |" not in r_meta and "⚠ 미해결 주문" not in r_meta)
r29 = build_daily_report(ReportInputs(trade_date=date(2026, 9, 29), events=F6_EVENTS,
                                      balance_available=False), calendar=cal)
check("6-5) 9/29 리포트는 청산 반영(보유 없음, 실현 +10,000)", "보유 없음" in r29
      and "| 누적 실현손익 (비용 전) | +10,000 |" in r29)
tmp6 = tempfile.mkdtemp()
s6 = build_minimal_settings(tmp6)
st6 = FillLedgerStore(s6.storage.fill_ledger_file)
for e in F6_EVENTS:
    st6.append(e)
before = generate_daily_report(s6, D, today=date(2026, 9, 30)).read_text(encoding="utf-8")
st6.append(ev("fb2", "BUY", "000660", 1, 100_000, "2026-09-30", src="BROKER_AVG"))
after = generate_daily_report(s6, D, today=date(2026, 9, 30)).read_text(encoding="utf-8")
strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("생성 ") and "기준일 이후" not in l)
check("6-6) 이후 매매를 추가해도 과거 리포트의 보유·손익 수치는 동일",
      strip(before) == strip(after) and "| 005930 | 10 |" in after)
check("6-7) 과거 재생성은 잔고를 줘도 장부 대조를 넣지 않음",
      "LEDGER_ONLY" not in generate_daily_report(s6, D, balance=AccountBalance(1, 1, []),
                                                 today=date(2026, 9, 30)).read_text(encoding="utf-8"))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
