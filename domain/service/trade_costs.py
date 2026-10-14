from __future__ import annotations

"""체결 사건 비용 — 실제(체결 근거) 우선, 없으면 추정. 보고서·관리 창·일일 손실 한도가 같은 함수를 씀 (C3).

- 실제: 그 주문의 체결 근거(ka10076 `tdy_trde_cmsn`·`tdy_trde_tax`)가 둘 다 있으면 그 합. 모델 비용을 더하지 않음(이중 차감 없음).
- 추정: 근거가 없거나 값이 비면 `CostProfile`(모의 계좌 실측 10/13 기준 수수료 0.35%·매도세 0.20%)로 **올림** 계산 —
  손실 한도 판단이 비용을 작게 보지 않게.
  10/13 실측(229,000원 1주): 매수 수수료 800, 매도 수수료 800 + 세금 457 → 추정은 802 / 802 + 458.
- 한 사건이 여러 매칭(부분 매도 등)에 나뉘면 `allocate`(최대 나머지 방식)로 정수 배분 — 나눈 합 = 원래 비용.

수익률 연구용 Gross/Base/Stress 시나리오(`domain/cost_model.py`)와는 별개 — 그쪽은 백테스트 민감도 비교용.
"""

import math
from dataclasses import dataclass
from typing import Iterable, Mapping

# 체결 근거가 없을 때의 사건 비용 추정 비율 — 단일 출처(설정 기본값도 여기서 가져감). 키움 모의 계좌 실측(2026-10-13,
# 229,000원 1주: 수수료 800·매도세 457). 연구용 왕복 시나리오(domain/cost_model.py)와는 별개.
PAPER_COMMISSION_RATE = 0.0035
PAPER_SELL_TAX_RATE = 0.0020

ACTUAL, ESTIMATE = "ACTUAL", "ESTIMATE"


@dataclass(frozen=True)
class CostProfile:
    commission_rate: float = PAPER_COMMISSION_RATE
    sell_tax_rate: float = PAPER_SELL_TAX_RATE
    label: str = ""          # 비우면 실제 적용 비율로 만듦(C4 — 설정을 바꾸면 표시도 따라감)

    VERSION = "ops-est-v1"   # 운영 추정 모델(연구용 Gross/Base/Stress와 별개)

    def __post_init__(self) -> None:
        for name in ("commission_rate", "sell_tax_rate"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0 <= v < 0.05):
                raise ValueError(f"{name}={v!r} — 0 이상 0.05 미만")
        if not self.label:
            base = " (모의 실측 10/13 기준값)" if (self.commission_rate, self.sell_tax_rate) == (
                PAPER_COMMISSION_RATE, PAPER_SELL_TAX_RATE) else " (설정값)"
            object.__setattr__(self, "label", f"{self.VERSION}: 수수료 {self.commission_rate * 100:.3g}%·매도세 "
                                              f"{self.sell_tax_rate * 100:.3g}%, 원 단위 올림{base}")


@dataclass(frozen=True)
class EventCost:
    amount: int
    source: str            # ACTUAL / ESTIMATE
    commission: int
    tax: int


def estimate_cost(kind: str, quantity: int, price: int, profile: CostProfile) -> EventCost:
    notional = int(quantity) * int(price)
    commission = math.ceil(notional * profile.commission_rate)
    tax = math.ceil(notional * profile.sell_tax_rate) if kind == "SELL" else 0
    return EventCost(commission + tax, ESTIMATE, commission, tax)


def event_cost(event, actual: tuple | None, profile: CostProfile) -> EventCost:
    """event: FillEvent(BUY/SELL). actual: (commission, tax) 또는 None. OPENING 등 거래가 아닌 사건은 0."""
    if event.kind not in ("BUY", "SELL"):
        return EventCost(0, ACTUAL, 0, 0)
    if actual is not None and len(actual) == 2 and all(type(v) is int and v >= 0 for v in actual):
        return EventCost(actual[0] + actual[1], ACTUAL, actual[0], actual[1])
    return estimate_cost(event.kind, event.quantity, event.price, profile)


def event_costs(events: Iterable, actual_costs: Mapping[str, tuple], profile: CostProfile) -> dict[str, EventCost]:
    return {e.event_id: event_cost(e, actual_costs.get(e.event_id), profile) for e in events}


def allocate(total: int, weights: list[int]) -> list[int]:
    """정수 total을 weights 비율로 나눔 — 합이 정확히 total (최대 나머지, 같으면 앞쪽)."""
    s = sum(weights)
    if s <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    base = [math.floor(r) for r in raw]
    left = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] - base[i]), i))
    for i in order[:left]:
        base[i] += 1
    return base


def match_costs(realized: list, costs: Mapping[str, EventCost], event_qty: Mapping[str, int]) -> list[tuple]:
    """매칭(RealizedMatch)마다 (매수 몫, 매도 몫, 출처). 출처: ACTUAL(둘 다 실제)·ESTIMATE(하나라도 추정)·None(사건 비용 없음).

    한 사건의 비용은 그 사건 수량 전체에 배분 — 아직 매도되지 않은 매수 잔량 몫은 매칭에 들어가지 않음. 배분 합 = 사건 비용."""
    parts: dict[str, list[tuple[int, int]]] = {}     # event_id → [(match index, qty)]
    for i, m in enumerate(realized):
        parts.setdefault(m.buy_event_id, []).append((i, m.quantity))
        parts.setdefault(m.sell_event_id, []).append((i, m.quantity))
    share: dict[tuple[str, int], int] = {}
    for eid, lst in parts.items():
        c = costs.get(eid)
        qty = event_qty.get(eid)
        if c is None or not qty:
            continue
        used = sum(q for _, q in lst)
        weights = [q for _, q in lst] + ([qty - used] if qty > used else [])
        alloc = allocate(c.amount, weights)
        for (i, _), a in zip(lst, alloc):
            share[(eid, i)] = a
    out = []
    for i, m in enumerate(realized):
        b, s = share.get((m.buy_event_id, i)), share.get((m.sell_event_id, i))
        if b is None or s is None:
            out.append((b, s, None))
            continue
        src = ACTUAL if costs[m.buy_event_id].source == ACTUAL and costs[m.sell_event_id].source == ACTUAL else ESTIMATE
        out.append((b, s, src))
    return out
