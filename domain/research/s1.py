from __future__ import annotations

"""S1 눌림 회복 평가기 — s1_pullback_v0.1 (요청서 8절). 주문과 무관한 순수 함수.

판정 묶음
- pattern_pass     : 이력·추세·상대강도·눌림·회복·과열 (가격 패턴)
- eligibility_pass : 증권 유형·위험 상태·유동성 (종목 자격)
- market_pass      : 시장 환경 RISK_ON만 PASS (MIXED·RISK_OFF는 FAIL=보류, UNKNOWN은 UNKNOWN)
- stop_valid       : 참고 손절가가 유효(>0)한가 (A13-R4)
- eligible_signal  : 네 묶음 모두 PASS — 다음날 확인 후보는 이 값만 봅니다.
  (INVALID_STOP이면 패턴·자격 결과는 보존하되 최종 후보는 FAIL로 보류)
각 조건은 PASS/FAIL/UNKNOWN과 값·사유를 남깁니다. 한 조건이 UNKNOWN이어도 나머지는 계산합니다.

기준일 정합 (A13-R1): 종목·지수 View와 시장 판정의 기준일이 모두 같아야 합니다. 다르면
RS·시장 조건을 UNKNOWN(AS_OF_MISMATCH)으로 두고, 수익률 구간 날짜가 다르면
SESSION_ALIGNMENT_MISMATCH입니다.
지수 원천 정합 (A13-Q1): **기본 경로는 market=None** — RS에 쓴 같은 index View로 시장을 판정합니다.
외부 시장 판정을 넘기면 index.source_id와 market.index_id가 둘 다 있고 같아야 하며, 아니면
시장 조건 UNKNOWN(INDEX_SOURCE_MISMATCH). Eligibility.market_index_id(종목의 당시 소속 시장 지수)를
주면 index.source_id와 같아야 하고, 다르면 RS·시장 조건 UNKNOWN(INDEX_NOT_STOCK_MARKET).

눌림 정의 (8.3)
  p = t-20..t-1 중 고가 최대 봉(동률이면 최근), 조정 구간 = p 다음 봉 ~ t-1 (2~10개),
  구간에 C[p]보다 낮은 종가 1개 이상, 구간 모든 종가 ≥ 그 날짜의 MA60.
회복 (8.4): C[t] > H[t-1], C[t] ≥ MA20[t], (C-MA20)/ATR14 ≤ 2.0, ATR14 > 0.
참고 손절가 = min(L_pullback, L[t]) - 0.2×ATR14 (≤0이면 INVALID_STOP),
진입 상한(분석용) = C[t] + 0.5×ATR14.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

from domain.research import features as F
from domain.research.market import RISK_ON, UNKNOWN as MKT_UNKNOWN, MarketRegime, classify_market
from domain.research.series import SeriesView
from domain.research.types import FV, Check, Tri, combine, compare

STRATEGY_ID = "s1_pullback"
STRATEGY_VERSION = "v0.1"

COMMON = "COMMON"          # 보통주
RISK_OK, RISK_FLAGGED = "OK", "FLAGGED"


@dataclass(frozen=True)
class S1Config:
    min_history: int = 160
    min_trade_value_20: int = 3_000_000_000          # 20일 평균 실제 거래대금(원)
    pivot_lookback: int = 20
    pullback_min: int = 2
    pullback_max: int = 10
    max_extension20: float = 2.0
    stop_atr_buffer: float = 0.2
    entry_cap_atr: float = 0.5

    def __post_init__(self) -> None:
        ints = ("min_history", "min_trade_value_20", "pivot_lookback", "pullback_min", "pullback_max")
        for name in ints:
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise ValueError(f"S1Config.{name}는 양의 정수 — {v!r}")
        for name in ("max_extension20", "stop_atr_buffer", "entry_cap_atr"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not (v == v and abs(v) != float("inf")) or v < 0:
                raise ValueError(f"S1Config.{name}는 0 이상 유한수 — {v!r}")
        if self.pullback_min > self.pullback_max or self.pullback_max >= self.pivot_lookback:
            raise ValueError("pullback_min ≤ pullback_max < pivot_lookback 이어야 함")

    def config_hash(self) -> str:
        raw = json.dumps({"strategy": f"{STRATEGY_ID}_{STRATEGY_VERSION}", **asdict(self)}, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Eligibility:
    """t 시점에 확인된 종목 자격 정보. 모르면 None(UNKNOWN)."""
    security_type: str | None = None      # "COMMON" / "PREFERRED" / "ETF" ...
    risk_status: str | None = None        # "OK" / "FLAGGED" / None
    risk_detail: str = ""
    market_index_id: str | None = None    # 종목 소속 시장 지수의 source_id (예: "INDEX:KOSPI:001")


@dataclass
class S1Result:
    symbol: str
    signal_date: date
    strategy: str = f"{STRATEGY_ID}_{STRATEGY_VERSION}"
    feature_version: str = F.FEATURE_VERSION
    config_hash: str = ""
    pattern_pass: Tri = Tri.UNKNOWN
    eligibility_pass: Tri = Tri.UNKNOWN
    market_pass: Tri = Tri.UNKNOWN
    stop_valid: Tri = Tri.UNKNOWN
    eligible_signal: Tri = Tri.UNKNOWN
    checks: list[Check] = field(default_factory=list)
    market: dict | None = None
    pullback: dict = field(default_factory=dict)
    levels: dict = field(default_factory=dict)        # stop_ref, entry_cap, risk_ratio, stop_status
    observations: dict = field(default_factory=dict)  # 조건이 아닌 관찰값

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def reason_codes(self) -> list[str]:
        return [f"{c.name}:{c.result.value}" + (f"({c.detail})" if c.result != Tri.PASS and c.detail else "")
                for c in self.checks]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "signal_date": self.signal_date.isoformat(), "strategy": self.strategy,
            "feature_version": self.feature_version, "config_hash": self.config_hash,
            "pattern_pass": self.pattern_pass.value, "eligibility_pass": self.eligibility_pass.value,
            "market_pass": self.market_pass.value, "stop_valid": self.stop_valid.value,
            "eligible_signal": self.eligible_signal.value,
            "checks": [c.to_dict() for c in self.checks], "market": self.market,
            "pullback": self.pullback, "levels": self.levels, "observations": self.observations,
        }


def _v(fv: FV):
    return fv.value if fv.ok else None


def _find_pullback(v: SeriesView, cfg: S1Config) -> tuple[list[Check], dict]:
    """눌림 구간 판정. (조건 목록, 눌림 정보)."""
    name = "PULLBACK"
    bars, why = v.window(cfg.pivot_lookback, 1)          # t-20..t-1
    if bars is None:
        return [Check(name, Tri.UNKNOWN, None, why)], {}
    # 고가 최대, 동률이면 가장 최근
    p_idx = max(range(len(bars)), key=lambda i: (bars[i].high, i))
    p = bars[p_idx]
    segment = bars[p_idx + 1:]                             # p 다음 ~ t-1
    info = {"pivot_date": p.date.isoformat(), "pivot_high": p.high, "pivot_close": p.close,
            "pullback_len": len(segment)}
    if not segment:
        return [Check(name, Tri.FAIL, 0, "EMPTY_PULLBACK(고점이 t-1)")], info
    if not (cfg.pullback_min <= len(segment) <= cfg.pullback_max):
        return [Check(name, Tri.FAIL, len(segment), f"PULLBACK_LEN({cfg.pullback_min}~{cfg.pullback_max})")], info
    if not any(b.close < p.close for b in segment):
        return [Check(name, Tri.FAIL, len(segment), "NO_LOWER_CLOSE")], info
    # 구간 각 날짜의 MA60 (그 날짜까지의 데이터로)
    n_seg = len(segment)
    below, unknown = [], []
    for k, b in enumerate(segment):
        off = n_seg - k                                    # segment 마지막 = t-1 → off 1
        m = F.sma(v, 60, off)
        if not m.ok:
            unknown.append(f"{b.date}:{m.reason}")
        elif b.close < m.value:
            below.append(b.date.isoformat())
    low = min(b.low for b in segment)
    info.update({"pullback_low": low, "depth": (p.high - low) / p.high,
                 "pullback_start": segment[0].date.isoformat(), "pullback_end": segment[-1].date.isoformat(),
                 "avg_volume_pullback": sum(b.volume for b in segment) / n_seg,
                 "avg_range_pullback": sum(b.range for b in segment) / n_seg})
    if below:
        return [Check(name, Tri.FAIL, len(segment), f"MA60_BREAK:{below[0]}")], info
    if unknown:
        return [Check(name, Tri.UNKNOWN, len(segment), f"MA60_UNKNOWN:{unknown[0]}")], info
    return [Check(name, Tri.PASS, len(segment), f"len={n_seg}")], info


def evaluate_s1(symbol: str, stock: SeriesView, index: SeriesView, market: MarketRegime | None,
                eligibility: Eligibility, cfg: S1Config | None = None) -> S1Result:
    cfg = cfg or S1Config()
    t = stock.t
    external_market = market is not None
    if market is None:
        market = classify_market(index)
    r = S1Result(symbol, t, config_hash=cfg.config_hash(), market=market.to_dict())
    idx_mismatch = ""
    if eligibility.market_index_id is not None and eligibility.market_index_id != index.source_id:
        idx_mismatch = (f"INDEX_NOT_STOCK_MARKET:stock_market={eligibility.market_index_id},"
                        f"index={index.source_id or None}")
    pattern: list[Check] = []

    # ── 이력 ──
    hist, why = stock.window(cfg.min_history)
    pattern.append(Check("HISTORY", Tri.PASS if hist else Tri.UNKNOWN,
                         cfg.min_history if hist else None, "" if hist else why))

    # ── 추세·강도 ──
    c, ma20, ma60, ma60p, ma120 = F.close(stock), F.sma(stock, 20), F.sma(stock, 60), F.sma(stock, 60, 5), F.sma(stock, 120)
    atr14 = F.atr(stock, 14)
    if c.ok and ma120.ok:
        pattern.append(Check("C_GT_MA120", Tri.PASS if c.value > ma120.value else Tri.FAIL, c.value - ma120.value))
    else:
        pattern.append(Check("C_GT_MA120", Tri.UNKNOWN, None, (ma120 if c.ok else c).reason))
    if ma60.ok and ma60p.ok:
        pattern.append(Check("MA60_RISING", Tri.PASS if ma60.value > ma60p.value else Tri.FAIL,
                             ma60.value / ma60p.value - 1))
    else:
        pattern.append(Check("MA60_RISING", Tri.UNKNOWN, None, (ma60p if ma60.ok else ma60).reason))
    rs60 = F.rs(stock, index, 60) if not idx_mismatch else FV.unknown(idx_mismatch)
    pattern.append(compare("RS60_POS", rs60, ">", 0.0))

    # ── 눌림 ──
    pb_checks, pb = _find_pullback(stock, cfg)
    pattern.extend(pb_checks)
    r.pullback = pb

    # ── 회복·과열 ──
    prev = stock.bar_at(1)
    if c.ok and prev is not None:
        pattern.append(Check("CLOSE_GT_PREV_HIGH", Tri.PASS if c.value > prev.high else Tri.FAIL,
                             c.value - prev.high))
    else:
        pattern.append(Check("CLOSE_GT_PREV_HIGH", Tri.UNKNOWN, None, c.reason if not c.ok else "NO_PREV_BAR"))
    if c.ok and ma20.ok:
        pattern.append(Check("CLOSE_GE_MA20", Tri.PASS if c.value >= ma20.value else Tri.FAIL, c.value - ma20.value))
    else:
        pattern.append(Check("CLOSE_GE_MA20", Tri.UNKNOWN, None, (ma20 if c.ok else c).reason))
    pattern.append(compare("ATR_POS", atr14, ">", 0.0))
    ext = F.extension(stock, 20)
    pattern.append(compare("NOT_EXTENDED", ext, "<=", cfg.max_extension20))

    # ── 종목 자격 ──
    elig: list[Check] = []
    st = eligibility.security_type
    elig.append(Check("SECURITY_TYPE", Tri.UNKNOWN if st is None else (Tri.PASS if st == COMMON else Tri.FAIL),
                      st, "" if st else "SECURITY_TYPE_UNKNOWN"))
    rk = eligibility.risk_status
    elig.append(Check("RISK_STATUS", Tri.UNKNOWN if rk is None else (Tri.PASS if rk == RISK_OK else Tri.FAIL),
                      rk, eligibility.risk_detail or ("" if rk else "RISK_STATUS_UNKNOWN")))
    tv20 = F.trade_value_avg(stock, 20)
    elig.append(compare("LIQUIDITY_TV20", tv20, ">=", float(cfg.min_trade_value_20)))

    # ── 시장 ──
    if idx_mismatch:
        mk = Check("MARKET_REGIME", Tri.UNKNOWN, market.state, idx_mismatch)
    elif external_market and (not index.source_id or not market.index_id or market.index_id != index.source_id):
        mk = Check("MARKET_REGIME", Tri.UNKNOWN, market.state,
                   f"INDEX_SOURCE_MISMATCH:index={index.source_id or None},market={market.index_id or None}")
    elif index.t != t or market.as_of != t:
        mk = Check("MARKET_REGIME", Tri.UNKNOWN, market.state,
                   f"AS_OF_MISMATCH:stock={t.isoformat()},index={index.t.isoformat()},"
                   f"market={market.as_of.isoformat() if market.as_of else None}")
    elif market.state == MKT_UNKNOWN:
        mk = Check("MARKET_REGIME", Tri.UNKNOWN, market.state, market.reason)
    else:
        mk = Check("MARKET_REGIME", Tri.PASS if market.state == RISK_ON else Tri.FAIL, market.state,
                   "" if market.state == RISK_ON else "MARKET_HOLD")

    # ── 참고 가격 (분석용) ──
    bar_t = stock.bar_at(0)
    if atr14.ok and bar_t is not None and "pullback_low" in pb:
        stop = min(pb["pullback_low"], bar_t.low) - cfg.stop_atr_buffer * atr14.value
        cap = bar_t.close + cfg.entry_cap_atr * atr14.value
        r.levels = {"stop_ref": stop, "entry_cap": cap, "atr14": atr14.value,
                    "stop_status": "INVALID_STOP" if stop <= 0 else "OK",
                    "risk_ratio_at_cap": (cap - stop) / cap if stop > 0 else None}
        pb["depth_atr"] = (pb["pivot_high"] - pb["pullback_low"]) / atr14.value if atr14.value > 0 else None
        stop_check = Check("STOP_VALID", Tri.PASS if stop > 0 else Tri.FAIL, stop,
                           "" if stop > 0 else "INVALID_STOP(참고 손절가 ≤ 0)")
    else:
        why = atr14.reason if not atr14.ok else "NO_PULLBACK"
        r.levels = {"stop_status": "UNKNOWN", "reason": why}
        stop_check = Check("STOP_VALID", Tri.UNKNOWN, None, why)

    r.checks = pattern + elig + [mk, stop_check]
    r.pattern_pass = combine(x.result for x in pattern)
    r.eligibility_pass = combine(x.result for x in elig)
    r.market_pass = mk.result
    r.stop_valid = stop_check.result
    r.eligible_signal = combine([r.pattern_pass, r.eligibility_pass, r.market_pass, r.stop_valid])

    # ── 관찰값 (조건 아님) ──
    ma120_dist = F.ratio(c.value - ma120.value, atr14, what="ATR14") if (c.ok and ma120.ok) else ma120
    hi60 = None
    bars60, _ = stock.window(60, 1)
    if bars60 and pb.get("pivot_high") is not None:
        hi60 = 1.0 if pb["pivot_high"] >= max(b.high for b in bars60) else 0.0
    r.observations = {
        "rs20": _v(F.rs(stock, index, 20)) if not idx_mismatch else None, "rs60": _v(rs60), "ret20": _v(F.ret(stock, 20)),
        "ret60": _v(F.ret(stock, 60)), "tv20": _v(tv20), "tv20_reason": "" if tv20.ok else tv20.reason,
        "volume_ratio": _v(F.volume_ratio(stock)), "close_location": _v(F.close_location(stock)),
        "extension20": _v(ext), "ma120_distance_atr": _v(ma120_dist), "pivot_is_60d_high": hi60,
        "ma60_slope5": (ma60.value / ma60p.value - 1) if (ma60.ok and ma60p.ok) else None,
    }
    return r


def candidate_sort_key(res: S1Result):
    """관찰 후보 정렬: RS60 내림차순 → 20일 거래대금 내림차순 → 종목코드 오름차순 (결정적)."""
    rs60 = res.observations.get("rs60")
    tv = res.observations.get("tv20")
    return (-(rs60 if rs60 is not None else float("-inf")), -(tv if tv is not None else float("-inf")), res.symbol)
