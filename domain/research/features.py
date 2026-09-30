from __future__ import annotations

"""순수 지표 함수 (feature_version = f1).

모두 `SeriesView`(기준일 t까지만 보는 창)를 받아 `FV`(값 또는 UNKNOWN+사유)를
돌려줍니다. `off`는 t에서 몇 세션 전을 기준으로 계산할지입니다(0 = t).

정의 (요청서 5절·보충안 4절)
- SMA_N        : 기준일을 포함한 최근 N개 세션 종가 단순평균
- slope(N,lag) : SMA_N[t] / SMA_N[t-lag] - 1
- ret_N        : C[t] / C[t-N] - 1
- RS_N         : 종목 ret_N - 같은 세션 구간 지수 ret_N
- ATR14        : TR = max(H-L, |H-C전일|, |L-C전일|)의 최근 14개 **단순평균** (Wilder 아님)
- extension_N  : (C - SMA_N) / ATR14
- close_loc    : (C-L)/(H-L), H=L이면 UNKNOWN(ZERO_RANGE)
- volume_ratio : V[t] / 평균(V[t-20..t-1]), 분모 0이면 UNKNOWN
- trade_value_avg_N : 최근 N개 세션 실제 거래대금 평균(원). 하나라도 없으면 UNKNOWN
- tr_contraction    : 평균(TR[t-5..t-1]) / 평균(TR[t-25..t-6])   ← t 당일 제외
- volume_dryup      : 평균(V[t-5..t-1]) / 평균(V[t-25..t-6])     ← t 당일 제외
- high252_ratio / low252_ratio : C[t]/max(H[t-251..t])-1, C[t]/min(L[t-251..t])-1 (252개 전부 있을 때만)
- return_atr   : (C[t]-C[t-1]) / ATR14[t-1]
- narrow_range7: (H-L)[t] ≤ 직전 6개 세션 각각의 (H-L)
- EMA_N        : alpha=2/(N+1). t까지 공백 없이 이어진 구간의 첫 N개 종가 SMA로 시작값을 만들고
                 이후 갱신. **시작값 뒤 5N번 갱신한 뒤부터 유효** (필요 봉 수 = 6N). 그 전은 UNKNOWN.
"""

from dataclasses import dataclass
from datetime import date

from domain.research.series import ResearchBar, SeriesView
from domain.research.types import FV, ratio

FEATURE_VERSION = "f1"
EMA_WARMUP_MULTIPLE = 5


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def close(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(1, off)
    return why or FV(bars[0].close)


def sma(v: SeriesView, n: int, off: int = 0) -> FV:
    bars, why = v.fv_window(n, off)
    return why or FV(_mean([b.close for b in bars]))


def sma_slope(v: SeriesView, n: int, lag: int, off: int = 0) -> FV:
    now, before = sma(v, n, off), sma(v, n, off + lag)
    if not now.ok or not before.ok:
        return FV.unknown(now.reason or before.reason)
    return FV(now.value / before.value - 1)


def ret(v: SeriesView, n: int, off: int = 0) -> FV:
    bars, why = v.fv_window(n + 1, off)
    return why or FV(bars[-1].close / bars[0].close - 1)


def rs(stock: SeriesView, index: SeriesView, n: int, off: int = 0) -> FV:
    """A13-R1: 기준일이 같고, 수익률 구간의 실제 날짜 배열이 같을 때만 계산."""
    if stock.t != index.t:
        return FV.unknown(f"AS_OF_MISMATCH:stock={stock.t.isoformat()},index={index.t.isoformat()}")
    sb, why = stock.window(n + 1, off)
    if sb is None:
        return FV.unknown(why)
    ib, iwhy = index.window(n + 1, off)
    if ib is None:
        return FV.unknown(f"INDEX:{iwhy}")
    if [b.date for b in sb] != [b.date for b in ib]:
        return FV.unknown("SESSION_ALIGNMENT_MISMATCH")
    return FV((sb[-1].close / sb[0].close - 1) - (ib[-1].close / ib[0].close - 1))


def _trs(bars: list[ResearchBar]) -> list[float]:
    """bars[0]은 전일 종가용. 반환 길이 = len(bars)-1."""
    return [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(bars, bars[1:])]


def atr(v: SeriesView, n: int = 14, off: int = 0) -> FV:
    bars, why = v.fv_window(n + 1, off)
    return why or FV(_mean(_trs(bars)))


def extension(v: SeriesView, n: int = 20, off: int = 0) -> FV:
    c, m, a = close(v, off), sma(v, n, off), atr(v, 14, off)
    for x in (c, m, a):
        if not x.ok:
            return x
    return ratio(c.value - m.value, a, what="ATR14")


def close_location(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(1, off)
    if why:
        return why
    b = bars[0]
    if b.high == b.low:
        return FV.unknown("ZERO_RANGE")
    return FV((b.close - b.low) / (b.high - b.low))


def volume_ratio(v: SeriesView, n: int = 20, off: int = 0) -> FV:
    bars, why = v.fv_window(n + 1, off)
    if why:
        return why
    return ratio(float(bars[-1].volume), _mean([float(b.volume) for b in bars[:-1]]), what="AVG_VOLUME")


def trade_value_avg(v: SeriesView, n: int = 20, off: int = 0) -> FV:
    bars, why = v.fv_window(n, off)
    if why:
        return why
    missing = [b.date for b in bars if b.trade_value is None]
    if missing:
        return FV.unknown(f"TRADE_VALUE_MISSING:{len(missing)}")
    return FV(_mean([float(b.trade_value) for b in bars]))


def tr_contraction(v: SeriesView, off: int = 0) -> FV:
    # TR[t-25..t-1] 25개 → 종가 t-26..t-1 필요 = 26개 봉, 끝은 t-1
    bars, why = v.fv_window(26, off + 1)
    if why:
        return why
    trs = _trs(bars)                  # 25개: TR[t-25..t-1]
    return ratio(_mean(trs[-5:]), _mean(trs[:20]), what="TR_BASE")


def volume_dryup(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(25, off + 1)   # V[t-25..t-1]
    if why:
        return why
    vols = [float(b.volume) for b in bars]
    return ratio(_mean(vols[-5:]), _mean(vols[:20]), what="VOLUME_BASE")


def high252_ratio(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(252, off)
    return why or FV(bars[-1].close / max(b.high for b in bars) - 1)


def low252_ratio(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(252, off)
    return why or FV(bars[-1].close / min(b.low for b in bars) - 1)


def return_atr(v: SeriesView, off: int = 0) -> FV:
    bars, why = v.fv_window(2, off)
    if why:
        return why
    return ratio(bars[1].close - bars[0].close, atr(v, 14, off + 1), what="ATR14_PREV")


def narrow_range7(v: SeriesView, off: int = 0) -> FV:
    """1.0 = 좁은 일봉, 0.0 = 아님."""
    bars, why = v.fv_window(7, off)
    if why:
        return why
    r = bars[-1].range
    return FV(1.0 if all(r <= b.range for b in bars[:-1]) else 0.0)


@dataclass(frozen=True)
class EmaResult:
    value: FV
    seed_date: date | None
    updates_after_seed: int


def ema(v: SeriesView, n: int, off: int = 0) -> EmaResult:
    """공백 없이 이어진 구간의 시작부터 계산 (off 세션 전까지)."""
    target = v.session_at(off)
    st = v.status()
    if st or target is None:
        return EmaResult(FV.unknown(st or "INSUFFICIENT_SESSIONS"), None, 0)
    if target not in v.by_date:
        return EmaResult(FV.unknown(f"DATA_GAP:{target.isoformat()}"), None, 0)
    # target에서 거슬러 올라가 공백 없는 구간 시작 찾기
    i = v._idx[target]
    while i - 1 >= 0 and v.sessions[i - 1] in v.by_date:
        i -= 1
    run = [v.by_date[d] for d in v.sessions[i:v._idx[target] + 1]]
    if len(run) < n:
        return EmaResult(FV.unknown("INSUFFICIENT_HISTORY"), None, 0)
    alpha = 2 / (n + 1)
    e = _mean([b.close for b in run[:n]])
    for b in run[n:]:
        e = alpha * b.close + (1 - alpha) * e
    updates = len(run) - n
    if updates < EMA_WARMUP_MULTIPLE * n:
        return EmaResult(FV.unknown(f"EMA_WARMUP:{updates}/{EMA_WARMUP_MULTIPLE * n}"), run[n - 1].date, updates)
    return EmaResult(FV(e), run[n - 1].date, updates)
