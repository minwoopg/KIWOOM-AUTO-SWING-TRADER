from __future__ import annotations

"""현재가 조회 (8-B, F3 — 계좌 전체 노출을 평가액으로 계산하기 위함).

신규 매수 의도가 있을 때만, 계좌 전체 보유 종목의 현재가를 한 번씩 조회합니다.
조회 실패한 종목은 결과에서 빠지고, 안전 한도(`check_intent`)가 PRICE_UNKNOWN으로
매수를 보류합니다(원가로 대신 평가하지 않음).

`KiwoomQuoteSource`는 원본 그대로 가져온 `KiwoomBroker._post()`로 ka10001
(주식기본정보)만 호출합니다 — 조회 전용, 주문 경로와 무관. 호출 간격은 일봉
수집과 같은 이유(0.5초 간격에서 429 재현)로 기본 1초.
"""

import time
from typing import Callable, Iterable, Protocol


class QuoteSource(Protocol):
    def get_prices(self, symbols: Iterable[str]) -> dict[str, int]: ...


def _abs_int(value) -> int:
    text = str(value if value is not None else "").strip().replace(",", "")
    if text.startswith(("+", "-")):
        text = text[1:]
    return int(text) if text.isdigit() else 0


class KiwoomQuoteSource:
    ENDPOINT = "/api/dostk/stkinfo"
    API_ID = "ka10001"

    def __init__(self, broker, *, min_interval_sec: float = 1.0, logger=None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.broker = broker
        self.min_interval_sec = min_interval_sec
        self.logger = logger
        self.clock = clock
        self.sleep = sleep
        self._last_call_at: float | None = None
        self.calls = 0

    def _pace(self) -> None:
        if self._last_call_at is not None:
            wait = self.min_interval_sec - (self.clock() - self._last_call_at)
            if wait > 0:
                self.sleep(wait)
        self._last_call_at = self.clock()

    def get_prices(self, symbols: Iterable[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for sym in sorted(set(symbols)):
            self._pace()
            self.calls += 1
            try:
                resp = self.broker._post(endpoint=self.ENDPOINT, api_id=self.API_ID,
                                         payload={"stk_cd": sym}, cont_yn="N", next_key="")
                body = resp.body if isinstance(resp.body, dict) else {}
                price = _abs_int(body.get("cur_prc"))
            except Exception as exc:
                if self.logger is not None:
                    self.logger.warning(f"[QUOTE] {sym} 현재가 조회 실패: {type(exc).__name__}: {exc}")
                continue
            if price > 0:
                out[sym] = price
            elif self.logger is not None:
                self.logger.warning(f"[QUOTE] {sym} 현재가 값 이상: {body.get('cur_prc')!r}")
        return out


class StaticQuoteSource:
    """테스트·모의 실행용 고정 가격표."""

    def __init__(self, prices: dict[str, int] | None = None) -> None:
        self.prices = dict(prices or {})
        self.requests: list[tuple[str, ...]] = []

    def get_prices(self, symbols: Iterable[str]) -> dict[str, int]:
        req = tuple(sorted(set(symbols)))
        self.requests.append(req)
        return {s: self.prices[s] for s in req if self.prices.get(s, 0) > 0}
