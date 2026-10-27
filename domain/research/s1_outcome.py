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
- 거래일 = 분석 도구가 주는 거래일 목록(KOSPI 지수 일봉 날짜).
- 봉이 없거나 거래 없는 날 (G2 — GPT 10/27: 이전에는 둘 다 "거래 불가"로 건너뛰고 확정 손익에 넣었음):
  · **거래량 0 봉(NO_TRADES)**: 거래가 없었던 날 — 체결·손절 불가로 건너뜀(no_trades_days). 거래정지 확정은 아님(과거 정지
    이력 원천 없음). 확정 손익에 포함하되 건수를 표시.
  · **봉 자체가 없는 세션(중간 공백)**: 그 종목 첫 봉~마지막 봉 사이인데 봉이 없음 — 수집 누락일 수 있어 **그날 손절이
    났는지 알 수 없음**. 이후 가격으로 계산은 이어 가되(순차 계좌의 보유 기간 근사용) quality=DATA_GAP_UNCERTAIN으로 표시하고
    **확정 손익 통계에서 제외**(gap_days에 날짜).
  · 그 종목 마지막 봉 이후 세션은 공백이 아니라 데이터 끝 → 그때까지 청산 안 되면 OPEN_AT_END.
  · 진입일: 봉 없음 = ENTRY_DATA_GAP, 거래량 0 = ENTRY_NO_TRADES, 그 종목 데이터가 신호일에서 끝남 = NO_NEXT_DAY.
- 데이터 끝까지 청산되지 않으면 OPEN_AT_END(마지막 종가 평가, 손익 통계에서 따로).
- 진입 방식(E4 — 사용자 10/27): entry_mode="close"는 신호일 종가 진입 근사(마감 직전 매수) — `_simulate_close`.
- 손절 거리(G4): 진입 가정가 기준 (진입 − 손절 기준)/진입, 상한(지정가) 기준 (지정가 − 손절 기준)/지정가를 따로 기록.
  예상 손실액 = (진입 − 손절 기준) × 수량 + 예상 비용(매수 + 손절가 매도). 운영 S1의 risk_ratio_at_cap은 상한 기준.
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
ENTRY_DATA_GAP = "ENTRY_DATA_GAP"              # 진입일 봉 없음(그 종목 데이터 중간 공백 — 원인 불명)
ENTRY_NO_TRADES = "ENTRY_NO_TRADES"            # 진입일 거래량 0 봉(거래 없음)
NOT_ENTERED_ABOVE_CAP = "NOT_ENTERED_ABOVE_CAP"
NOT_ENTERED_AT_OR_BELOW_STOP = "NOT_ENTERED_AT_OR_BELOW_STOP"
INVALID_LEVELS = "INVALID_LEVELS"              # 상한·손절 기준 없음 또는 지정가 ≤ 손절가
STOP, STOP_GAP, TIME, OPEN_AT_END = "STOP", "STOP_GAP", "TIME", "OPEN_AT_END"
ENTRY_LABEL = {NO_NEXT_DAY: "진입일 없음(데이터 끝)", ENTRY_DATA_GAP: "진입일 봉 없음(중간 공백·원인 불명)",
               ENTRY_NO_TRADES: "진입일 거래 없는 봉(거래량 0)",
               NOT_ENTERED_ABOVE_CAP: "시가가 진입 상한(지정가) 초과", NOT_ENTERED_AT_OR_BELOW_STOP: "시가가 손절 기준 이하",
               INVALID_LEVELS: "상한·손절 기준 없음/역전", ENTERED: "진입 가정 성립"}
EXIT_LABEL = {STOP: "장중 손절", STOP_GAP: "갭 손절(시가)", TIME: "10거래일 시간 청산(종가 근사)",
              OPEN_AT_END: "분석 종료까지 미청산"}
QUALITY_OK, DATA_GAP_UNCERTAIN = "OK", "DATA_GAP_UNCERTAIN"


@dataclass(frozen=True)
class OutcomeParams:
    time_exit_sessions: int = 10
    entry_slip_bps: float = 0.0       # 지정가 매수 — 시가에 더하되 지정가를 넘지 않음
    exit_slip_bps: float = 10.0       # 시장가 매도(손절·갭·시간) 불리한 쪽
    quantity: int = 1
    cost_fn: Callable[[str, int, int], int] | None = None    # (BUY/SELL, 수량, 원 단위 가격) → 비용(원). None이면 0
    cost_label: str = "비용 0(비용 함수 없음)"
    entry_mode: str = "open"          # E4: open = 다음 거래일 시가(현재 운영) / close = 신호일 종가(마감 직전 진입 근사)
    close_entry_slip_bps: float = 10.0  # E4: 종가 진입은 마감 직전 매수 — 종가보다 불리한 쪽으로(상한 제한 없음)


def _won(x: float) -> int:
    return int(math.floor(x + 0.5))


def simulate(sig: dict, days: list[date], bars: dict[date, object], p: OutcomeParams) -> dict:
    """sig: {"date": "YYYY-MM-DD", "code", "stop_ref", "entry_cap"}. days: 거래일 목록(오름차순, 신호일 이후 포함).
    bars: 그 종목 date → ResearchBar. 반환: 진입 상태·청산 상태·가격·손익·데이터 품질·손절 거리."""
    t = date.fromisoformat(sig["date"])
    out = {"date": sig["date"], "code": sig["code"], "status": None, "exit": None, "same_day_uncertain": False,
           "gap_days": [], "no_trades_days": 0, "quality": QUALITY_OK}
    sr, cap = sig.get("stop_ref"), sig.get("entry_cap")
    if sr is None or cap is None:
        out["status"] = INVALID_LEVELS
        return out
    limit, stop = floor_to_tick(cap), int(math.floor(sr))
    out.update({"limit": limit, "stop": stop})
    if stop <= 0 or limit <= stop:
        out["status"] = INVALID_LEVELS
        return out
    out["stop_dist_cap_pct"] = (limit - stop) / limit * 100
    if p.entry_mode == "close":
        return _simulate_close(sig, t, days, bars, p, out, stop)
    last_bar = max(bars) if bars else None
    after = [d for d in days if d > t and last_bar is not None and d <= last_bar]   # 마지막 봉 이후 = 데이터 끝
    if not after:
        out["status"] = NO_NEXT_DAY
        return out
    d1 = after[0]
    b1 = bars.get(d1)
    if b1 is None:
        out.update({"status": ENTRY_DATA_GAP, "entry_date": d1.isoformat()})
        return out
    if not b1.volume:
        out.update({"status": ENTRY_NO_TRADES, "entry_date": d1.isoformat()})
        return out
    o = b1.open
    if o > limit:
        out.update({"status": NOT_ENTERED_ABOVE_CAP, "entry_date": d1.isoformat(), "open": o})
        return out
    if o <= stop:
        out.update({"status": NOT_ENTERED_AT_OR_BELOW_STOP, "entry_date": d1.isoformat(), "open": o})
        return out
    entry = min(o * (1 + p.entry_slip_bps / 10_000), limit)
    out.update({"status": ENTERED, "entry_date": d1.isoformat(), "entry": entry, **_risk(entry, stop, p)})
    slip = 1 - p.exit_slip_bps / 10_000
    exit_px = exit_day = None
    for i, d in enumerate(after):
        b = bars.get(d)
        if b is None:                      # 중간 공백 — 그날 손절 여부를 알 수 없음
            out["gap_days"].append(d.isoformat())
            continue
        if not b.volume:                   # 거래 없는 날 — 체결·손절 불가
            out["no_trades_days"] += 1
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
    if out["gap_days"]:
        out["quality"] = DATA_GAP_UNCERTAIN
    if exit_px is None:
        last = next((d for d in reversed(after) if bars.get(d) is not None and bars[d].volume), d1)
        out.update({"exit": OPEN_AT_END, "exit_date": last.isoformat(), "mark": bars[last].close})
        out.update(_pnl(entry, bars[last].close, p, realized=False))
        return out
    out.update({"exit_date": exit_day.isoformat(), "exit_price": exit_px,
                "hold_sessions": after.index(exit_day)})
    out.update(_pnl(entry, exit_px, p, realized=True))
    return out


def _simulate_close(sig: dict, t: date, days: list[date], bars: dict, p: OutcomeParams, out: dict, stop: int) -> dict:
    """E4(사용자 10/27): 신호일 종가 진입 근사 — 마감 직전(15:10~15:20) 매수를 그날 종가 × (1 + 슬리피지)로.
    가정: 마감 직전 가격 ≈ 종가, 그 시점의 잠정 판정 = 종가 기준 최종 판정(실제로는 뒤집힐 수 있음 — 따로 측정 필요).
    진입일 = 신호일(0일), 다음 거래일부터 손절(갭·장중), 진입일 0일 기준 10번째 거래일 종가 시간 청산.
    진입 상한(지정가)은 적용하지 않음(다음 날 시가 상한 초과로 못 사는 문제를 없애는 방식이라) — 대신 종가 ≤ 손절 기준이면 미진입."""
    out["entry_mode"] = "close"
    b0 = bars.get(t)
    if b0 is None:
        out.update({"status": ENTRY_DATA_GAP, "entry_date": t.isoformat()})
        return out
    if not b0.volume:
        out.update({"status": ENTRY_NO_TRADES, "entry_date": t.isoformat()})
        return out
    if b0.close <= stop:
        out.update({"status": NOT_ENTERED_AT_OR_BELOW_STOP, "entry_date": t.isoformat(), "open": b0.close})
        return out
    entry = b0.close * (1 + p.close_entry_slip_bps / 10_000)
    out.update({"status": ENTERED, "entry_date": t.isoformat(), "entry": entry, **_risk(entry, stop, p)})
    last_bar = max(bars)
    after = [d for d in days if t < d <= last_bar]
    slip = 1 - p.exit_slip_bps / 10_000
    exit_px = exit_day = None
    for i, d in enumerate(after):
        b = bars.get(d)
        if b is None:
            out["gap_days"].append(d.isoformat())
            continue
        if not b.volume:
            out["no_trades_days"] += 1
            continue
        if b.open <= stop:
            exit_px, exit_day, out["exit"] = b.open * slip, d, STOP_GAP
            break
        if b.low <= stop:
            exit_px, exit_day, out["exit"] = stop * slip, d, STOP
            break
        if i + 1 >= p.time_exit_sessions:
            exit_px, exit_day, out["exit"] = b.close * slip, d, TIME
            break
    if out["gap_days"]:
        out["quality"] = DATA_GAP_UNCERTAIN
    if exit_px is None:
        last = next((d for d in reversed(after) if bars.get(d) is not None and bars[d].volume), t)
        out.update({"exit": OPEN_AT_END, "exit_date": last.isoformat(), "mark": bars[last].close})
        out.update(_pnl(entry, bars[last].close, p, realized=False))
        return out
    out.update({"exit_date": exit_day.isoformat(), "exit_price": exit_px, "hold_sessions": after.index(exit_day) + 1})
    out.update(_pnl(entry, exit_px, p, realized=True))
    return out


def paired(open_res: dict, close_res: dict) -> dict:
    """같은 신호를 두 진입 방식으로 — 둘 다 확정 표본인 신호의 차이, 시가 진입에서 상한 초과로 놓친 신호의 종가 진입 결과."""
    both = [(k, open_res[k], close_res[k]) for k in open_res
            if k in close_res and confirmed([open_res[k]]) and confirmed([close_res[k]])]
    diff = [c["net_pct"] - o["net_pct"] for _, o, c in both]
    missed = [close_res[k] for k, r in open_res.items() if r.get("status") == NOT_ENTERED_ABOVE_CAP and k in close_res]
    return {"n_both": len(both), "mean_diff": sum(diff) / len(diff) if diff else None,
            "median_diff": median(diff) if diff else None, "close_better": sum(1 for x in diff if x > 0),
            "missed": missed, "missed_metrics": metrics(missed)}


def _risk(entry: float, stop: int, p: OutcomeParams) -> dict:
    """진입 시 손절 거리(진입가 기준)·예상 손실액(수량 p.quantity, 손절가 매도 가정 + 예상 비용, 슬리피지 제외)."""
    cf = p.cost_fn or (lambda kind, qty, price: 0)
    q = p.quantity
    exp_loss = (entry - stop) * q + cf("BUY", q, _won(entry)) + cf("SELL", q, stop)
    return {"stop_dist_pct": (entry - stop) / entry * 100, "exp_loss": exp_loss,
            "exp_loss_pct": exp_loss / (entry * q) * 100}


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


def confirmed(trades: Iterable[dict]) -> list[dict]:
    """확정 손익 표본: 청산 완료이고 보유 중 중간 공백이 없는 거래(G2)."""
    return [t for t in trades if t.get("exit") not in (None, OPEN_AT_END) and t.get("quality", QUALITY_OK) == QUALITY_OK]


def gap_uncertain(trades: Iterable[dict]) -> list[dict]:
    return [t for t in trades if t.get("status") == ENTERED and t.get("quality") == DATA_GAP_UNCERTAIN]


def metrics(trades: Iterable[dict]) -> dict:
    """확정 손익 표본(confirmed)만으로 계산. 누적·낙폭은 **거래 수익률 단순 합산선** — 계좌 수익률·계좌 낙폭이 아님
    (계좌 기준은 account())."""
    closed = confirmed(trades)
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


def account(trades: list[dict], bars_by_code: dict[str, dict], days: list[date], capital: int,
            qty_fn: Callable[[int], int], cost_fn: Callable[[str, int, int], int] | None = None) -> dict:
    """계좌 기준 손익(G3 — GPT 10/27): 초기 자금·투입 금액·현금·보유 평가액·비용을 반영한 일별 평가 곡선.

    trades: sequential() 결과(최대 1포지션). qty_fn(진입가 원) → 수량(0이면 건너뜀). days: 평가할 거래일(오름차순).
    매수 = 진입가(원 반올림) × 수량 + 매수 비용, 매도 = 청산가(원 반올림) × 수량 − 매도 비용. 보유 중 평가 = 그날 종가
    (봉이 없거나 거래 없는 날은 직전 종가). 미청산은 마지막 날 평가액. 비용 포함 현금이 모자라면 수량을 줄이고, 1주도
    못 사거나 수량 0이면 그 거래는 건너뜀
    (순차 선택은 바꾸지 않음 — 건너뛴 거래의 보유 기간에도 다른 신호를 새로 고르지 않는 근사)."""
    cf = cost_fn or (lambda kind, qty, price: 0)
    by_day = {}
    for t in trades:
        by_day.setdefault(t["entry_date"], t)
    cash, pos = float(capital), None
    peak = float(capital)
    mdd_pct = mdd_won = 0.0
    max_exp = 0.0
    in_mkt = 0
    total_cost = 0
    done, skipped = [], []
    last_close: dict[str, float] = {}
    for d in days:
        ds = d.isoformat()
        t = by_day.get(ds)
        if pos is None and t is not None:
            px = _won(t["entry"])
            q = int(qty_fn(px))
            while q > 0 and px * q + cf("BUY", q, px) > cash:     # 비용 포함 현금 한도까지 수량 줄임
                q -= 1
            bc = cf("BUY", q, px) if q > 0 else 0
            if int(qty_fn(px)) <= 0:
                skipped.append((t, "수량 0(투입 금액 < 1주 가격)"))
            elif q <= 0:
                skipped.append((t, "현금 부족"))
            else:
                eq_before = cash
                cash -= px * q + bc
                total_cost += bc
                pos = {"t": t, "q": q, "px": px, "bc": bc}
                max_exp = max(max_exp, px * q / eq_before * 100)
        b = bars_by_code.get(pos["t"]["code"], {}).get(d) if pos else None
        if pos and b is not None and b.volume:
            last_close[pos["t"]["code"]] = b.close
        if pos and pos["t"]["exit"] != OPEN_AT_END and pos["t"]["exit_date"] == ds:
            sp = _won(pos["t"]["exit_price"])
            sc = cf("SELL", pos["q"], sp)
            cash += sp * pos["q"] - sc
            total_cost += sc
            done.append({**pos, "sp": sp, "sc": sc, "pnl": (sp - pos["px"]) * pos["q"] - pos["bc"] - sc})
            pos = None
            in_mkt += 1
        elif pos:
            in_mkt += 1
        mark = last_close.get(pos["t"]["code"], pos["px"]) if pos else 0
        equity = cash + (mark * pos["q"] if pos else 0)
        peak = max(peak, equity)
        mdd_won = min(mdd_won, equity - peak)
        mdd_pct = min(mdd_pct, (equity - peak) / peak * 100)
    final = equity if days else float(capital)
    return {"capital": capital, "final": final, "net_won": final - capital, "return_pct": (final - capital) / capital * 100,
            "mdd_pct": mdd_pct, "mdd_won": mdd_won, "max_exposure_pct": max_exp, "days_in_market": in_mkt,
            "days": len(days), "total_cost": total_cost, "n_trades": len(done) + (1 if pos else 0),
            "open_at_end": pos is not None, "skipped": skipped,
            "gap_uncertain": sum(1 for x in done if x["t"].get("quality") == DATA_GAP_UNCERTAIN)
            + (1 if pos and pos["t"].get("quality") == DATA_GAP_UNCERTAIN else 0)}


BIG_WIN_PCT = 15.0


def _dist_stats(ts: list[dict]) -> dict:
    net = [t["net_pct"] for t in ts]
    if not net:
        return {"n": 0}
    return {"n": len(net), "mean": sum(net) / len(net), "median": median(net), "worst": min(net),
            "win_rate": sum(1 for x in net if x > 0) / len(net) * 100, "sum": sum(net),
            "big_wins": sum(1 for x in net if x >= BIG_WIN_PCT)}


def distance_buckets(trades: Iterable[dict], edges: tuple[float, ...] = (5.0, 8.0, 12.0)) -> list[dict]:
    """확정 손익 표본을 진입가 기준 손절 거리 구간으로 나눔(G4). 측정용 — 운영 조건 아님."""
    ts = [t for t in confirmed(trades) if t.get("stop_dist_pct") is not None]
    bounds = [-math.inf, *edges, math.inf]          # (lo, hi] — 상한 표(≤ X%)와 같은 경계
    out = []
    for lo, hi in zip(bounds, bounds[1:]):
        sel = [t for t in ts if lo < t["stop_dist_pct"] <= hi]
        label = (f"{hi:g}% 이하" if lo == -math.inf else
                 f"{lo:g}% 초과 {hi:g}% 이하" if hi != math.inf else f"{lo:g}% 초과")
        out.append({"label": label, **_dist_stats(sel)})
    return out


def distance_caps(trades: Iterable[dict], caps: tuple = (5.0, 8.0, 10.0, 12.0, 15.0, None)) -> list[dict]:
    """'손절 거리 ≤ X%인 신호만 남기면' — 남는 진입 가정 신호 수, 확정 표본 통계, 큰 이익(+15% 이상) 중 남는 수(G4).
    신호별 독립 기준. 순차 계좌는 다시 고르지 않음(제외로 다른 신호가 들어올 수 있음 — 따로 측정 필요)."""
    trades = list(trades)
    entered = [t for t in trades if t.get("status") == ENTERED and t.get("stop_dist_pct") is not None]
    conf = [t for t in confirmed(trades) if t.get("stop_dist_pct") is not None]
    all_big = sum(1 for t in conf if t["net_pct"] >= BIG_WIN_PCT)
    out = []
    for c in caps:
        keep = (lambda t: True) if c is None else (lambda t, c=c: t["stop_dist_pct"] <= c)
        out.append({"cap": c, "entered": sum(1 for t in entered if keep(t)), "all_big": all_big,
                    **_dist_stats([t for t in conf if keep(t)])})
    return out
