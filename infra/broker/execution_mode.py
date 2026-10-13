from __future__ import annotations

"""실행 모드와 키움 모의투자 전용 브로커 (M0 커밋 1, GPT 지시서 2026-10-13).

모드는 두 가지뿐입니다. 이 단계에서 실전 주소·실전 플래그는 **기동 거부**입니다.

| 모드 | 조건 | 브로커 |
|---|---|---|
| MOCK_LOCAL | `use_mock: true` | 프로그램 안 가짜 `MockBroker` (키움 서버에 닿지 않음) |
| KIWOOM_PAPER | `use_mock: false` ∧ `is_paper_trading: true` ∧ `base_url == https://mockapi.kiwoom.com` | `PaperKiwoomBroker` |
| (거부) | 그 밖 — 실전 주소, http, 다른 호스트·경로·포트, paper 플래그 false | `ExecutionModeError` |

`PaperKiwoomBroker`는 `KiwoomBroker`를 그대로 상속하고(단타 원본과 같은 경로의 코드는 바꾸지 않음) 두 가지만 더합니다.

- 모든 HTTP 요청의 주소를 요청 직전에 다시 검사(https·mockapi.kiwoom.com·기본 포트)하고 리다이렉트를 따라가지 않음
  (`allow_redirects=False` — 3xx는 비정상 응답으로 위로 올라가 주문은 '응답 불명'으로 처리됨).
- 주문 전송 직전에 설정·세션을 다시 검사.

계좌 식별: 앱키·시크릿·계좌번호가 있어야 하고, 계좌 라벨(`account_scope_id`)은 비어 있거나 단타 라벨(`acct-a`)이면 거부.
모의 서버 토큰은 모의투자 앱키로만 발급되므로, 기동 때 mockapi 인증 + 잔고 조회 성공이 '모의 계좌' 확인의 실측 근거입니다
(계좌번호 자체로 모의/실전을 구분하는 규칙은 확인하지 못해 쓰지 않음).
"""

from urllib.parse import urlsplit

import requests

from infra.broker.kiwoom_broker import KiwoomBroker

MOCK_LOCAL = "MOCK_LOCAL"
KIWOOM_PAPER = "KIWOOM_PAPER"
PAPER_BASE_URL = "https://mockapi.kiwoom.com"
PAPER_HOST = "mockapi.kiwoom.com"
DAYTRADE_SCOPE_IDS = frozenset({"acct-a"})

MODE_LABELS = {MOCK_LOCAL: "로컬 가짜 브로커(MOCK_LOCAL)", KIWOOM_PAPER: "키움 모의투자(KIWOOM_PAPER)"}


class ExecutionModeError(RuntimeError):
    """허용되지 않는 실행 모드·주소·계좌 설정 — 브로커를 만들지 않음."""


def _url_problem(url: str) -> str:
    try:
        p = urlsplit(str(url))
    except ValueError as exc:
        return f"주소 해석 실패: {exc}"
    if p.scheme != "https":
        return f"https가 아님({p.scheme or '-'})"
    if (p.hostname or "").lower() != PAPER_HOST:
        return f"호스트 {p.hostname!r} ≠ {PAPER_HOST}"
    try:
        port = p.port
    except ValueError:
        return "포트 해석 실패"
    if port not in (None, 443):
        return f"포트 {port}"
    if p.username or p.password:
        return "주소에 사용자 정보"
    return ""


def resolve_execution_mode(broker_cfg) -> str:
    """설정 → 모드. 허용되지 않는 조합이면 ExecutionModeError."""
    if broker_cfg.use_mock:
        return MOCK_LOCAL
    if not broker_cfg.is_paper_trading:
        raise ExecutionModeError("is_paper_trading=false — 이 단계는 키움 모의투자만 허용(실전 거부)")
    base = str(broker_cfg.base_url or "")
    if base.rstrip("/") != PAPER_BASE_URL:
        why = _url_problem(base) or "경로가 붙어 있음"
        raise ExecutionModeError(f"base_url {base!r} ≠ {PAPER_BASE_URL} ({why}) — 모의투자 주소만 허용")
    return KIWOOM_PAPER


def paper_identity_problems(broker_cfg) -> list[str]:
    out = []
    if not str(broker_cfg.app_key or "").strip() or not str(broker_cfg.secret_key or "").strip():
        out.append("앱키/시크릿 없음(.env)")
    acct = str(broker_cfg.account_number or "").replace("-", "").strip()
    if not acct:
        out.append("계좌번호 없음(.env)")
    elif not acct.isdigit() or len(acct) < 8:
        out.append("계좌번호 형식 아님(숫자 8자리 이상)")
    scope = str(getattr(broker_cfg, "account_scope_id", "") or "").strip()
    if not scope:
        out.append("account_scope_id 없음")
    elif scope in DAYTRADE_SCOPE_IDS:
        out.append(f"account_scope_id {scope!r}는 단타 계좌 라벨")
    return out


def mode_label(mode: str) -> str:
    return MODE_LABELS.get(mode, mode)


class PaperOnlySession(requests.Session):
    """요청마다 주소 검사 + 리다이렉트 금지."""

    max_redirects = 0

    def request(self, method, url, *args, **kwargs):
        why = _url_problem(url)
        if why:
            raise ExecutionModeError(f"모의투자 외 주소로 요청 거부: {why}")
        kwargs["allow_redirects"] = False
        return super().request(method, url, *args, **kwargs)


class PaperKiwoomBroker(KiwoomBroker):
    """키움 모의투자 전용 — 설정이 KIWOOM_PAPER가 아니면 만들어지지 않음."""

    execution_mode = KIWOOM_PAPER

    def __init__(self, config) -> None:
        if resolve_execution_mode(config) != KIWOOM_PAPER:
            raise ExecutionModeError("PaperKiwoomBroker는 use_mock=false 모의투자 설정에서만")
        problems = paper_identity_problems(config)
        if problems:
            raise ExecutionModeError("모의 계좌 설정 확인 실패: " + ", ".join(problems))
        super().__init__(config)
        self.session = PaperOnlySession()

    def _assert_paper(self) -> None:
        try:
            ok = resolve_execution_mode(self.config) == KIWOOM_PAPER
        except ExecutionModeError as exc:
            raise ExecutionModeError(f"주문 직전 검사: {exc}") from exc
        if not ok:
            raise ExecutionModeError("주문 직전 검사: 설정이 모의투자가 아님")
        if not isinstance(self.session, PaperOnlySession):
            raise ExecutionModeError("주문 직전 검사: HTTP 세션이 모의투자 전용이 아님")

    def place_order(self, order):
        self._assert_paper()
        return super().place_order(order)


def build_execution_broker(settings):
    """모드에 맞는 브로커. 거부 조합이면 ExecutionModeError(브로커 없음)."""
    mode = resolve_execution_mode(settings.broker)
    if mode == MOCK_LOCAL:
        from infra.broker.mock_broker import MockBroker
        return MockBroker()
    return PaperKiwoomBroker(settings.broker)
