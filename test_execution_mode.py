# -*- coding: utf-8 -*-
"""실행 모드·키움 모의투자 전용 브로커 (M0 커밋 1, GPT 지시서 2026-10-13).

네트워크에 나가지 않음 — requests.Session.request를 가짜로 바꿔 요청 인자만 기록합니다. 경로는 모두 임시 폴더.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from unittest.mock import Mock

import requests

sys.path.insert(0, ".")

from app.main import StartupBlockedError, build_broker, run_startup_checks  # noqa: E402
from app.session_runner import SessionSummary  # noqa: E402
from domain.models import OrderRequest, OrderSide  # noqa: E402
from infra.broker import execution_mode as EM  # noqa: E402
from infra.broker.mock_broker import MockBroker  # noqa: E402
from testing_helpers import build_minimal_settings  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = tempfile.mkdtemp(prefix="exec_mode_")
BASE = build_minimal_settings(TMP)


def paper(**kw):
    b = dict(use_mock=False, is_paper_trading=True, base_url="https://mockapi.kiwoom.com",
             app_key="k", secret_key="s", account_number="12345678", account_scope_id="acct-swing")
    b.update(kw)
    return dataclasses.replace(BASE, broker=dataclasses.replace(BASE.broker, **b))


def mode_or_err(s):
    try:
        return EM.resolve_execution_mode(s.broker)
    except EM.ExecutionModeError as exc:
        return f"ERR:{exc}"


# 1. 모드 판정
ok = [mode_or_err(BASE), mode_or_err(paper()), mode_or_err(paper(base_url="https://mockapi.kiwoom.com/"))]
bad = {u: mode_or_err(paper(base_url=u)) for u in (
    "https://api.kiwoom.com", "http://mockapi.kiwoom.com", "https://mockapi.kiwoom.com.evil.example",
    "https://mockapi.kiwoom.com:8443", "https://mockapi.kiwoom.com/v2", "https://user@mockapi.kiwoom.com", "")}
check("1) use_mock=true → MOCK_LOCAL, mockapi https(끝 / 허용) → KIWOOM_PAPER, 실전·http·다른 호스트·포트·경로·사용자정보·빈 주소는 거부",
      ok == ["MOCK_LOCAL", "KIWOOM_PAPER", "KIWOOM_PAPER"] and all(v.startswith("ERR:") for v in bad.values()))
live = mode_or_err(paper(is_paper_trading=False))
check("2) use_mock=false ∧ is_paper_trading=false → 거부(실전 금지) — mockapi 주소여도",
      live.startswith("ERR:") and "실전" in live)

# 3. 계좌 식별
probs = {k: EM.paper_identity_problems(paper(**v).broker) for k, v in {
    "ok": {}, "nokey": {"app_key": ""}, "noacct": {"account_number": ""}, "badacct": {"account_number": "12ab"},
    "noscope": {"account_scope_id": " "}, "daytrade": {"account_scope_id": "acct-a"}}.items()}
check("3) 계좌 식별: 앱키·계좌번호(숫자 8자리 이상)·계좌 라벨 필수, 단타 라벨 acct-a 거부",
      probs["ok"] == [] and all(probs[k] for k in ("nokey", "noacct", "badacct", "noscope", "daytrade"))
      and any("단타" in p for p in probs["daytrade"]))

# 4. 브로커 생성
b_local = build_broker(BASE)
b_paper = build_broker(paper())
refused = []
for s in (paper(base_url="https://api.kiwoom.com"), paper(is_paper_trading=False), paper(account_scope_id="acct-a")):
    try:
        build_broker(s)
        refused.append(False)
    except EM.ExecutionModeError:
        refused.append(True)
check("4) build_broker: MOCK_LOCAL → MockBroker, KIWOOM_PAPER → PaperKiwoomBroker(세션 = 모의 전용), 실전·식별 실패 → 브로커 없음",
      type(b_local) is MockBroker and isinstance(b_paper, EM.PaperKiwoomBroker)
      and isinstance(b_paper.session, EM.PaperOnlySession) and refused == [True, True, True])

# 5. 요청 직전 주소 검사·리다이렉트 금지 (가짜 전송)
calls: list[dict] = []
_orig = requests.Session.request


def fake_request(self, method, url, *a, **kw):
    calls.append({"method": method, "url": url, "allow_redirects": kw.get("allow_redirects")})
    r = requests.Response()
    r.status_code = 302
    r.headers["Location"] = "https://api.kiwoom.com/api/dostk/ordr"
    r._content = b'{"return_code": 0, "ord_no": "0000001"}'
    r.url = url
    return r


requests.Session.request = fake_request
try:
    br = build_broker(paper())
    br.access_token = "t"
    res = br.place_order(OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=1, order_type="market"))
    n_redirect = len(calls)
    blocked = None
    try:
        br.session.post("https://api.kiwoom.com/oauth2/token", json={})
    except EM.ExecutionModeError as exc:
        blocked = str(exc)
    n_after_block = len(calls)
    # 설정이 바뀐 경우(비정상) 주문 직전 검사
    object.__setattr__(br.config, "base_url", "https://api.kiwoom.com")
    pre = None
    try:
        br.place_order(OrderRequest(symbol="005930", side=OrderSide.BUY, quantity=1, order_type="market"))
    except EM.ExecutionModeError as exc:
        pre = str(exc)
finally:
    requests.Session.request = _orig
check("5) 주문 요청은 allow_redirects=False 한 번뿐 — 3xx(실전 주소로 리다이렉트)를 따라가지 않고 '응답 불명' 미접수로 반환",
      n_redirect == 1 and calls[0]["allow_redirects"] is False and calls[0]["url"].startswith("https://mockapi.kiwoom.com/")
      and res.accepted is False and res.is_ambiguous is True)
check("6) 모의 전용 세션은 다른 호스트 요청을 보내기 전에 거부, 설정이 실전 주소로 바뀌면 주문 직전 검사가 전송 없이 거부",
      blocked is not None and n_after_block == n_redirect and pre is not None and "주문 직전 검사" in pre
      and len(calls) == n_redirect)


# 7. 기동 엄격 기준
class _PaperBalanceFail(EM.PaperKiwoomBroker):
    def authenticate(self):
        self.access_token = "t"

    def get_account_balance(self):
        raise RuntimeError("balance down")


async def _no_sleep(_s):
    return None


def startup(settings, broker):
    try:
        return asyncio.run(run_startup_checks(settings, broker, Mock(), sleep=_no_sleep))
    except StartupBlockedError as exc:
        return f"BLOCK:{exc}"


ps = paper(**{})
ps = dataclasses.replace(ps, storage=BASE.storage)
r_fail = startup(ps, _PaperBalanceFail(ps.broker))
r_wrong = startup(ps, MockBroker())
r_local = startup(BASE, type("B", (MockBroker,), {"get_account_balance": lambda self: (_ for _ in ()).throw(RuntimeError("x"))})())
check("7) KIWOOM_PAPER는 잔고 조회 실패면 시작 거부(실전과 같은 엄격 기준), 브로커가 모의 전용이 아니면 거부 — MOCK_LOCAL은 예전처럼 경고 후 계속",
      isinstance(r_fail, str) and r_fail.startswith("BLOCK:") and "잔고" in r_fail
      and isinstance(r_wrong, str) and "PaperKiwoomBroker" in r_wrong
      and not isinstance(r_local, str) and r_local.execution_mode == "MOCK_LOCAL" and "MOCK_LOCAL" in r_local.mode)

# 8. 보고서 표시
sm = SessionSummary(status="COMPLETED", execution_mode="KIWOOM_PAPER", account_scope="acct-swing")
d = sm.to_status_dict()
check("8) 세션 요약 줄·상태 파일에 실행 모드와 계좌 라벨(계좌번호 아님) 표시",
      sm.lines()[0] == "실행 모드 KIWOOM_PAPER | 계좌 라벨 acct-swing"
      and d["execution_mode"] == "KIWOOM_PAPER" and d["account_scope"] == "acct-swing" and "12345678" not in str(d))

# 9. 취소 주문 kt10003 (응답 형식 미실측 — 요청 모양과 실패 분류만)
sent9 = []


def fake9(self, method, url, *a, **kw):
    sent9.append({"url": url, "json": kw.get("json"), "api": (kw.get("headers") or {}).get("api-id")})
    r = requests.Response()
    r.status_code = 200 if len(sent9) == 1 else 500
    r._content = (b'{"return_code": 0, "ord_no": "0000009", "return_msg": "ok"}' if len(sent9) == 1 else b'{"x":1}')
    r.url = url
    return r


requests.Session.request = fake9
try:
    br9 = build_broker(paper())
    br9.access_token = "t"
    c1 = br9.cancel_order("0112293", "005930", 1)
    c2 = br9.cancel_order("0112293", "005930", 1)
finally:
    requests.Session.request = _orig
check("9) 취소: kt10003 · /api/dostk/ordr · {KRX, 원주문번호, 종목, 남은 수량 명시} — 200·return_code 0이면 접수(완료 아님), "
      "HTTP 오류는 응답 불명",
      sent9[0]["api"] == "kt10003" and sent9[0]["url"].endswith("/api/dostk/ordr")
      and sent9[0]["json"] == {"dmst_stex_tp": "KRX", "orig_ord_no": "0112293", "stk_cd": "005930", "cncl_qty": "1"}
      and c1.accepted and not c1.is_ambiguous and c1.cancel_order_id == "0000009"
      and not c2.accepted and c2.is_ambiguous)

import shutil  # noqa: E402

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
