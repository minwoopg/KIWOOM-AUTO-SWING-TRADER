# -*- coding: utf-8 -*-
"""tools/probe_market_data.py 회귀 테스트 (네트워크 없음, 가짜 세션)."""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from tools.probe_market_data import (
    ProbeConfigError, analyze_orders, assert_mock_domain, main, parse_yyyymmdd, redact,
)
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


def raises(fn, exc=ProbeConfigError) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    """api-id별로 페이지 응답을 돌려주고, 호출된 URL·헤더를 기록."""

    def __init__(self, daily_rows_pages, oso_rows=(), cntr_rows=(), cur_prc="+71000"):
        self.daily = list(daily_rows_pages)
        self.oso, self.cntr, self.cur = list(oso_rows), list(cntr_rows), cur_prc
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, dict(headers or {}), json))
        if url.endswith("/oauth2/token"):
            return _Resp(200, {"return_code": 0, "token": "SECRET_TOKEN_VALUE"})
        api = headers["api-id"]
        if api == "ka10001":
            return _Resp(200, {"return_code": 0, "cur_prc": self.cur, "acnt_no": "1234567890"})
        if api == "ka10081":
            idx = 0 if headers["cont-yn"] == "N" else int(headers["next-key"])
            more = idx + 1 < len(self.daily)
            return _Resp(200, {"return_code": 0, "stk_dt_pole_chart_qry": self.daily[idx]},
                         {"cont-yn": "Y" if more else "N", "next-key": str(idx + 1) if more else ""})
        if api == "ka10075":
            return _Resp(200, {"return_code": 0, "oso": self.oso}, {"cont-yn": "N"})
        if api == "ka10076":
            return _Resp(200, {"return_code": 0, "cntr": self.cntr}, {"cont-yn": "N"})
        raise AssertionError(f"unexpected api {api}")


cal = TradingCalendar.load()


def bars(dates, close="+71000"):
    return [{"dt": d, "cur_prc": close, "open_pric": "70000", "trde_qty": "100"} for d in dates]


def run(session, now, extra=()):
    tmp = Path(tempfile.mkdtemp())
    env = tmp / ".env"
    env.write_text("KIWOOM_APP_KEY=AK_SECRET\nKIWOOM_SECRET_KEY=SK_SECRET\n", encoding="utf-8")
    main(["--env-file", str(env), "--out-dir", str(tmp), "--sleep", "0", "--symbols", "005930", *extra],
         session=session, now=now, calendar=cal)
    summary = next(tmp.glob("*_summary.txt")).read_text(encoding="utf-8")
    raw = next(p for p in tmp.glob("*.jsonl")).read_text(encoding="utf-8")
    data = json.loads(summary.split("\n", 1)[1])
    return data, raw


# ── 1. 안전장치 ─────────────────────────────────────────────
check("1-1) 모의투자 도메인 허용", not raises(lambda: assert_mock_domain("https://mockapi.kiwoom.com")))
check("1-2) 실전 도메인 거부", raises(lambda: assert_mock_domain("https://api.kiwoom.com")))
check("1-3) http 거부", raises(lambda: assert_mock_domain("http://mockapi.kiwoom.com")))
check("1-4) 비슷한 도메인 우회 거부", raises(lambda: assert_mock_domain("https://mockapi.kiwoom.com.evil.io")))
check("1-5) 비표준 포트 거부", raises(lambda: assert_mock_domain("https://mockapi.kiwoom.com:8443")))
check("1-6) CLI로 실전 도메인 지정 시 네트워크 호출 전에 중단",
      raises(lambda: main(["--base-url", "https://api.kiwoom.com", "--env-file", "x"], session=FakeSession([]))))
check("1-7) 계좌번호·토큰 키 값 가림", redact({"acnt_no": "1", "x": [{"token": "t", "a": 1}]})
      == {"acnt_no": "[REDACTED]", "x": [{"token": "[REDACTED]", "a": 1}]})
check("1-8) 날짜 파싱", parse_yyyymmdd("20260928") is not None and parse_yyyymmdd("153000") is None
      and parse_yyyymmdd("20261399") is None)

# ── 2. 장중 실행 — 당일 미완성 봉 포함 ─────────────────────────
page1 = bars(["20260928", "20260923", "20260922", "20260921", "20260918"])
page2 = bars(["20260917", "20260916", "20260915", "20260914"])
s = FakeSession([page1, page2])
data, raw = run(s, datetime(2026, 9, 28, 10, 30))
d = data["daily"][0]
check("2-1) 장중 단계로 판정", data["phase_at_run"] == "REGULAR")
check("2-2) 당일 봉 포함 + 종가 칸 = 현재가 판정", d["today_row_present"]
      and d["verdict"].startswith("장중 조회에 당일 미완성 봉이 포함됨 (종가 칸 = 현재가)"))
check("2-3) 연속조회 2페이지 수집", d["page_sizes"] == [5, 4] and d["stop_reason"] == "END")
check("2-4) 최신→과거 정렬 확인", d["descending_order"] and d["duplicate_dates"] == 0)
check("2-5) 캘린더 대조 기준일 = 마지막 완성 거래일(9/23)", data["calendar"]["range"].endswith("2026-09-23"))
check("2-6) 캘린더와 일치 (9/24·25 휴장, 당일 미완성 봉은 대조 제외)",
      data["calendar"]["missing_bars"] == [] and data["calendar"]["unexpected_bars"] == [])
check("2-7) 원시 기록에 토큰·앱키·계좌번호 원문 없음",
      "SECRET_TOKEN_VALUE" not in raw and "AK_SECRET" not in raw and "1234567890" not in raw)
check("2-8) 주문 API(kt10000/kt10001) 호출 없음",
      all(c[1].get("api-id", "") not in ("kt10000", "kt10001", "kt10003") for c in s.calls))

# ── 3. 캘린더 불일치 검출 ─────────────────────────────────────
s = FakeSession([bars(["20260923", "20260922", "20260918", "20260917"])])  # 9/21 누락
data, _ = run(s, datetime(2026, 9, 28, 16, 0), ["--skip-orders"])
check("3-1) 장 마감 후 당일 봉 없음 판정", "장 마감 후인데 당일 봉 없음" in data["daily"][0]["verdict"])
check("3-2) 누락 거래일(9/21)과 당일(9/28) 누락 검출",
      data["calendar"]["missing_bars"] == ["2026-09-21", "2026-09-28"])

# ── 4. 주문 조회 범위 판정 ───────────────────────────────────
pre = datetime(2026, 9, 28, 8, 30)
r = analyze_orders("ka10076", [[{"ord_no": "1", "tm": "142030"}]], "END", pre, cal)
check("4-1) 장 시작 전 행 있음 → 이전 거래일 주문 조회됨", "이전 거래일 주문도 조회됨" in r["verdict"])
r = analyze_orders("ka10076", [[]], "END", pre, cal)
check("4-2) 장 시작 전 0건 → 판정 보류 안내", "주문 이력 있는 계좌로 재확인" in r["verdict"])
r = analyze_orders("ka10075", [[{"ord_no": "1", "ord_dt": "20260923"}]], "END",
                   datetime(2026, 9, 28, 11, 0), cal)
check("4-3) 날짜 필드가 과거면 시각과 무관하게 확정", r["verdict"].startswith("이전 날짜 주문이 조회됨")
      and r["past_date_values"] == ["ord_dt=20260923"])
r = analyze_orders("ka10076", [[{"ord_no": "1", "tm": "100000"}]], "END", datetime(2026, 9, 28, 11, 0), cal)
check("4-4) 장중 실행 + 날짜 필드 없음 → 판정 불가 안내", "장 시작 전에 재실행" in r["verdict"])

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
