from __future__ import annotations

"""시장 환경 분류 (요청서 6절, market_version = m1).

신호일 t의 완성된 소속 시장 지수 일봉으로, 아래 우선순위대로 판정합니다.
  1. UNKNOWN  : 지수 봉·MA 계산 불가 (기준일 지수 봉 없음 포함)
  2. RISK_OFF : C < MA120 이고 MA60[t] < MA60[t-5]
  3. RISK_ON  : C > MA60  이고 MA60[t] > MA60[t-5]
  4. MIXED    : 나머지
"""

from dataclasses import dataclass

from domain.research import features as F
from domain.research.series import SeriesView

MARKET_VERSION = "m1"
RISK_ON, RISK_OFF, MIXED, UNKNOWN = "RISK_ON", "RISK_OFF", "MIXED", "UNKNOWN"


@dataclass(frozen=True)
class MarketRegime:
    state: str
    close: float | None
    ma60: float | None
    ma60_prev5: float | None
    ma120: float | None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "close": self.close, "ma60": self.ma60,
                "ma60_prev5": self.ma60_prev5, "ma120": self.ma120, "reason": self.reason}


def classify_market(index: SeriesView) -> MarketRegime:
    c, m60, m60p, m120 = F.close(index), F.sma(index, 60), F.sma(index, 60, 5), F.sma(index, 120)
    vals = [c, m60, m60p, m120]
    if not all(x.ok for x in vals):
        why = next(x.reason for x in vals if not x.ok)
        return MarketRegime(UNKNOWN, c.value, m60.value, m60p.value, m120.value, why)
    c, m60, m60p, m120 = (x.value for x in vals)
    if c < m120 and m60 < m60p:
        state = RISK_OFF
    elif c > m60 and m60 > m60p:
        state = RISK_ON
    else:
        state = MIXED
    return MarketRegime(state, c, m60, m60p, m120)
