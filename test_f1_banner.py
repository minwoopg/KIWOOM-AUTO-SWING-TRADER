# -*- coding: utf-8 -*-
"""F1 (10/27): 시작 화면·시작 알림 전략 문구가 설정(swing_strategy.mode)을 그대로 보여줌.

    python test_f1_banner.py
"""
from __future__ import annotations

import sys
from types import SimpleNamespace as NS

from app.main import strategy_banner

results = []


def check(name, ok):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name)


paper = NS(swing_strategy=NS(mode="paper", allowed_symbols=["005930"], quantity=1))
b = strategy_banner(paper)
check("1) paper: S1 모의주문·허용 종목·수량 표시, NullStrategy 문구 없음",
      "S1 모의주문" in b and "005930" in b and "1주" in b and "NullStrategy" not in b)
check("2) shadow: 주문 안 냄 표시", "shadow" in strategy_banner(NS(swing_strategy=NS(mode="shadow"))))
check("3) off·설정 없음: 전략 없음(주문 안 냄)",
      "off" in strategy_banner(NS(swing_strategy=NS(mode="off"))) and "off" in strategy_banner(NS()))
check("4) --check-only는 모드와 무관하게 기반 점검 모드", strategy_banner(paper, check_only=True) == "기반 점검 모드")
src = open("app/main.py", encoding="utf-8").read()
check("5) 고정 문구 '(전략: NullStrategy)'·'(전략 없음)' 제거", "(전략: NullStrategy)" not in src and "시작 (전략 없음)" not in src)

passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
