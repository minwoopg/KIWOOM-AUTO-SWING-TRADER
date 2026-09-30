# -*- coding: utf-8 -*-
"""A1: tools/probe_research_sources.py 회귀 테스트 (네트워크 없음, 가짜 세션).

응답 형식은 가짜입니다 — 실제 필드 이름·단위는 프로브 실행 결과로 확인합니다.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, ".")

from tools.probe_market_data import ProbeConfigError
from tools.probe_research_sources import analyze_daily_fields, field_distributions, main

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


class _Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, index_error=False):
        self.calls = []
        self.index_error = index_error

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, dict(headers or {}), json))
        if url.endswith("/oauth2/token"):
            return _Resp(200, {"return_code": 0, "token": "SECRET_TOKEN_VALUE"})
        api = headers["api-id"]
        if api == "ka10099":
            rows = [{"code": "005930", "name": "삼성전자", "marketCode": json["mrkt_tp"], "orderWarning": "0"},
                    {"code": "005935", "name": "삼성전자우", "marketCode": json["mrkt_tp"], "orderWarning": "0"},
                    {"code": "0000J0", "name": "ETN", "marketCode": json["mrkt_tp"], "orderWarning": "1"}]
            return _Resp(200, {"return_code": 0, "list": rows}, {"cont-yn": "N"})
        if api == "ka10081":
            first = headers["cont-yn"] == "N"
            rows = [{"dt": "20260929" if first else "20190102", "cur_prc": "+70000", "trde_qty": "1000",
                     "trde_prica": "70", "acnt_no": "1234567890"}]
            return _Resp(200, {"return_code": 0, "stk_dt_pole_chart_qry": rows},
                         {"cont-yn": "Y" if first else "N", "next-key": "k1" if first else ""})
        if api == "ka20006":
            if self.index_error:
                return _Resp(200, {"return_code": 1, "return_msg": "필수 입력값 누락"})
            return _Resp(200, {"return_code": 0, "inds_dt_pole_qry": [
                {"dt": "20260929", "cur_prc": "258412", "open_pric": "257000"},
                {"dt": "20260926", "cur_prc": "256001", "open_pric": "255500"}]}, {"cont-yn": "N"})
        raise AssertionError(f"unexpected api {api}")


def run(session, extra=()):
    tmp = Path(tempfile.mkdtemp())
    env = tmp / ".env"
    env.write_text("KIWOOM_APP_KEY=AK_SECRET\nKIWOOM_SECRET_KEY=SK_SECRET\n", encoding="utf-8")
    main(["--env-file", str(env), "--out-dir", str(tmp), "--sleep", "0", *extra],
         session=session, now=datetime(2026, 9, 30, 16, 0))
    summary = next(tmp.glob("*_summary.txt")).read_text(encoding="utf-8")
    raw = next(tmp.glob("*.jsonl")).read_text(encoding="utf-8")
    return json.loads(summary.split("\n", 1)[1]), raw


s = FakeSession()
data, raw = run(s)
kospi = data["stock_list"][0]
check("1-1) 종목 목록: 목록 키 자동 인식·행 수·필드", kospi["list_key"] == "list" and kospi["rows"] == 3
      and "orderWarning" in kospi["fields"])
check("1-2) 값 종류 적은 필드 분포 기록(경고·시장 후보)", "orderWarning" in kospi["low_cardinality_fields"])
check("1-3) 종목코드 형태: 0으로 안 끝나는 코드 예시(우선주 후보), 숫자 아닌 코드",
      kospi["code_shapes"]["examples_not_ending_0"] == ["005935"] and kospi["code_shapes"]["non_digit"] == 1)
check("1-4) KOSPI·KOSDAQ 요청 코드(0·10)", [c[2]["mrkt_tp"] for c in s.calls if c[1].get("api-id") == "ka10099"]
      == ["0", "10"])
d = data["daily_fields"]
check("2-1) 일봉 연속조회로 가장 오래된 날짜 확인", d["oldest"] == "2019-01-02" and d["page_sizes"] == [1, 1])
check("2-2) 거래대금 후보 필드와 단위 추정(70 ÷ (70000×1000) = 1e-6 → 백만원)",
      d["trade_value_candidates"]["trde_prica"]["unit_guess"] == "백만원 단위로 보임")
ix = data["index"][0]
check("3-1) 지수: 목록 키·값 원문·소수점 없음 표시(배율 확인용)", ix["list_key"] == "inds_dt_pole_qry"
      and ix["price_field_raw_values"]["cur_prc"][0] == "258412" and ix["has_decimal_point"] is False)
check("3-2) 지수 요청: 코드 001/101", [c[2]["inds_cd"] for c in s.calls if c[1].get("api-id") == "ka20006"]
      == ["001", "101"])
check("4-1) 원시 기록에 토큰·앱키·계좌번호 원문 없음",
      "SECRET_TOKEN_VALUE" not in raw and "AK_SECRET" not in raw and "1234567890" not in raw)
check("4-2) 주문 API 호출 없음", all(c[1].get("api-id", "") in ("", "ka10099", "ka10081", "ka20006")
                                  for c in s.calls))
data, _ = run(FakeSession(index_error=True))
check("4-3) TR 오류 응답도 결과로 기록(중단 없음)", data["index"][0]["stop_reason"] == "RETURN_CODE_1"
      and data["index"][0]["error"]["return_msg"] == "필수 입력값 누락")
try:
    main(["--base-url", "https://api.kiwoom.com", "--env-file", "x"], session=FakeSession())
    blocked = False
except ProbeConfigError:
    blocked = True
check("4-4) 실전 도메인이면 네트워크 호출 전에 중단", blocked)
check("5-1) 단위 추정: 원 단위(비율 1)", analyze_daily_fields("x", [[{"dt": "20260929", "cur_prc": "100", "trde_qty": "10",
                                                              "trde_prica": "1000"}]], "END", None)
      ["trade_value_candidates"]["trde_prica"]["unit_guess"] == "원 단위로 보임")
check("5-2) 값 종류 많은 필드는 분포 생략", "v" not in field_distributions([{"v": str(i)} for i in range(100)]))

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
