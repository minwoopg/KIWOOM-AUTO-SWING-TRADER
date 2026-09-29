# -*- coding: utf-8 -*-
"""현재가 조회 회귀 테스트 (8-B, F3)."""
from __future__ import annotations

import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from infra.market_data.quote_source import KiwoomQuoteSource, StaticQuoteSource

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


class FakeBroker:
    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = []

    def _post(self, *, endpoint, api_id, payload, cont_yn, next_key):
        self.calls.append((endpoint, api_id, payload["stk_cd"]))
        b = self.bodies[payload["stk_cd"]]
        if isinstance(b, Exception):
            raise b
        return SimpleNamespace(body=b, headers={})


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


fb = FakeBroker({"005930": {"cur_prc": "-70,500"}, "000660": {"cur_prc": "+180000"},
                 "035420": RuntimeError("http=500"), "051910": {"cur_prc": ""}})
clk = Clock()
warn = []
qs = KiwoomQuoteSource(fb, clock=clk, sleep=clk.sleep, logger=SimpleNamespace(warning=warn.append))
out = qs.get_prices(["005930", "000660", "035420", "051910", "005930"])
check("1-1) 부호 붙은 값도 절대값으로", out.get("005930") == 70_500 and out.get("000660") == 180_000)
check("1-2) 조회 실패·빈 값은 결과에서 빠짐(0으로 채우지 않음)", "035420" not in out and "051910" not in out)
check("1-3) 실패는 경고 로그", len(warn) == 2)
check("1-4) 종목당 1회, ka10001만 호출", qs.calls == 4 and {c[1] for c in fb.calls} == {"ka10001"})
check("1-5) 호출 간격 1초 유지", clk.slept == [1.0, 1.0, 1.0])
sq = StaticQuoteSource({"A": 1, "B": 0})
check("2-1) StaticQuoteSource: 0 이하 가격은 모름으로", sq.get_prices(["A", "B", "C"]) == {"A": 1})

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
