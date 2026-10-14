from __future__ import annotations

"""일일 손익 (C3 — GPT 최소판 일일 손실 한도). 입출금과 무관하게 **거래로 생긴 손익만** 셈.

    종목별 손익 = 지금 보유 × 현재가 + Σ 오늘 매도 금액 − 시작 보유 × 시작 기준가 − Σ 오늘 매수 금액
    일일 손익   = Σ 종목별 손익 − Σ 오늘 체결 사건 비용(실제 우선·없으면 추정, `trade_costs`)

- 시작 보유·기준가 = 그날 첫 폴링의 원장 보유와 그때 현재가(DAY_START). 현금 잔고는 쓰지 않음 → 입금·출금이 손익으로 잡히지 않음.
- 체결가는 체결 근거가 있으면 실제 체결가(원장 추정가는 겹쳐서 바꿈 — 호출부가 overlay된 사건을 넘김).
- 보유가 있는데 현재가가 없으면 None(알 수 없음) — 호출부는 신규 매수를 보류(손실 한도를 모르는 채 사지 않음).
- OPENING(기존 보유 인수)은 그 가격으로 산 것처럼(비용 0).
"""

from dataclasses import dataclass, field


@dataclass
class DayPnl:
    pnl: int | None
    by_symbol: dict = field(default_factory=dict)
    costs: int = 0
    estimated_costs: int = 0
    missing_prices: list = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {"pnl": self.pnl, "by_symbol": self.by_symbol, "costs": self.costs,
                "estimated_costs": self.estimated_costs, "missing_prices": self.missing_prices, "note": self.note}


def day_pnl(start: dict, events_today: list, qty_now: dict[str, int], prices_now: dict[str, int],
            costs: dict) -> DayPnl:
    """start: DAY_START 기록(qty·marks). events_today: 오늘 체결 사건(overlay 후). costs: event_id → EventCost."""
    start_qty = {s: int(q) for s, q in (start.get("qty") or {}).items()}
    marks = {s: int(p) for s, p in (start.get("marks") or {}).items()}
    syms = set(start_qty) | set(qty_now) | {e.symbol for e in events_today}
    out = DayPnl(pnl=0)
    total = 0
    for s in sorted(syms):
        buy = sum(e.quantity * e.price for e in events_today if e.symbol == s and e.kind in ("BUY", "OPENING"))
        sell = sum(e.quantity * e.price for e in events_today if e.symbol == s and e.kind == "SELL")
        q0, q1 = start_qty.get(s, 0), int(qty_now.get(s, 0))
        if q0 and s not in marks:
            out.missing_prices.append(s)
            continue
        if q1:
            px = prices_now.get(s)
            if type(px) is not int or px <= 0:
                out.missing_prices.append(s)
                continue
        else:
            px = 0
        v = q1 * px + sell - q0 * marks.get(s, 0) - buy
        out.by_symbol[s] = v
        total += v
    for e in events_today:
        c = costs.get(e.event_id)
        if c is not None:
            out.costs += c.amount
            if c.source != "ACTUAL":
                out.estimated_costs += c.amount
    if out.missing_prices:
        out.pnl = None
        out.note = f"현재가·기준가 없음: {', '.join(out.missing_prices)}"
        return out
    out.pnl = total - out.costs
    return out
