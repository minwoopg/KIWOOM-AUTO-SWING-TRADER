from __future__ import annotations

"""일봉 원시 페이지 수집 + 호출 속도 제한 (스윙 분리 5라운드, 2026-09-28).

실측(2026-09-28 프로브 2회): 0.5초 간격으로 호출하면 5번째에서 HTTP 429가
재현됨 → 기본 호출 간격 1초, 429·전송 실패는 대기 후 재시도(조회 전용이라
재시도해도 안전), 재시도 횟수에 상한.

`KiwoomDailyBarSource`는 원본 그대로 가져온 `KiwoomBroker`의 `_post()`를
사용합니다(인증·헤더·HTTP 오류 분류를 다시 만들지 않기 위함). 조회 TR은
ka10081 하나뿐이며 주문 경로와 무관합니다.
"""

import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence


@dataclass(frozen=True)
class RawDailyPage:
    rows: list
    cont_yn: str
    next_key: str

    @property
    def has_more(self) -> bool:
        return self.cont_yn == "Y" and bool(self.next_key)


class DailyBarSource(Protocol):
    def fetch_page(self, symbol: str, base_dt: str, cont_yn: str, next_key: str) -> RawDailyPage: ...


class RateLimitedError(RuntimeError):
    """호출 한도 초과(429) — 재시도 대상."""


class TransientFetchError(RuntimeError):
    """전송 실패 등 일시적 오류 — 재시도 대상."""


class DataFetchError(RuntimeError):
    """재시도 후에도 실패, 또는 재시도하면 안 되는 오류."""


class KiwoomDailyBarSource:
    """ka10081 한 페이지 조회. 429/전송 실패는 재시도용 예외로 바꿔서 던짐."""

    ENDPOINT = "/api/dostk/chart"
    API_ID = "ka10081"
    LIST_KEY = "stk_dt_pole_chart_qry"

    def __init__(self, broker) -> None:
        self.broker = broker

    def fetch_page(self, symbol: str, base_dt: str, cont_yn: str = "N", next_key: str = "") -> RawDailyPage:
        from infra.broker.kiwoom_broker import KiwoomHttpError, KiwoomTransportError

        try:
            resp = self.broker._post(
                endpoint=self.ENDPOINT, api_id=self.API_ID,
                payload={"stk_cd": symbol, "base_dt": base_dt, "upd_stkpc_tp": "1"},
                cont_yn=cont_yn, next_key=next_key,
            )
        except KiwoomHttpError as exc:
            if exc.status_code == 429:
                raise RateLimitedError(str(exc)) from exc
            raise DataFetchError(f"{symbol}: HTTP {exc.status_code}") from exc
        except KiwoomTransportError as exc:
            raise TransientFetchError(str(exc)) from exc
        except RuntimeError as exc:  # 업무 오류(return_code != 0), 토큰 없음 등
            raise DataFetchError(f"{symbol}: {exc}") from exc
        rows = resp.body.get(self.LIST_KEY) if isinstance(resp.body, dict) else None
        if not isinstance(rows, list):
            raise DataFetchError(f"{symbol}: 응답에 {self.LIST_KEY} 목록 없음")
        return RawDailyPage(
            rows=rows,
            cont_yn=str(resp.headers.get("cont-yn", "")).strip().upper(),
            next_key=str(resp.headers.get("next-key", "")).strip(),
        )


class PacedFetcher:
    """모든 조회를 이 객체 하나로 통과시켜 간격을 지키고 재시도합니다.

    여러 종목을 갱신해도 같은 인스턴스를 쓰면 전체 호출이 min_interval 이상
    떨어집니다.
    """

    def __init__(
        self,
        source: DailyBarSource,
        *,
        min_interval_sec: float = 1.0,
        retry_backoff_sec: Sequence[float] = (2.0, 5.0, 10.0, 20.0),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        logger=None,
    ) -> None:
        if min_interval_sec < 0:
            raise ValueError("min_interval_sec는 0 이상")
        self.source = source
        self.min_interval_sec = min_interval_sec
        self.retry_backoff_sec = tuple(retry_backoff_sec)
        self.clock = clock
        self.sleep = sleep
        self.logger = logger
        self._last_call_at: float | None = None
        self.calls = 0
        self.retries = 0

    def _pace(self) -> None:
        if self._last_call_at is not None:
            wait = self.min_interval_sec - (self.clock() - self._last_call_at)
            if wait > 0:
                self.sleep(wait)
        self._last_call_at = self.clock()

    def fetch_page(self, symbol: str, base_dt: str, cont_yn: str = "N", next_key: str = "") -> RawDailyPage:
        attempt = 0
        while True:
            self._pace()
            self.calls += 1
            try:
                return self.source.fetch_page(symbol, base_dt, cont_yn, next_key)
            except (RateLimitedError, TransientFetchError) as exc:
                if attempt >= len(self.retry_backoff_sec):
                    raise DataFetchError(
                        f"{symbol}: 재시도 {attempt}회 후에도 실패 — {type(exc).__name__}: {exc}"
                    ) from exc
                wait = self.retry_backoff_sec[attempt]
                attempt += 1
                self.retries += 1
                if self.logger is not None:
                    self.logger.warning(
                        f"[DAILY_BARS] {symbol} | {type(exc).__name__} — {wait}초 후 재시도 "
                        f"({attempt}/{len(self.retry_backoff_sec)})")
                self.sleep(wait)
