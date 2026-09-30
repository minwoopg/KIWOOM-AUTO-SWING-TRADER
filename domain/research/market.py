from __future__ import annotations

"""시장 환경 분류 (요청서 6절, market_version = m1).

신호일 t의 완성된 소속 시장 지수 일봉으로, 아래 우선순위대로 판정합니다.
  1. UNKNOWN  : 지수 봉·MA 계산 불가 (기준일 지수 봉 없음 포함)
  2. RISK_OFF : C < MA120 이고 MA60[t] < MA60[t-5]
  3. RISK_ON  : C > MA60  이고 MA60[t] > MA60[t-5]
  4. MIXED    : 나머지

A13-R1: 판정에 기준일(as_of)과 지수 식별자(index_id)를 붙입니다. 평가기는 종목 기준일과
다른 판정을 받으면 UNKNOWN(AS_OF_MISMATCH)으로 처리합니다.
"""

from dataclasses import dataclass
from datetime import date

from domain.research import features as F
from domain.research.series import SeriesView

MARKET_VERSION = "m1"
RISK_ON, RISK_OFF, MIXED, UNKNOWN = "RISK_ON", "RISK_OFF", "MIXED", "UNKNOWN"


@dataclass(frozen=True)
class MarketRegime:
    as_of: date | None
    index_id: str
    state: str
    close: float | None
    ma60: float | None
    ma60_prev5: float | None
    ma120: float | None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"as_of": self.as_of.isoformat() if self.as_of else None, "index_id": self.index_id,
                "state": self.state, "close": self.close, "ma60": self.ma60,
                "ma60_prev5": self.ma60_prev5, "ma120": self.ma120, "reason": self.reason}


def classify_market(index: SeriesView, index_id: str = "") -> MarketRegime:
    c, m60, m60p, m120 = F.close(index), F.sma(index, 60), F.sma(index, 60, 5), F.sma(index, 120)
    vals = [c, m60, m60p, m120]
    if not all(x.ok for x in vals):
        why = next(x.reason for x in vals if not x.ok)
        return MarketRegime(index.t, index_id, UNKNOWN, c.value, m60.value, m60p.value, m120.value, why)
    c, m60, m60p, m120 = (x.value for x in vals)
    if c < m120 and m60 < m60p:
        state = RISK_OFF
    elif c > m60 and m60 > m60p:
        state = RISK_ON
    else:
        state = MIXED
    return MarketRegime(index.t, index_id, state, c, m60, m60p, m120)
