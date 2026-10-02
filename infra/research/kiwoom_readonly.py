from __future__ import annotations

"""연구 수집 전용 키움 조회 클라이언트 (A2).

안전 규칙 (A단계 합의·프로브와 같은 원칙)
- **모의투자 도메인(https://mockapi.kiwoom.com)에서만** 동작합니다. 우회 옵션 없음.
  (모의 도메인의 시세·목록은 실제 시장 데이터 — A1 실측으로 확인)
- 허용 TR은 조회 6개뿐: 수집용 ka10099(종목 목록)·ka10081(종목 일봉)·ka20006(지수 일봉)과
  A5-1 가격 기록용 ka10001(주식기본정보 — 현재가·기준가)·ka10003(체결정보 — 체결 시각)·ka10004(주식호가).
  주문·계좌 TR은 목록에 없으므로 호출 자체가 막힙니다.
- 운영 브로커(`infra.broker`)·주문 실행부·원장·commands 폴더와 무관합니다.
- 토큰·앱키는 예외 메시지나 로그에 넣지 않습니다.

호출 간격: 모든 요청을 이 객체 하나로 통과시켜 `min_interval_sec`(기본 1초 — 0.5초 간격에서 429 실측)를 지킵니다.
429·전송 실패는 대기 후 재시도(조회 전용이라 안전), HTTP 401은 한 번 재인증 후 재시도.
그 밖의 HTTP 오류·return_code≠0·목록 없음은 재시도하지 않고 `ResearchApiError`.

응답 계약 (A2-R3): return_code가 **있고 0**이어야 성공. cont-yn 헤더는 Y 또는 N이어야 하고,
Y이면 next-key가 있어야 합니다. 어기면 `ResearchApiError`(이력 끝으로 해석하지 않음).
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from utils.time_utils import now_local

ALLOWED_BASE_URL_HOST = "mockapi.kiwoom.com"
RESEARCH_API = {
    "ka10099": "/api/dostk/stkinfo",   # 종목정보 리스트
    "ka10081": "/api/dostk/chart",     # 주식 일봉
    "ka20006": "/api/dostk/chart",     # 업종(지수) 일봉
}
PRICE_API = {                          # A5-1: 시세 응답 (fetch_body로만, 첫 페이지만 — 이어 받지 않음)
    "ka10001": "/api/dostk/stkinfo",   # 주식기본정보 — cur_prc(현재가)·base_pric(기준가) 등 (판정 기준)
    "ka10003": "/api/dostk/stkinfo",   # 체결정보 — 최근 체결 tm·cur_prc (원천 가격 시각, 10/2 실측 확인)
    "ka10004": "/api/dostk/mrkcond",   # 주식호가 — 호가 기준 시각·최우선 호가 (10/2 실측 확인)
}
_ALL_API = {**RESEARCH_API, **PRICE_API}


class ResearchConfigError(ValueError):
    """안전 요건 위반 (도메인·TR)."""


class ResearchApiError(RuntimeError):
    """재시도하지 않는 조회 오류."""


class _Retryable(RuntimeError):
    pass


class DeadlinePassed(RuntimeError):
    """요청 직전(호출 간격 대기·재시도 대기 뒤) 시각이 마감 이후 — 요청을 보내지 않음 (A5-R2).
    ResearchApiError가 아님: '조회 실패'가 아니라 '마감이 지나 조회하지 않음'. attempts = 이미 보낸 요청 수."""

    def __init__(self, msg: str, attempts: int) -> None:
        super().__init__(msg)
        self.attempts = attempts


def assert_mock_domain(base_url: str) -> None:
    parsed = urlparse(base_url or "")
    if ((parsed.scheme or "").lower() != "https"
            or (parsed.hostname or "").lower() != ALLOWED_BASE_URL_HOST
            or parsed.port not in (None, 443)):
        raise ResearchConfigError(
            f"연구 수집은 https://{ALLOWED_BASE_URL_HOST} 에서만 실행합니다 (현재 {base_url!r})")


@dataclass(frozen=True)
class Body:
    body: dict
    requested_at: datetime         # 마지막 시도의 요청 직전 시각
    received_at: datetime          # 응답을 받은 시각
    attempts: int                  # 재시도 포함 시도 횟수


@dataclass(frozen=True)
class Page:
    rows: list
    cont_yn: str
    next_key: str
    received_at: datetime          # 응답을 받은 시각 (Asia/Seoul naive)

    @property
    def has_more(self) -> bool:
        return self.cont_yn == "Y" and bool(self.next_key)


class ReadOnlyResearchClient:
    def __init__(self, session, base_url: str, app_key: str, secret_key: str, *,
                 min_interval_sec: float = 1.0,
                 retry_backoff_sec: Sequence[float] = (2.0, 5.0, 10.0, 20.0),
                 now: Callable[[], datetime] = now_local,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] | None = None) -> None:
        assert_mock_domain(base_url)
        if min_interval_sec < 0.5:
            raise ResearchConfigError("min_interval_sec는 0.5 이상 (실측상 0.5초 간격에서도 429)")
        self.session = session
        self.base_url = base_url
        self._app_key = app_key
        self._secret_key = secret_key
        self.min_interval_sec = min_interval_sec
        self.retry_backoff_sec = tuple(retry_backoff_sec)
        self.now = now
        self.monotonic = monotonic
        self.sleep = sleep
        self.log = log or (lambda msg: None)
        self._token = ""
        self._last_call: float | None = None
        self.calls = 0
        self.retries = 0

    # ── 인증 ──
    def authenticate(self) -> None:
        assert_mock_domain(self.base_url)
        self._pace()
        r = self.session.post(f"{self.base_url}/oauth2/token", json={
            "grant_type": "client_credentials", "appkey": self._app_key, "secretkey": self._secret_key,
        }, timeout=10)
        try:
            body = r.json()
        except ValueError:
            body = None
        token = body.get("token") if isinstance(body, dict) else None
        if r.status_code != 200 or not token:
            msg = body.get("return_msg") if isinstance(body, dict) else ""
            raise ResearchApiError(f"토큰 발급 실패: http={r.status_code} return_msg={msg}")
        self._token = str(token)

    # ── 조회 ──
    def _pace(self) -> None:
        if self._last_call is not None:
            wait = self.min_interval_sec - (self.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.monotonic()

    def _post_once(self, api_id: str, payload: dict, cont_yn: str, next_key: str,
                   deadline: datetime | None = None, sent: int = 0) -> tuple[int, dict, Any, datetime]:
        import requests
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self._token}",
            "cont-yn": cont_yn, "next-key": next_key, "api-id": api_id,
        }
        self._pace()
        requested_at = self.now()
        if deadline is not None and requested_at >= deadline:      # 대기·재시도 뒤 실제 요청 직전에 검사
            raise DeadlinePassed(f"{api_id}: 요청 직전 {requested_at} ≥ 마감 {deadline} — 요청하지 않음", sent)
        self.calls += 1
        try:
            r = self.session.post(f"{self.base_url}{_ALL_API[api_id]}", headers=headers, json=payload, timeout=15)
        except requests.RequestException as exc:
            raise _Retryable(f"전송 실패 {type(exc).__name__}") from exc
        resp_headers = {k: str(r.headers.get(k, "") or "").strip() for k in ("cont-yn", "next-key")}
        try:
            body = r.json()
        except ValueError:
            body = None
        return r.status_code, resp_headers, body, requested_at

    def _request(self, api_id: str, payload: dict, cont_yn: str, next_key: str,
                 deadline: datetime | None = None) -> tuple:
        """(status, 응답 헤더, 본문, 요청 시각, 수신 시각, 시도 횟수). 429·전송 실패 재시도, 401은 한 번 재인증.
        deadline이 있으면 매 요청 직전에 검사해 지났으면 DeadlinePassed(요청하지 않음)."""
        if not self._token:
            self.authenticate()
        attempt, reauthed, calls0 = 0, False, self.calls
        while True:
            try:
                status, h, body, requested_at = self._post_once(api_id, payload, cont_yn, next_key, deadline,
                                                                self.calls - calls0)
                if status == 429:
                    raise _Retryable("HTTP 429")
                if status == 401 and not reauthed:
                    reauthed = True
                    self.log(f"[RESEARCH] {api_id} HTTP 401 — 재인증 후 재시도")
                    self.authenticate()
                    continue
                break
            except _Retryable as exc:
                if attempt >= len(self.retry_backoff_sec):
                    raise ResearchApiError(f"{api_id} {payload}: 재시도 {attempt}회 후에도 실패 — {exc}") from exc
                wait = self.retry_backoff_sec[attempt]
                attempt += 1
                self.retries += 1
                self.log(f"[RESEARCH] {api_id} {exc} — {wait}초 후 재시도 ({attempt}/{len(self.retry_backoff_sec)})")
                self.sleep(wait)
        return status, h, body, requested_at, self.now(), self.calls - calls0

    @staticmethod
    def _check_ok(api_id: str, payload: dict, status: int, body: Any) -> None:
        if status != 200 or not isinstance(body, dict):
            raise ResearchApiError(f"{api_id} {payload}: HTTP {status}")
        rc = body.get("return_code")
        if rc is None or isinstance(rc, bool) or str(rc).strip() != "0":
            raise ResearchApiError(f"{api_id} {payload}: return_code={rc!r} {body.get('return_msg', '')}")

    def fetch_body(self, api_id: str, payload: dict, *, not_after: datetime | None = None) -> Body:
        """목록이 아닌 한 건짜리 조회(A5-1 시세). return_code가 있고 0이어야 성공 — 아니면 ResearchApiError.
        not_after(정규장 종료 등)가 있으면 인증·호출 간격·재시도 대기 뒤 **요청 직전마다** 검사해 지났으면
        DeadlinePassed. 응답 수신 시각 검증은 호출한 쪽에서(Body.received_at)."""
        assert_mock_domain(self.base_url)
        if api_id not in PRICE_API:
            raise ResearchConfigError(f"가격 기록에 허용되지 않은 api-id: {api_id}")
        status, _h, body, requested_at, received_at, tries = self._request(api_id, payload, "N", "", not_after)
        self._check_ok(api_id, payload, status, body)
        return Body(body, requested_at, received_at, tries)

    def fetch_page(self, api_id: str, payload: dict, list_key: str,
                   cont_yn: str = "N", next_key: str = "") -> Page:
        assert_mock_domain(self.base_url)
        if api_id not in RESEARCH_API:
            raise ResearchConfigError(f"연구 수집에 허용되지 않은 api-id: {api_id}")
        status, h, body, _req, received_at, _tries = self._request(api_id, payload, cont_yn, next_key)
        if status != 200 or not isinstance(body, dict):
            raise ResearchApiError(f"{api_id} {payload}: HTTP {status}")
        rc = body.get("return_code")
        if rc is None or isinstance(rc, bool) or str(rc).strip() != "0":
            # A2-R3: 성공 코드(0)가 있어야 성공. 없거나 다르면 오류 (누락을 성공으로 보지 않음)
            raise ResearchApiError(f"{api_id} {payload}: return_code={rc!r} {body.get('return_msg', '')}")
        rows = body.get(list_key)
        if not isinstance(rows, list):
            raise ResearchApiError(f"{api_id} {payload}: 응답에 {list_key} 목록 없음")
        cont = h.get("cont-yn", "").strip().upper()
        nkey = h.get("next-key", "").strip()
        if cont not in ("Y", "N"):
            raise ResearchApiError(f"{api_id} {payload}: cont-yn 헤더가 Y/N이 아님 ({cont!r})")
        if cont == "Y" and not nkey:
            # A2-R3: 이어진다고 하면서 다음 키가 없음 → '이력 끝'으로 해석하지 않고 오류
            raise ResearchApiError(f"{api_id} {payload}: cont-yn=Y인데 next-key 없음")
        return Page(rows, cont, nkey if cont == "Y" else "", received_at)
