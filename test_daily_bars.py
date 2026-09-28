# -*- coding: utf-8 -*-
"""일봉 데이터 계층 회귀 테스트 (스윙 분리 5라운드, 2026-09-28). 네트워크 없음."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from domain.market_data.daily_bar import BarValidationError, DailyBar, parse_kiwoom_daily_row
from infra.broker.kiwoom_broker import KiwoomHttpError, KiwoomTransportError
from infra.market_data.daily_bar_repository import (
    APPENDED, FAILED, FULL_REFETCH, INITIAL, UNCHANGED, DailyBarRepository,
)
from infra.market_data.daily_bar_source import (
    DataFetchError, KiwoomDailyBarSource, PacedFetcher, RateLimitedError, RawDailyPage, TransientFetchError,
)
from infra.market_data.daily_bar_store import DailyBarStore, DailyBarStoreCorruptError
from utils.trading_calendar import CalendarCoverageError, TradingCalendar

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


cal = TradingCalendar.load()


def trading_days(start: date, end: date) -> list[date]:
    """2026년은 캘린더, 그 이전은 평일로 대신함(테스트용)."""
    out, d = [], start
    while d <= end:
        ok = cal.is_trading_day(d) if d.year in cal.covered_years else d.weekday() < 5
        if ok:
            out.append(d)
        d += timedelta(days=1)
    return out


def row(d: date, close: int, *, sign="+", vol=1000) -> dict:
    return {"dt": d.strftime("%Y%m%d"), "open_pric": f"{sign}{close}", "high_pric": f"{sign}{close + 10}",
            "low_pric": f"{sign}{close - 10}", "cur_prc": f"{sign}{close}", "trde_qty": str(vol)}


class FakeSource:
    """최신→과거 행 목록을 page_size씩 돌려줌. next_key = 오프셋."""

    def __init__(self, dates: list[date], page_size=50, price=lambda i, d: 10000 + i):
        self.page_size = page_size
        self.rows = [row(d, price(i, d)) for i, d in enumerate(dates)][::-1]  # 최신 먼저
        self.fail_queue: list[Exception] = []
        self.calls: list[tuple] = []

    def fetch_page(self, symbol, base_dt, cont_yn="N", next_key=""):
        self.calls.append((symbol, base_dt, cont_yn, next_key))
        if self.fail_queue:
            raise self.fail_queue.pop(0)
        off = int(next_key) if cont_yn == "Y" else 0
        chunk = self.rows[off:off + self.page_size]
        more = off + self.page_size < len(self.rows)
        return RawDailyPage(chunk, "Y" if more else "N", str(off + self.page_size) if more else "")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_repo(source, tmp=None, pages=3):
    tmp = tmp or tempfile.mkdtemp()
    fc = FakeClock()
    fetcher = PacedFetcher(source, min_interval_sec=1.0, retry_backoff_sec=(2, 5), clock=fc.clock, sleep=fc.sleep)
    return DailyBarRepository(DailyBarStore(f"{tmp}/bars"), fetcher, cal, backfill_pages=pages, logger=Mock()), tmp, fc


INTRADAY = datetime(2026, 9, 28, 10, 30)
POSTCLOSE = datetime(2026, 9, 28, 16, 0)
ALL = trading_days(date(2025, 6, 2), date(2026, 9, 28))   # 9/28(오늘) 포함 — 장중에는 미완성

# ── 1. 행 파싱·검증 ──────────────────────────────────────────
b = parse_kiwoom_daily_row(row(date(2026, 9, 23), 70000, sign="-"))
check("1-1) 등락 부호(-)는 절대값으로", b.close == 70000 and b.low == 69990)
check("1-2) 숫자 아닌 가격 → 예외", raises(lambda: parse_kiwoom_daily_row({**row(date(2026, 9, 23), 1), "cur_prc": "abc"}),
                                          BarValidationError))
check("1-3) 빈 가격(0으로 채우지 않음) → 예외",
      raises(lambda: parse_kiwoom_daily_row({**row(date(2026, 9, 23), 1000), "open_pric": ""}), BarValidationError))
check("1-4) 날짜 형식 오류 → 예외", raises(lambda: parse_kiwoom_daily_row({**row(date(2026, 9, 23), 1000), "dt": "2026-09-23"}),
                                         BarValidationError))
check("1-5) 존재하지 않는 날짜 → 예외", raises(lambda: parse_kiwoom_daily_row({**row(date(2026, 9, 23), 1000), "dt": "20260231"}),
                                           BarValidationError))
check("1-6) 고가 < 종가 모순 → 예외", raises(lambda: DailyBar(date(2026, 9, 23), 100, 100, 90, 110, 1), BarValidationError))
check("1-7) 거래량 0은 허용", DailyBar(date(2026, 9, 23), 100, 100, 100, 100, 0).volume == 0)
check("1-8) 기존 PriceBar로 변환", b.to_price_bar().date == "20260923")

# ── 2. 호출 간격·재시도 ─────────────────────────────────────
src = FakeSource(ALL)
fc = FakeClock()
f = PacedFetcher(src, min_interval_sec=1.0, retry_backoff_sec=(2, 5), clock=fc.clock, sleep=fc.sleep)
f.fetch_page("005930", "20260928")
f.fetch_page("005930", "20260928")
check("2-1) 연속 호출 사이 1초 대기", fc.sleeps == [1.0])
src.fail_queue = [RateLimitedError("429"), TransientFetchError("reset")]
fc.sleeps.clear()
f.fetch_page("005930", "20260928")
check("2-2) 429 → 2초, 전송 실패 → 5초 대기 후 재시도 성공", [s for s in fc.sleeps if s != 1.0] == [2, 5]
      and f.retries == 2)
src.fail_queue = [RateLimitedError("429")] * 3
check("2-3) 재시도 한도 초과 → DataFetchError", raises(lambda: f.fetch_page("005930", "x"), DataFetchError))
src.fail_queue = [DataFetchError("HTTP 500")]
n = f.calls
check("2-4) 재시도 대상 아닌 오류는 즉시 전파", raises(lambda: f.fetch_page("005930", "x"), DataFetchError)
      and f.calls == n + 1)

# ── 3. 첫 수집 (장중) ────────────────────────────────────────
src = FakeSource(ALL, page_size=100)
repo, tmp, _ = make_repo(src)
r = repo.update("005930", INTRADAY)
check("3-1) INITIAL", r.action == INITIAL)
check("3-2) 장중 당일(9/28) 미완성 봉은 버림", r.dropped_incomplete == 1 and r.last_date == date(2026, 9, 23))
check("3-3) 완성 기준일까지 최신 (is_current)", r.is_current and r.completed_through == date(2026, 9, 23))
check("3-4) backfill_pages(3)만큼만 받음", r.pages_fetched == 3 and r.total == 300 - 1)
check("3-5) 요청 base_dt는 오늘(실측 확인된 형태)", src.calls[0][1] == "20260928")
bars = repo.completed_bars("005930", INTRADAY)
check("3-6) 조회 결과에 미래·미완성 봉 없음", bars[-1].date == date(2026, 9, 23))
check("3-7) lookback", [x.date for x in repo.completed_bars("005930", INTRADAY, lookback=3)]
      == [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)])
check("3-8) 과거 시점(9/22 장중)으로 조회하면 9/21까지만 — 미래 봉 차단",
      repo.completed_bars("005930", datetime(2026, 9, 22, 11, 0))[-1].date == date(2026, 9, 21))
meta = json.loads(Path(tmp, "bars", "005930.meta.json").read_text(encoding="utf-8"))
check("3-9) meta: 수정주가·출처·완성 기준일 기록", meta["adjusted"] and meta["source"] == "kiwoom_ka10081"
      and meta["completed_through"] == "2026-09-23")

# ── 4. 증분 갱신 ─────────────────────────────────────────────
r = repo.update("005930", INTRADAY)
check("4-1) 같은 날 다시 → UNCHANGED, 1페이지만", r.action == UNCHANGED and r.pages_fetched == 1)
r = repo.update("005930", POSTCLOSE)
check("4-2) 장 마감 후 → 9/28 봉 추가(APPENDED +1)", r.action == APPENDED and r.added == 1
      and r.last_date == date(2026, 9, 28) and r.dropped_incomplete == 0)
check("4-3) 기존 이력 유지 (총 300)", r.total == 300)

# ── 5. 수정주가 변경 감지 ────────────────────────────────────
split = date(2026, 9, 1)
src2 = FakeSource(ALL, page_size=100, price=lambda i, d: (10000 + i) // (2 if d < split else 1))
repo.fetcher.source = src2
r = repo.update("005930", POSTCLOSE)
check("5-1) 겹치는 구간 값이 바뀌면 FULL_REFETCH", r.action == FULL_REFETCH and "수정주가" in r.refetch_reason)
check("5-2) 저장본이 새 값(분할 반영)으로 교체됨",
      next(x for x in repo.completed_bars("005930", POSTCLOSE) if x.date == date(2026, 8, 31)).close
      == (10000 + ALL.index(date(2026, 8, 31))) // 2)
check("5-3) 기존 깊이(가장 오래된 날짜)까지 다시 받음", not r.history_truncated)

# 로컬에 있던 날짜가 새 응답에서 빠진 경우도 변경으로 봄
dates_missing = [d for d in ALL if d != date(2026, 9, 10)]
repo.fetcher.source = FakeSource(dates_missing, page_size=100,
                                 price=lambda i, d: (10000 + ALL.index(d)) // (2 if d < split else 1))
r = repo.update("005930", POSTCLOSE)
check("5-4) 로컬에 있던 날짜가 응답에서 사라져도 FULL_REFETCH", r.action == FULL_REFETCH)
check("5-5) 캘린더상 거래일 누락은 보고만 (실패 아님)", date(2026, 9, 10) in r.missing_sessions)

# ── 6. 공백 (오래 갱신 안 함) ────────────────────────────────
src = FakeSource(ALL, page_size=100)
repo, tmp, _ = make_repo(src)
repo.update("005930", datetime(2026, 1, 20, 16, 0))           # 1/20까지 저장
r = repo.update("005930", POSTCLOSE)                          # 1페이지(100행)가 1/20까지 못 닿음
check("6-1) 이어 붙일 수 없으면 FULL_REFETCH", r.action == FULL_REFETCH and "공백" in r.refetch_reason)

# ── 7. 이상한 행 → 저장 안 함 ─────────────────────────────────
src = FakeSource(ALL, page_size=100)
repo, tmp, _ = make_repo(src)
repo.update("005930", INTRADAY)
before = Path(tmp, "bars", "005930.csv").read_text(encoding="utf-8")
src.rows[1]["cur_prc"] = "N/A"
r = repo.update("005930", POSTCLOSE)
check("7-1) 이상한 행이 있으면 FAILED", r.action == FAILED and "cur_prc" in r.error)
check("7-2) 실패 시 로컬 파일 그대로", Path(tmp, "bars", "005930.csv").read_text(encoding="utf-8") == before)
check("7-3) 잘못된 종목코드 → FAILED", repo.update("../x", POSTCLOSE).action == FAILED)

# ── 8. 저장소 무결성 ─────────────────────────────────────────
src = FakeSource(ALL, page_size=100)
repo, tmp, _ = make_repo(src)
repo.update("005930", INTRADAY)
store = repo.store
mp = Path(tmp, "bars", "005930.meta.json")
m = json.loads(mp.read_text(encoding="utf-8"))
m["row_count"] += 1
mp.write_text(json.dumps(m), encoding="utf-8")
check("8-1) meta와 CSV 행 수 불일치 → 손상", raises(lambda: store.load("005930"), DailyBarStoreCorruptError))
r = repo.update("005930", INTRADAY)
check("8-2) 손상된 로컬은 전체 재수집으로 복구", r.action == FULL_REFETCH and "로컬 손상" in r.refetch_reason)
check("8-3) 완성 기준일 이후 봉 저장 거부",
      raises(lambda: store.save("000660", [DailyBar(date(2026, 9, 28), 1, 1, 1, 1, 1)], adjusted=True,
                                 source="t", fetched_at="t", completed_through=date(2026, 9, 23)), ValueError))
Path(tmp, "bars", "000660.csv").write_text("x", encoding="utf-8")
check("8-4) CSV만 있고 meta 없음 → 손상", raises(lambda: store.load("000660"), DailyBarStoreCorruptError))
check("8-5) 임시 파일 남지 않음", list(Path(tmp, "bars").glob("*.tmp")) == [])

# ── 9. 캘린더 범위 밖 ───────────────────────────────────────
check("9-1) 캘린더가 없는 연도(2027)에는 갱신 거부(추측 금지)",
      raises(lambda: repo.update("005930", datetime(2027, 1, 5, 16, 0)), CalendarCoverageError))


# ── 10. 키움 소스 어댑터 ─────────────────────────────────────
class _Resp:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers


class _Broker:
    def __init__(self, result):
        self.result, self.calls = result, []

    def _post(self, **kw):
        self.calls.append(kw)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


br = _Broker(_Resp({"return_code": 0, "stk_dt_pole_chart_qry": [row(date(2026, 9, 23), 1000)]},
                   {"cont-yn": "y", "next-key": " K1 "}))
pg = KiwoomDailyBarSource(br).fetch_page("005930", "20260928", "N", "")
check("10-1) ka10081 + 수정주가(upd_stkpc_tp=1)로 요청", br.calls[0]["api_id"] == "ka10081"
      and br.calls[0]["payload"] == {"stk_cd": "005930", "base_dt": "20260928", "upd_stkpc_tp": "1"})
check("10-2) 연속조회 헤더 정규화", pg.cont_yn == "Y" and pg.next_key == "K1" and pg.has_more)
check("10-3) HTTP 429 → RateLimitedError",
      raises(lambda: KiwoomDailyBarSource(_Broker(KiwoomHttpError("x", 429, {}))).fetch_page("A", "b"), RateLimitedError))
check("10-4) 전송 실패 → TransientFetchError",
      raises(lambda: KiwoomDailyBarSource(_Broker(KiwoomTransportError("x"))).fetch_page("A", "b"), TransientFetchError))
check("10-5) 그 외 HTTP 오류 → DataFetchError(재시도 안 함)",
      raises(lambda: KiwoomDailyBarSource(_Broker(KiwoomHttpError("x", 500, {}))).fetch_page("A", "b"), DataFetchError))
check("10-6) 업무 오류(return_code≠0) → DataFetchError",
      raises(lambda: KiwoomDailyBarSource(_Broker(RuntimeError("business error"))).fetch_page("A", "b"), DataFetchError))
check("10-7) 목록 없는 응답 → DataFetchError",
      raises(lambda: KiwoomDailyBarSource(_Broker(_Resp({"return_code": 0}, {}))).fetch_page("A", "b"), DataFetchError))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
