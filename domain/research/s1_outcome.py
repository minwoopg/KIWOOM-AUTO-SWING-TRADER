from __future__ import annotations

"""S1 신호 결과 측정 — 일봉 근사 (E3, GPT 10/22). **운영 체결 재현이 아님.**

운영(실행기)과 다른 점 — 보고서에 그대로 표시:
- 진입: 운영은 09:05~09:30 현재가·최우선 매도호가를 보고 상한 호가 내림 지정가로 주문, 09:30 미체결 취소.
  여기서는 **시가 진입 가정**: 신호 다음 거래일 시가 O가 지정가(상한 호가 내림) 이하이고 손절 기준보다 크면 O(+진입 슬리피지,
  지정가를 넘지 않게)에 산 것으로 봄. O가 지정가보다 높으면 **장중 저가가 내려왔어도 체결로 보지 않음**(NOT_ENTERED_ABOVE_CAP).
- 손절: 운영은 60초마다 현재가 ≤ 손절가면 시장가. 여기서는
  · 둘째 날부터 시가 ≤ 손절가 → **갭 손절**: 시가 × (1 − 청산 슬리피지) (손절가에 팔렸다고 보지 않음)
  · 장중 저가 ≤ 손절가 → 손절가 × (1 − 청산 슬리피지)
  · 진입일 당일 저가 ≤ 손절가 → 진입(시가)과 손절의 선후를 일봉으로 알 수 없음 → **불확실 표본**으로 표시하고
    보수적으로 그날 손절된 것으로 처리.
- 시간 청산: 운영은 진입일 = 0일, 10번째 거래일 마감 20분 전(15:10) 시장가. 여기서는 그날 **종가 × (1 − 슬리피지)로 근사**.
  그날 손절 조건도 맞으면 손절을 먼저(보수적). 그날 봉이 없으면(거래정지) 다음 봉이 있는 날로 미룸.
- 거래일 = 분석 도구가 주는 거래일 목록(KOSPI 지수 일봉 날짜). 보유 중 그 종목 봉이 없는 날은 거래 불가로 건너뜀(missing_days).
- 데이터 끝까지 청산되지 않으면 OPEN_AT_END(마지막 종가 평가, 손익 통계에서 따로).
- 비용: 호출부가 넘기는 비용 함수(도구는 `domain/service/trade_costs` 추정 — 수수료·매도세 비율, 원 단위 올림. 연구 계층은
  서비스 계층을 import하지 않음) — 수량 1주, 가격은 원 단위로 반올림해 계산.
  비용 차감 전·후를 함께 냄. 수정주가 기준이라 실제 원 단위 비용과 다를 수 있음.
"""

import math
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Callable, Iterable

from domain.market_data.tick_size import floor_to_tick

ENTERED = "ENTERED"
NO_NEXT_DAY = "NO_NEXT_DAY"                    # 신호일이 데이터 끝 — 진입일 없음
ENTRY_NO_DATA = "ENTRY_NO_DATA"                # 진입일 봉 없음·거래 없음(거래정지 등)
NOT_ENTERED_ABOVE_CAP = "NOT_ENTERED_ABOVE_CAP"
NOT_ENTERED_AT_OR_BELOW_STOP = "NOT_ENTERED_AT_OR_BELOW_STOP"
INVALID_LEVELS = "INVALID_LEVELS"              # 상한·손절 기준 없음 또는 지정가 ≤ 손절가
STOP, STOP_GAP, TIME, OPEN_AT_END = "STOP", "STOP_GAP", "TIME", "OPEN_AT_END"
ENTRY_LABEL = {NO_NEXT_DAY: "진입일 없음(데이터 끝)", ENTRY_NO_DATA: "진입일 봉 없음·거래정지",
               NOT_ENTERED_ABOVE_CAP: "시가가 진입 상한(지정가) 초과", NOT_ENTERED_AT_OR_BELOW_STOP: "시가가 손절 기준 이하",
               INVALID_LEVELS: "상한·손절 기준 없음/역전", ENTERED: "진입 가정 성립"}
EXIT_LABEL = {STOP: "장중 손절", STOP_GAP: "갭 손절(시가)", TIME: "10거래일 시간 청산(종가 근사)",
              OPEN_AT_END: "분석 종료까지 미청산"}


@dataclass(frozen=True)
class OutcomeParams:
    time_exit_sessions: int = 10
    entry_slip_bps: float = 0.0       # 지정가 매수 — 시가에 더하되 지정가를 넘지 않음
    exit_slip_bps: float = 10.0       # 시장가 매도(손절·갭·시간) 불리한 쪽
    quantity: int = 1
    cost_fn: Callable[[str, int, int], int] | None = None    # (BUY/SELL, 수량, 원 단위 가격) → 비용(원). None이면 0
    cost_label: str = "비용 0(비용 함수 없음)"


def _won(x: float) -> int:
    return int(math.floor(x + 0.5))


def simulate(sig: dict, days: list[date], bars: dict[date, object], p: OutcomeParams) -> dict:
    """sig: {"date": "YYYY-MM-DD", "code", "stop_ref", "entry_cap"}. days: 거래일 목록(오름차순, 신호일 이후 포함).
    bars: 그 종목 date → ResearchBar. 반환: 진입 상태·청산 상태·가격·손익."""
    t = date.fromisoformat(sig["date"])
    out = {"date": sig["date"], "code": sig["code"], "status": None, "exit": None, "same_day_uncertain": False,
           "missing_days": 0}
    sr, cap = sig.get("stop_ref"), sig.get("entry_cap")
    if sr is None or cap is None:
        out["status"] = INVALID_LEVELS
        return out
    limit, stop = floor_to_tick(cap), int(math.floor(sr))
    out.update({"limit": limit, "stop": stop})
    if stop <= 0 or limit <= stop:
        out["status"] = INVALID_LEVELS
        return out
    after = [d for d in days if d > t]
    if not after:
        out["status"] = NO_NEXT_DAY
        return out
    d1 = after[0]
    b1 = bars.get(d1)
    if b1 is None or not b1.volume:
        out.update({"status": ENTRY_NO_DATA, "entry_date": d1.isoformat()})
        return out
    o = b1.open
    if o > limit:
        out.update({"status": NOT_ENTERED_ABOVE_CAP, "entry_date": d1.isoformat(), "open": o})
        return out
    if o <= stop:
        out.update({"status": NOT_ENTERED_AT_OR_BELOW_STOP, "entry_date": d1.isoformat(), "open": o})
        return out
    entry = min(o * (1 + p.entry_slip_bps / 10_000), limit)
    out.update({"status": ENTERED, "entry_date": d1.isoformat(), "entry": entry})
    slip = 1 - p.exit_slip_bps / 10_000
    exit_px = exit_day = None
    for i, d in enumerate(after):
        b = bars.get(d)
        if b is None or not b.volume:
            out["missing_days"] += 1
            continue
        if i == 0:
            if b.low <= stop:
                exit_px, exit_day, out["exit"], out["same_day_uncertain"] = stop * slip, d, STOP, True
                break
        else:
            if b.open <= stop:
                exit_px, exit_day, out["exit"] = b.open * slip, d, STOP_GAP
                break
            if b.low <= stop:
                exit_px, exit_day, out["exit"] = stop * slip, d, STOP
                break
        if i >= p.time_exit_sessions:
            exit_px, exit_day, out["exit"] = b.close * slip, d, TIME
            break
    if exit_px is None:
        last = next((d for d in reversed(after) if bars.get(d) is not None and bars[d].volume), d1)
        out.update({"exit": OPEN_AT_END, "exit_date": last.isoformat(), "mark": bars[last].close})
        out.update(_pnl(entry, bars[last].close, p, realized=False))
        return out
    out.update({"exit_date": exit_day.isoformat(), "exit_price": exit_px,
                "hold_sessions": after.index(exit_day)})
    out.update(_pnl(entry, exit_px, p, realized=True))
    return out


def _pnl(entry: float, exit_: float, p: OutcomeParams, *, realized: bool) -> dict:
    q = p.quantity
    gross = (exit_ - entry) * q
    cf = p.cost_fn or (lambda kind, qty, price: 0)
    buy = cf("BUY", q, _won(entry))
    sell = cf("SELL", q, _won(exit_)) if realized else 0
    net = gross - buy - sell
    return {"gross": gross, "cost": buy + sell, "net": net, "gross_pct": gross / (entry * q) * 100,
            "net_pct": net / (entry * q) * 100}


def sequential(signals: list[dict], results: dict[tuple, dict]) -> list[dict]:
    """최대 1포지션 계좌(운영 규칙 근사): 진입일에 보유가 없을 때만(이전 포지션 청산일 < 진입일), 같은 진입일 후보는
    종목코드 순(운영 S1EntryStrategy가 승인 후보를 코드순으로 보고 가격 조건을 처음 통과한 것을 고름)으로 진입 가정이
    성립하는 첫 신호. 신호마다 한 번만(같은 신호 재진입 없음), 같은 종목의 다음 날 신호는 별개 신호."""
    by_entry: dict[str, list[dict]] = {}
    for s in signals:
        r = results[(s["date"], s["code"])]
        if r.get("entry_date"):
            by_entry.setdefault(r["entry_date"], []).append(r)
    trades, busy_until = [], None
    for d in sorted(by_entry):
        if busy_until is not None and d <= busy_until:
            continue
        cands = sorted(by_entry[d], key=lambda r: r["code"])
        pick = next((r for r in cands if r["status"] == ENTERED), None)
        if pick is None:
            continue
        trades.append(pick)
        if pick["exit"] == OPEN_AT_END:
            break
        busy_until = pick["exit_date"]
    return trades


def metrics(trades: Iterable[dict]) -> dict:
    closed = [t for t in trades if t.get("exit") not in (None, OPEN_AT_END)]
    if not closed:
        return {"n": 0}
    net = [t["net_pct"] for t in closed]
    gross = [t["gross_pct"] for t in closed]
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    streak = best = 0
    for x in net:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    cum = peak = mdd = 0.0
    cum_w = peak_w = mdd_w = 0.0
    for t in closed:
        cum += t["net_pct"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
        cum_w += t["net"]
        peak_w = max(peak_w, cum_w)
        mdd_w = min(mdd_w, cum_w - peak_w)
    return {"n": len(closed), "gross_mean": sum(gross) / len(gross), "net_mean": sum(net) / len(net),
            "net_median": median(net), "win_rate": len(wins) / len(net) * 100,
            "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
            "max_consec_losses": best, "sum_net_pct": cum, "mdd_pct_points": mdd, "sum_net_won_1sh": cum_w,
            "mdd_won_1sh": mdd_w,
            "same_day_uncertain": sum(1 for t in closed if t.get("same_day_uncertain")),
            "by_exit": {k: sum(1 for t in closed if t["exit"] == k) for k in (STOP, STOP_GAP, TIME)}}
