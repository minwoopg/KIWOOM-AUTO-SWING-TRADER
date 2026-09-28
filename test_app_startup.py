# -*- coding: utf-8 -*-
"""app/main.py 기반 점검 모드 회귀 테스트 (스윙 분리 1라운드, 2026-09-28).

주문을 내지 않는 기동 경로(인증 → 잔고 → 미해결 주문 흔적 확인)만
검증합니다. 모든 파일 경로는 tmpdir 기준입니다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from app.main import StartupBlockedError, retry_on_429, run_startup_checks
from domain.models import RuntimeState
from infra.broker.mock_broker import MockBroker
from infra.storage.state_store import JsonStateStore
from infra.storage.tracked_order_journal import TrackedOrderJournalStore, TrackedOrderRecord
from testing_helpers import build_minimal_settings

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


async def _no_sleep(_seconds):
    return None


def _run(settings, broker, logger=None):
    return asyncio.run(run_startup_checks(settings, broker, logger or Mock(), sleep=_no_sleep))


def _live(settings):
    return dataclasses.replace(
        settings, broker=dataclasses.replace(settings.broker, is_paper_trading=False)
    )


class _BalanceFailBroker(MockBroker):
    def get_account_balance(self):
        raise RuntimeError("balance down")


class _RateLimitedOnceBroker(MockBroker):
    def __init__(self):
        super().__init__()
        self.auth_calls = 0

    def authenticate(self):
        self.auth_calls += 1
        if self.auth_calls == 1:
            raise RuntimeError("kiwoom request failed: http=429")


class _PlaceOrderSpy(MockBroker):
    def __init__(self):
        super().__init__()
        self.orders = 0

    def place_order(self, order):
        self.orders += 1
        return super().place_order(order)


# ── 1. 정상 기동 ────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    spy = _PlaceOrderSpy()
    r = _run(build_minimal_settings(tmp), spy)
    check("1-1) 모의투자 정상 기동 시 잔고가 채워짐", r.balance is not None and r.balance.cash == 1_000_000)
    check("1-2) 미해결 주문 흔적 없음", not r.has_unresolved_orders)
    check("1-3) 기동 점검은 주문을 전혀 내지 않음", spy.orders == 0)
    check("1-4) 상태 파일/저널이 없어도 새로 만들지 않음(읽기 전용)",
          not Path(tmp, "state.json").exists() and not Path(tmp, "tracked_order_journal.json").exists())

# ── 2. 429 재시도 ───────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    b = _RateLimitedOnceBroker()
    r = _run(build_minimal_settings(tmp), b)
    check("2-1) 인증 429 1회 후 재시도로 성공", b.auth_calls == 2 and r.balance is not None)

sleeps = []


async def _record_sleep(s):
    sleeps.append(s)


def _always_429():
    raise RuntimeError("http=429")


try:
    asyncio.run(retry_on_429(_always_429, "x", Mock(), max_retries=3, sleep=_record_sleep))
    check("2-2) 재시도 초과 시 예외", False)
except RuntimeError as e:
    check("2-2) 재시도 초과 시 예외", "최대 재시도 초과" in str(e))
check("2-3) 대기 시간 규칙이 단타 레포와 동일(30초씩 증가)", sleeps == [30, 60, 90])


def _other_error():
    raise ValueError("boom")


try:
    asyncio.run(retry_on_429(_other_error, "x", Mock(), sleep=_record_sleep))
    check("2-4) 429가 아닌 오류는 재시도 없이 그대로 전파", False)
except ValueError:
    check("2-4) 429가 아닌 오류는 재시도 없이 그대로 전파", True)

# ── 3. 잔고 조회 실패 ───────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    r = _run(build_minimal_settings(tmp), _BalanceFailBroker())
    check("3-1) 모의투자: 잔고 실패는 경고 후 계속", r.balance is None and "balance down" in r.balance_error)
    try:
        _run(_live(build_minimal_settings(tmp)), _BalanceFailBroker())
        check("3-2) 실전투자: 잔고 실패 시 시작 중단", False)
    except StartupBlockedError:
        check("3-2) 실전투자: 잔고 실패 시 시작 중단", True)

# ── 4. 이전 프로세스의 미해결 주문 흔적 ─────────────────────
with tempfile.TemporaryDirectory() as tmp:
    s = build_minimal_settings(tmp)
    st = RuntimeState()
    st.unresolved_order_intents["005930"] = {"side": "BUY", "quantity": 1, "order_id": "", "created_at": "x"}
    JsonStateStore(s.storage.state_file).save(st, {})
    TrackedOrderJournalStore(s.storage.tracked_order_journal_file).upsert(TrackedOrderRecord(
        symbol="000660", side="SELL", order_id="0012345",
        base_quantity_before_order=10, target_quantity_after_order=0,
        accepted_at=datetime.now(), lifecycle_kind="SELL_PENDING",
    ))
    logger = Mock()
    r = _run(s, MockBroker(), logger)
    check("4-1) state.json의 주문 의도가 보고됨", r.unresolved_intent_symbols == ["005930"])
    check("4-2) 저널의 추적 주문이 보고됨", r.journal_symbols == ["000660"])
    crit = " ".join(str(c.args[0]) for c in logger.critical.call_args_list)
    check("4-3) CRITICAL 로그로 사람 확인을 요구함", "[STARTUP_ORDER_RECOVERY]" in crit)
    check("4-4) 저널은 자동으로 지워지지 않음",
          list(TrackedOrderJournalStore(s.storage.tracked_order_journal_file).load_all()) == ["000660"])

# ── 5. 저널 손상 ─────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    s = build_minimal_settings(tmp)
    Path(s.storage.tracked_order_journal_file).write_text("{broken", encoding="utf-8")
    r = _run(s, MockBroker())
    check("5-1) 모의투자: 손상 저널은 오류로 보고하고 계속", bool(r.journal_error) and r.has_unresolved_orders)
    try:
        _run(_live(s), MockBroker())
        check("5-2) 실전투자: 손상 저널이면 시작 중단", False)
    except StartupBlockedError:
        check("5-2) 실전투자: 손상 저널이면 시작 중단", True)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
