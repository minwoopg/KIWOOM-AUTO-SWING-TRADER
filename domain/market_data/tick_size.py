from __future__ import annotations

"""KRX 호가 단위 (유가·코스닥 공통, 2023-01-25 개편 기준) — 지정가를 유효 호가로 맞출 때 씀.

| 가격 구간(원) | 호가 단위(원) |
|---|---|
| ~ 2,000 미만 | 1 |
| 2,000 ~ 5,000 미만 | 5 |
| 5,000 ~ 20,000 미만 | 10 |
| 20,000 ~ 50,000 미만 | 50 |
| 50,000 ~ 200,000 미만 | 100 |
| 200,000 ~ 500,000 미만 | 500 |
| 500,000 이상 | 1,000 |

ETF·ETN 등은 다른 규칙이 있음 — 이 모듈은 보통주에만 씀(S1 대상은 보통주). 순수 함수.
"""

_TABLE = ((2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500))


def krx_tick(price: int) -> int:
    if type(price) is not int or price <= 0:
        raise ValueError(f"가격은 양의 정수: {price!r}")
    for upper, tick in _TABLE:
        if price < upper:
            return tick
    return 1_000


def floor_to_tick(price: float | int) -> int:
    """price 이하의 가장 큰 유효 호가. 구간 경계에서 아래 구간 단위로 내려가도 price를 넘지 않음."""
    p = int(price)
    if p <= 0:
        raise ValueError(f"가격은 양수: {price!r}")
    t = krx_tick(p)
    return p - (p % t)


def is_valid_tick(price: int) -> bool:
    return type(price) is int and price > 0 and price % krx_tick(price) == 0
