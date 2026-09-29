# -*- coding: utf-8 -*-
"""OrderExecutor 회귀 테스트 (스윙 분리 2라운드, 2026-09-28).

단타 레포에서 TradingService를 통해 검증하던 주문 안전 시나리오를
OrderExecutor 기준으로 다시 작성했습니다. 근거 사건 번호는 원본 테스트
(test_partial_fill_lifecycle / test_order_status_reconciliation /
test_tracked_order_journal 6~12절 / test_order_status_evidence_observation)의
것을 그대로 표기합니다.

1. BUY 접수 → 부분체결 → 전량체결 (047040), 첫 체결 훅 1회
2. 기존 보유분이 있는 추가 매수
3. BUY 거부 → 롤백, 주문 의도 정리
4. BUY 응답 없음(ambiguous) → ERROR, 계좌 전체 신규매수 차단, ack_error로만 해제 (319400)
5. place_order 예외 → ERROR 후 예외 전파
6. SELL 부분체결 → 청산 훅 없음 → 전량 → 훅 1회
7. BUY_PENDING 중 SELL은 강제여도 차단 (006360/017900)
8. 강제 매도 거부 카운터 / 성공 시 리셋
9. 재시작 — 미해결 주문을 ERROR로 복원, 추측으로 해제하지 않음
10. 저널 손상 / 주문 의도 기록 실패 → 주문 차단
11. 체결 조회 — 나이 게이트, FILLED+잔고일치만 전환, OPEN/UNKNOWN/예외는 변화 없음, 폴링당 1건
12. 추적 불가능한 order_id는 조회하지 않음
13. 관측 기록기 — 조회마다 기록, 기록 실패가 판정에 영향 없음
14. 훅 예외가 대조 루프를 끊지 않음
15. ack_orphan 명령 — 보류 컨텍스트 폐기, 훅 미호출
16. 저널 I/O 실패가 주문을 막지 않음
17. 거래 로그 / 영구 거부 키워드
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from domain.models import BrokerOrderStatus
from domain.position.lifecycle import PositionLifecycle as L
from domain.service.order_executor import OrderExecutor, is_permanent_buy_reject
from infra.storage.logger import TradeCsvLogger
from infra.storage.state_store import JsonStateStore
from infra.storage.tracked_order_journal import TrackedOrderJournalStore
from testing_helpers import ScriptedBroker, build_minimal_settings

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


class Env:
    """executor + 훅 기록 + 편의 메서드."""

    def __init__(self, tmpdir=None, broker=None, recorder=None, fill_hook=None, close_hook=None,
                 journal=None):
        self.tmp = tmpdir or tempfile.mkdtemp()
        self.settings = build_minimal_settings(self.tmp)
        self.broker = broker or ScriptedBroker()
        self.fills: list[tuple] = []
        self.closes: list[tuple] = []
        self.logger = Mock()
        self.state_store = JsonStateStore(self.settings.storage.state_file)
        state, hp = self.state_store.load()
        self.cmd_dir = Path(self.tmp) / "commands"
        self.ex = OrderExecutor(
            settings=self.settings, broker=self.broker, state=state, highest_price=hp,
            state_store=self.state_store, app_logger=self.logger,
            trade_logger=TradeCsvLogger(self.settings.storage.trade_log_file),
            tracked_order_journal=journal,
            order_status_observation_recorder=recorder,
            on_first_fill_buy=fill_hook or (lambda s, q, c: self.fills.append((s, q, c))),
            on_sell_closed=close_hook or (lambda s, c: self.closes.append((s, c))),
            commands_dir=self.cmd_dir,
        )

    def sync(self):
        self.ex.sync_with_balance(self.broker.get_account_balance())

    def st(self, sym):
        return self.ex.position_state_machine.get(sym)

    def age(self, sym, seconds):
        st = self.st(sym)
        for attr in ("pending_since", "partial_fill_since", "requested_at", "orphan_since"):
            if getattr(st, attr):
                setattr(st, attr, getattr(st, attr) - timedelta(seconds=seconds))

    def journal(self):
        return TrackedOrderJournalStore(self.settings.storage.tracked_order_journal_file).load_all()

    def critical_text(self):
        return " ".join(str(c.args[0]) for c in self.logger.critical.call_args_list)


# ── 1. BUY 접수 → 부분체결 → 전량체결 (047040) ─────────────────
e = Env()
e.sync()
sub = e.ex.submit_buy("047040", 353, 2832, context={"entry_strategy": "swing_test"})
check("1-1) 접수 성공 시 block_code 없음 / accepted", sub.block_code == "" and sub.accepted)
check("1-2) BUY_PENDING 진입", e.st("047040").lifecycle == L.BUY_PENDING)
check("1-3) 실제 주문번호가 PSM에 연결됨", e.st("047040").pending_order_id == "0000001")
check("1-4) 주문 의도가 state.json에 저장됨",
      "047040" in JsonStateStore(e.settings.storage.state_file).load()[0].unresolved_order_intents)
check("1-5) 저널에 order_id·목표수량 기록", e.journal()["047040"].order_id == "0000001"
      and e.journal()["047040"].target_quantity_after_order == 353)
check("1-6) 접수만으로는 첫 체결 훅 없음", e.fills == [])
e.broker.positions["047040"] = 146
e.sync()
check("1-7) 부분체결 146주 → BUY_PENDING 유지", e.st("047040").lifecycle == L.BUY_PENDING)
check("1-8) 첫 실체결에서 훅 1회, 체결수량 146", len(e.fills) == 1 and e.fills[0][1] == 146)
check("1-9) 훅 ctx에 호출부 context와 기준가·요청수량이 실림",
      e.fills[0][2]["entry_strategy"] == "swing_test" and e.fills[0][2]["current_price"] == 2832
      and e.fills[0][2]["quantity"] == 353)
e.sync()
e.broker.positions["047040"] = 353
e.sync()
check("1-10) 353 도달 시 OPEN", e.st("047040").lifecycle == L.OPEN)
check("1-11) 첫 체결 훅은 전체 과정에서 1회뿐", len(e.fills) == 1)
check("1-12) 확정 후 저널 삭제", "047040" not in e.journal())
check("1-13) 확정 후 주문 의도 정리", "047040" not in e.ex.state.unresolved_order_intents)
check("1-14) 미해결 주문 없음", not e.ex.has_unresolved_orders())

# ── 2. 기존 보유분이 있는 추가 매수 ─────────────────────────────
e = Env()
e.broker.positions["005930"] = 100
e.sync()
e.ex.submit_buy("005930", 50, 70000)
check("2-1) 목표수량 = 기존 100 + 50", e.st("005930").expected_final_quantity == 150)
e.sync()
check("2-2) 잔고가 그대로면 훅 없음", e.fills == [])
e.broker.positions["005930"] = 120
e.sync()
check("2-3) 늘어난 수량(20)만 첫 체결로 전달", e.fills and e.fills[0][1] == 20)

# ── 3. BUY 거부 ─────────────────────────────────────────────────
e = Env()
e.sync()
e.broker.next_result.append("reject")
sub = e.ex.submit_buy("005930", 10, 70000)
check("3-1) 거부 시 sent이지만 accepted 아님", sub.sent and not sub.accepted and sub.block_code == "")
check("3-2) 거부 후 FLAT 롤백", e.st("005930").lifecycle == L.FLAT)
check("3-3) 주문 의도 정리", "005930" not in e.ex.state.unresolved_order_intents)
check("3-4) 저널 기록 없음", "005930" not in e.journal())
check("3-5) 영구 거부 키워드 판별(RC4007)", is_permanent_buy_reject(sub.result.message))
check("3-6) 일반 메시지는 영구 거부 아님", not is_permanent_buy_reject("잔고 부족"))

# ── 4. 응답 없음(ambiguous) — 319400 ───────────────────────────
e = Env()
e.sync()
e.broker.next_result.append("ambiguous")
sub = e.ex.submit_buy("005930", 10, 70000)
check("4-1) ambiguous → block_code ORDER_PLACEMENT_AMBIGUOUS", sub.block_code == "ORDER_PLACEMENT_AMBIGUOUS")
check("4-2) ERROR 전이", e.st("005930").lifecycle == L.ERROR)
check("4-3) 주문 의도는 남음(롤백 안 함)", "005930" in e.ex.state.unresolved_order_intents)
check("4-4) CRITICAL 로그", "[ORDER_PLACEMENT_AMBIGUOUS]" in e.critical_text())
n_calls = len(e.broker.place_calls)
check("4-5) 다른 종목 신규매수도 차단(ACCOUNT_ORDER_UNRESOLVED)",
      e.ex.submit_buy("000660", 5, 180000).block_code == "ACCOUNT_ORDER_UNRESOLVED")
check("4-6) 같은 종목 강제 매도도 차단", e.ex.submit_sell("005930", 1, forced=True).block_code != "")
check("4-7) 차단된 요청은 브로커에 전송되지 않음", len(e.broker.place_calls) == n_calls)
e.sync()
check("4-8) 잔고 0이어도 ERROR 유지(자동 회복 없음)", e.st("005930").lifecycle == L.ERROR)
e.cmd_dir.mkdir(exist_ok=True)
(e.cmd_dir / "ack_error_005930.json").write_text(
    json.dumps({"broker_quantity": 0, "note": "HTS 확인",
                "recovery_id": e.ex.recovery_id("005930", "ERROR")}), encoding="utf-8")
e.sync()
check("4-9) ack_error 명령으로만 해제", e.st("005930").lifecycle != L.ERROR)
check("4-10) 명령 파일은 처리 후 processed/로 이동",
      not (e.cmd_dir / "ack_error_005930.json").exists()
      and len(list((e.cmd_dir / "processed").glob("ack_error_005930.*.json"))) == 1)
check("4-11) 해제 후 매수 가능", e.ex.submit_buy("000660", 5, 180000).accepted)

# ── 5. place_order 예외 ────────────────────────────────────────
e = Env()
e.sync()
e.broker.next_result.append("raise")
try:
    e.ex.submit_buy("005930", 10, 70000)
    check("5-1) 예외가 호출부로 전파", False)
except RuntimeError:
    check("5-1) 예외가 호출부로 전파", True)
check("5-2) 예외 후 ERROR 상태", e.st("005930").lifecycle == L.ERROR)

# ── 6. SELL 부분체결 → 전량 (047040) ────────────────────────────
e = Env()
e.broker.positions["047040"] = 353
e.sync()
sub = e.ex.submit_sell("047040", 353, 9000, exit_reason="stop", avg_buy_price=10000,
                       context={"entry_strategy": "swing_test"})
check("6-1) SELL 접수 → SELL_PENDING", sub.accepted and e.st("047040").lifecycle == L.SELL_PENDING)
check("6-2) SELL도 저널 기록(목표 0)", e.journal()["047040"].target_quantity_after_order == 0)
e.broker.positions["047040"] = 343
e.sync()
check("6-3) 부분체결(343 남음)에는 청산 훅 없음", e.closes == [])
check("6-4) 부분체결 중 재매도 차단(HARD)", e.ex.submit_sell("047040", 343).block_code != "")
e.broker.positions["047040"] = 0
e.sync()
check("6-5) 잔고 0 → FLAT", e.st("047040").lifecycle == L.FLAT)
check("6-6) 완전 청산 훅 1회", len(e.closes) == 1)
ctx = e.closes[0][1]
check("6-7) 청산 ctx에 사유·평균단가·기준가·forced·context 포함",
      ctx["exit_reason"] == "stop" and ctx["avg_buy_price"] == 10000 and ctx["current_price"] == 9000
      and ctx["forced"] is False and ctx["entry_strategy"] == "swing_test")
e.sync()
check("6-8) 이후 폴링에서 훅 재실행 없음", len(e.closes) == 1)
check("6-9) 저널·주문 의도 정리", "047040" not in e.journal()
      and "047040" not in e.ex.state.unresolved_order_intents)

# ── 7. BUY_PENDING 중 SELL (006360/017900) ─────────────────────
e = Env()
e.sync()
e.ex.submit_buy("006360", 174, 5000)
e.broker.positions["006360"] = 7
e.sync()
n_calls = len(e.broker.place_calls)
check("7-1) BUY_PENDING 중 일반 SELL 차단", e.ex.submit_sell("006360", 7).block_code != "")
check("7-2) BUY_PENDING 중 강제 SELL도 차단", e.ex.submit_sell("006360", 7, forced=True).block_code != "")
check("7-3) 차단된 SELL은 전송되지 않음", len(e.broker.place_calls) == n_calls)

# ── 8. 강제 매도 거부 카운터 ───────────────────────────────────
e = Env()
e.broker.positions["005930"] = 10
e.sync()
e.broker.next_result.append("reject")
e.ex.submit_sell("005930", 10, forced=True, exit_reason="stop")
check("8-1) 강제 매도 거부 1회 기록", e.ex.forced_sell_failure_count("005930") == 1)
check("8-2) [FORCED_SELL_FAILED] CRITICAL", "[FORCED_SELL_FAILED]" in e.critical_text())
sub = e.ex.submit_sell("005930", 10, forced=False, exit_reason="normal")
check("8-3) 거부 직후 일반 매도는 재시도 백오프로 차단(SOFT), 카운터 불변",
      sub.block_code == "BLOCK_SELL_RETRY_BACKOFF" and e.ex.forced_sell_failure_count("005930") == 1)
e.age("005930", 400)  # 재시도 백오프/최소간격 경과
st = e.st("005930")
st.last_forced_sell_at = None
st.sell_reject_last_at = (st.sell_reject_last_at or datetime.now()) - timedelta(seconds=400)
sub = e.ex.submit_sell("005930", 10, forced=True, exit_reason="stop")
check("8-4) 강제 매도 성공 시 카운터 리셋", sub.accepted and e.ex.forced_sell_failure_count("005930") == 0)

# ── 9. 재시작 복구 ─────────────────────────────────────────────
tmp = tempfile.mkdtemp()
e1 = Env(tmp)
e1.sync()
e1.ex.submit_buy("005930", 10, 70000)
broker = e1.broker
e2 = Env(tmp, broker=broker)
check("9-1) 재시작 후 미해결 주문 종목이 ERROR로 복원", e2.st("005930").lifecycle == L.ERROR)
check("9-2) 복원된 주문번호 유지", e2.st("005930").pending_order_id == "0000001")
check("9-3) [STARTUP_ORDER_RECOVERY] CRITICAL", "[STARTUP_ORDER_RECOVERY]" in e2.critical_text())
check("9-4) 재시작 후 다른 종목 신규매수 차단",
      e2.ex.submit_buy("000660", 5, 180000).block_code == "ACCOUNT_ORDER_UNRESOLVED")
broker.positions["005930"] = 10
e2.sync()
e2.sync()
check("9-5) 잔고가 목표와 같아도 추측으로 해제하지 않음", e2.st("005930").lifecycle == L.ERROR)
check("9-6) has_unresolved_orders", e2.ex.has_unresolved_orders())

# ── 10. 저널 손상 / 의도 기록 실패 ────────────────────────────
tmp = tempfile.mkdtemp()
s = build_minimal_settings(tmp)
Path(s.storage.tracked_order_journal_file).write_text("{broken", encoding="utf-8")
e = Env(tmp)
check("10-1) 손상 저널 → recovery_failed", e.ex.recovery_failed)
check("10-2) 매수 차단 ORDER_RECOVERY_UNAVAILABLE",
      e.ex.submit_buy("005930", 1, 70000).block_code == "ORDER_RECOVERY_UNAVAILABLE")
check("10-3) 매도도 차단", e.ex.submit_sell("005930", 1).block_code == "ORDER_RECOVERY_UNAVAILABLE")
check("10-4) 브로커 전송 없음", e.broker.place_calls == [])

e = Env()
e.sync()
e.ex.state_store = Mock()
e.ex.state_store.save.side_effect = OSError("disk full")
sub = e.ex.submit_buy("005930", 10, 70000)
check("10-5) 의도 기록 실패 → ORDER_INTENT_WRITE_FAILED", sub.block_code == "ORDER_INTENT_WRITE_FAILED")
check("10-6) 의도 기록 실패 시 전송 안 함", e.broker.place_calls == [])
check("10-7) 이후 주문 전체 차단(recovery_failed)", e.ex.recovery_failed)

# ── 11. 체결 조회 ─────────────────────────────────────────────
e = Env()
e.sync()
e.ex.submit_buy("005930", 10, 70000)
e.sync()
check("11-1) 대기 30초 미만이면 조회 안 함", e.broker.status_calls == [])
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.OPEN
e.sync()
check("11-2) 30초 이상이면 조회 1회", e.broker.status_calls == [("005930", "0000001")])
check("11-3) OPEN → 상태 변화 없음", e.st("005930").lifecycle == L.BUY_PENDING)
e.sync()
check("11-4) 같은 종목 재조회 최소간격(30초) 준수", len(e.broker.status_calls) == 1)
e.ex._last_order_status_query_at["005930"] -= timedelta(seconds=31)
e.broker.status["0000001"] = RuntimeError("api down")
e.sync()
check("11-5) 조회 예외 → 상태 유지", e.st("005930").lifecycle == L.BUY_PENDING and len(e.broker.status_calls) == 2)

e = Env()
e.broker.positions.update({"005930": 10, "000660": 5})
e.sync()
e.ex.submit_sell("005930", 10)
e.ex.submit_sell("000660", 5)
e.age("005930", 35)
e.age("000660", 40)
e.sync()
check("11-6) 폴링당 조회 최대 1건 — 가장 오래 기다린 000660",
      e.broker.status_calls == [("000660", "0000002")])
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.broker.positions["005930"] = 3
e.sync()
check("11-7) 다음 폴링에서 나머지 종목 조회", e.broker.status_calls[-1] == ("005930", "0000001"))
check("11-8) FILLED여도 잔고 불일치면 SELL_PENDING 유지",
      e.st("005930").lifecycle == L.SELL_PENDING)

e = Env()
e.broker.positions["005930"] = 10
e.sync()
e.ex.submit_sell("005930", 10)
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.broker.positions["005930"] = 0
e.sync()
check("11-9) FILLED + 잔고 0 → FLAT", e.st("005930").lifecycle == L.FLAT)
check("11-10) 청산 훅 실행", len(e.closes) == 1)

# ── 12. 추적 불가능한 order_id ────────────────────────────────
e = Env()
e.sync()
e.broker.next_result.append("accept_noid")
e.ex.submit_buy("005930", 10, 70000)
check("12-1) 빈 주문번호 → [ORDER_ID_MISSING] CRITICAL", "[ORDER_ID_MISSING]" in e.critical_text())
check("12-2) 빈 주문번호는 저널에 남기지 않음", "005930" not in e.journal())
e.age("005930", 60)
e.sync()
check("12-3) 추적 불가 주문은 체결 조회 안 함", e.broker.status_calls == [])

# ── 13. 관측 기록기 ───────────────────────────────────────────
rec = Mock()
e = Env(recorder=rec)
e.sync()
e.ex.submit_buy("005930", 10, 70000)
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.UNKNOWN
e.sync()
check("13-1) 조회 1건당 관측 1건 기록", rec.record.call_count == 1)
obs = rec.record.call_args[0][0]
check("13-2) 관측에 주문번호·종류·저널 연결 여부",
      obs.requested_order_id == "0000001" and obs.query_kind == "BUY_PENDING" and obs.journal_linked)
rec2 = Mock()
rec2.record.side_effect = RuntimeError("disk")
e = Env(recorder=rec2)
e.broker.positions["005930"] = 10
e.sync()
e.ex.submit_sell("005930", 10)
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.broker.positions["005930"] = 0
e.sync()
check("13-3) 기록 실패해도 판정은 그대로(FLAT)", e.st("005930").lifecycle == L.FLAT)
check("13-4) 기록 실패는 CRITICAL로 남음", "[ORDER_STATUS_OBS_RECORD_FAILED]" in e.critical_text())
check("13-5) shutdown()은 기록기 종료를 호출", (e.ex.shutdown(), rec2.shutdown.called)[1])


# ── 14. 훅 예외 ───────────────────────────────────────────────
def _boom(*_a):
    raise ValueError("hook bug")


e = Env(fill_hook=_boom, close_hook=_boom)
e.sync()
e.ex.submit_buy("005930", 10, 70000)
e.broker.positions["005930"] = 10
e.sync()
check("14-1) 첫 체결 훅 예외 → CRITICAL, 상태는 OPEN 확정",
      "[FIRST_FILL_HOOK_FAILED]" in e.critical_text() and e.st("005930").lifecycle == L.OPEN)
check("14-2) 훅 예외 후에도 저널·의도 정리 진행", "005930" not in e.journal()
      and "005930" not in e.ex.state.unresolved_order_intents)
e.ex.submit_sell("005930", 10)
e.broker.positions["005930"] = 0
e.sync()
check("14-3) 청산 훅 예외 → CRITICAL, FLAT 확정",
      "[SELL_CLOSED_HOOK_FAILED]" in e.critical_text() and e.st("005930").lifecycle == L.FLAT)

# ── 15. ack_orphan 명령 ───────────────────────────────────────
e = Env()
e.sync()
e.ex.submit_buy("005930", 10, 70000)
e.age("005930", 200)  # BUY_PENDING 타임아웃 → 0주면 orphan
e.sync()
check("15-1) 0주 체결로 타임아웃 → orphan", e.ex.position_state_machine.has_orphan_order("005930"))
check("15-2) orphan이면 신규매수 차단", e.ex.submit_buy("005930", 1, 70000).block_code != "")
e.cmd_dir.mkdir(exist_ok=True)
(e.cmd_dir / "ack_orphan_005930.json").write_text(json.dumps({"note": "HTS 미체결 없음",
                                                               "recovery_id": e.ex.recovery_id("005930", "ORPHAN")}),
                                                   encoding="utf-8")
e.sync()
check("15-3) ack_orphan으로 해제", not e.ex.position_state_machine.has_orphan_order("005930"))
check("15-4) 보류 중이던 매수 컨텍스트 폐기, 훅 미호출",
      "005930" not in e.ex._pending_buy_side_effects and e.fills == [])
(e.cmd_dir / "ack_orphan_000660.json").write_text(json.dumps({"note": "x"}), encoding="utf-8")
e.sync()
check("15-5) orphan 없는 종목 명령은 오류 로그 후 failed/로 이동(사유 파일 포함)",
      not (e.cmd_dir / "ack_orphan_000660.json").exists() and e.logger.error.called
      and len(list((e.cmd_dir / "failed").glob("ack_orphan_000660.*.json"))) == 1
      and len(list((e.cmd_dir / "failed").glob("ack_orphan_000660.*.error.txt"))) == 1)


# ── 16. 저널 I/O 실패 ─────────────────────────────────────────
class _BrokenJournal:
    def load_all(self):
        return {}

    def get(self, symbol):
        raise OSError("journal io")

    def upsert(self, record):
        raise OSError("journal io")

    def remove(self, symbol):
        raise OSError("journal io")


e = Env(journal=_BrokenJournal())
e.sync()
sub = e.ex.submit_buy("005930", 10, 70000)
check("16-1) 저널 쓰기 실패해도 주문은 접수", sub.accepted)
check("16-2) [TRACKED_ORDER_JOURNAL_ERROR] CRITICAL", "[TRACKED_ORDER_JOURNAL_ERROR]" in e.critical_text())
e.broker.positions["005930"] = 10
e.sync()
check("16-3) 저널 실패가 대조를 막지 않음(OPEN)", e.st("005930").lifecycle == L.OPEN)

# ── 17. 거래 로그 ─────────────────────────────────────────────
e = Env()
e.broker.positions["005930"] = 10
e.sync()
e.ex.submit_sell("005930", 10, 9500, exit_reason="트레일링", avg_buy_price=10000,
                 context={"entry_strategy": "swing_test"})
import csv  # noqa: E402

rows = list(csv.DictReader(open(e.settings.storage.trade_log_file, encoding="utf-8")))
check("17-1) 매도 1행 기록", len(rows) == 1 and rows[0]["side"] == "SELL")
check("17-2) 가격·사유·평균단가·전략 컬럼", rows[0]["price"] == "9500" and rows[0]["exit_reason"] == "트레일링"
      and rows[0]["avg_buy_price"] == "10000" and rows[0]["entry_strategy"] == "swing_test")
e2 = Env()
e2.sync()
e2.broker.next_result.append("ambiguous")
e2.ex.submit_buy("005930", 10, 70000)
rows = list(csv.DictReader(open(e2.settings.storage.trade_log_file, encoding="utf-8")))
check("17-3) ambiguous도 accepted=False, AMBIGUOUS 메시지로 기록",
      rows[0]["accepted"] == "False" and rows[0]["message"].startswith("AMBIGUOUS:"))
check("17-4) 수량 0 이하 주문은 전송 전 거부",
      e2.ex.submit_sell("000660", 0).block_code == "INVALID_QUANTITY")

# ── 18. 수동 복구 명령: BOM·잘못된 입력 (8-A, F7) ─────────────
def _error_env():
    e = Env()
    e.sync()
    e.broker.next_result.append("ambiguous")
    e.ex.submit_buy("005930", 10, 70000)
    e.sync()
    e.cmd_dir.mkdir(exist_ok=True)
    return e

e = _error_env()
check("18-0) 준비: ambiguous 매수 → ERROR", e.st("005930").lifecycle == L.ERROR)
# PowerShell 5.1 Out-File -Encoding utf8 형식(BOM + CRLF)
(e.cmd_dir / "ack_error_005930.json").write_bytes(
    b"\xef\xbb\xbf" + json.dumps({"broker_quantity": 0, "note": "HTS 확인", "recovery_id": e.ex.recovery_id("005930", "ERROR")},
                                   ensure_ascii=False).encode("utf-8") + b"\r\n")
e.sync()
check("18-1) BOM 포함 명령도 정상 처리 → ERROR 해제", e.st("005930").lifecycle != L.ERROR)
check("18-2) 처리된 BOM 명령은 processed/에 보관",
      len(list((e.cmd_dir / "processed").glob("ack_error_005930.*.json"))) == 1)

for i, (label, raw) in enumerate([
    ("깨진 JSON", b"{broker_quantity: 0"),
    ("수량이 bool", json.dumps({"broker_quantity": True, "note": "x"}).encode()),
    ("수량이 문자열", json.dumps({"broker_quantity": "0", "note": "x"}).encode()),
    ("수량 음수", json.dumps({"broker_quantity": -1, "note": "x"}).encode()),
    ("note 공백", json.dumps({"broker_quantity": 0, "note": "  "}).encode()),
    ("JSON 배열", b"[0]"),
], start=3):
    e = _error_env()
    (e.cmd_dir / "ack_error_005930.json").write_bytes(raw)
    e.sync()
    failed_files = list((e.cmd_dir / "failed").glob("ack_error_005930.*.json"))
    check(f"18-{i}) {label} → ERROR 유지, 원문은 failed/에 보존",
          e.st("005930").lifecycle == L.ERROR and len(failed_files) == 1
          and failed_files[0].read_bytes() == raw
          and not (e.cmd_dir / "ack_error_005930.json").exists())
check("18-9) 실패 사유 파일 기록(마지막 사례: JSON 배열)",
      "JSON 객체가 아님" in next((e.cmd_dir / "failed").glob("*.error.txt")).read_text(encoding="utf-8"))
e.sync()
check("18-10) 보관된 실패 명령은 다시 처리되지 않음(같은 폴링 반복에도 ERROR 유지)",
      e.st("005930").lifecycle == L.ERROR and len(list((e.cmd_dir / "failed").glob("*.json"))) == 1)

# ── 19. 분할청산: 주문 종료 ≠ 전량 청산 (8-C, F2) ───────────────
def held100():
    e = Env()
    e.broker.positions["005930"] = 100
    e.sync()
    return e

e = held100()
check("19-0) 준비: 100주 OPEN", e.st("005930").lifecycle == L.OPEN)
e.ex.submit_sell("005930", 30, 70_000)
check("19-1) 매도 30주 요청 → 목표 잔고 70 고정, 저널에도 70",
      e.st("005930").expected_final_quantity == 70 and e.journal()["005930"].target_quantity_after_order == 70
      and e.journal()["005930"].base_quantity_before_order == 100)
e.broker.positions["005930"] = 70
e.sync()
check("19-2) [R1] 잔고가 목표(70)에 도달해도 주문 종료 증거 전에는 SELL_PENDING·저널 유지",
      e.st("005930").lifecycle == L.SELL_PENDING and e.ex.has_unresolved_orders() and "005930" in e.journal())
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.sync()
check("19-2b) 주문 FILLED + 잔고 70 → OPEN, 미해결 주문 없음",
      e.st("005930").lifecycle == L.OPEN and not e.ex.has_unresolved_orders()
      and e.st("005930").pending_order_id is None and e.st("005930").known_quantity == 70)
check("19-3) 저널·주문 의도 정리", "005930" not in e.journal() and "005930" not in e.ex.state.unresolved_order_intents)
check("19-4) 분할청산은 청산 훅을 부르지 않고 보류 컨텍스트만 정리",
      e.closes == [] and "005930" not in e.ex._pending_sell_side_effects)
sub = e.ex.submit_sell("005930", 70, 70_000)
e.broker.positions["005930"] = 0
e.sync()
check("19-5) 이어서 남은 70주 전량 매도 → FLAT, 청산 훅 1회(전량은 잔고 0으로 확정)",
      sub.accepted and e.st("005930").lifecycle == L.FLAT and len(e.closes) == 1)

e = held100()
e.ex.submit_sell("005930", 30, 70_000)
e.broker.positions["005930"] = 90
e.sync()
check("19-6) 30주 중 10주만 체결(잔고 90) → SELL_PENDING 유지(차단)",
      e.st("005930").lifecycle == L.SELL_PENDING and e.ex.has_unresolved_orders()
      and e.ex.submit_sell("005930", 10, 70_000).block_code != "")
e.age("005930", 120)
e.sync()
check("19-7) 타임아웃 → OPEN이지만 orphan으로 계속 차단(원 주문 잔여 20주가 살아 있을 수 있음)",
      e.st("005930").lifecycle == L.OPEN and e.ex.position_state_machine.has_orphan_order("005930"))
e.broker.positions["005930"] = 80
e.sync()
check("19-8) 목표(70) 미도달 변화는 orphan 유지", e.ex.position_state_machine.has_orphan_order("005930"))
e.broker.positions["005930"] = 70
e.sync()
check("19-9) [R1] orphan: 목표 잔고 70 도달만으로는 해제 안 함",
      e.ex.position_state_machine.has_orphan_order("005930") and e.ex.has_unresolved_orders())
e.ex._last_order_status_query_at.pop("005930", None)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.sync()
check("19-9b) orphan: FILLED + 잔고 70 → 해제", not e.ex.position_state_machine.has_orphan_order("005930")
      and not e.ex.has_unresolved_orders())

e = held100()
e.ex.submit_sell("005930", 100, 70_000)
e.broker.positions["005930"] = 0
e.sync()
check("19-10) 전량 매도(100→0)는 기존과 같이 FLAT", e.st("005930").lifecycle == L.FLAT and len(e.closes) == 1)

e = held100()
e.ex.submit_sell("005930", 30, 70_000)
e.broker.positions["005930"] = 60
e.sync()
check("19-11) 요청보다 더 줄어듦(100→60, 목표 70) → ERROR (HTS 매도 겹침 등)",
      e.st("005930").lifecycle == L.ERROR and e.st("005930").last_error == "UNEXPECTED_QUANTITY_DECREASE")

e = held100()
e.ex.submit_sell("005930", 30, 70_000)
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.broker.positions["005930"] = 90
e.sync()
check("19-12) 체결조회 FILLED여도 잔고(90)가 목표(70)와 다르면 확정 안 함",
      e.st("005930").lifecycle == L.SELL_PENDING)

e = held100()
e.ex.submit_sell("005930", 30, 70_000)
tmp = e.tmp
e2 = Env(tmpdir=tmp)
e2.broker.positions["005930"] = 70
e2.ex.restore_order_recovery_blocks()
check("19-13) 분할청산 도중 재시작 → 저널 목표 70 보존, 자동 확정 없이 ERROR 복원(사람 확인)",
      e2.journal()["005930"].target_quantity_after_order == 70 and e2.st("005930").lifecycle == L.ERROR)

# ── 20. R1: 주문 종료 증거 없이는 분할청산 차단 유지 ─────────────
for label, status in (("OPEN", BrokerOrderStatus.OPEN), ("UNKNOWN", BrokerOrderStatus.UNKNOWN),
                      ("조회 오류", RuntimeError("api down"))):
    e = held100()
    e.ex.submit_sell("005930", 30, 70_000)
    e.age("005930", 31)
    e.broker.status["0000001"] = status
    e.broker.positions["005930"] = 70
    e.sync()
    n_calls = len(e.broker.place_calls)
    sub = e.ex.submit_sell("005930", 70, 70_000)
    check(f"20-{label}) [R1 재현] 잔고 70·주문 조회 {label} → 차단·저널 유지, 추가 SELL 전송 0회",
          e.st("005930").lifecycle == L.SELL_PENDING and "005930" in e.journal()
          and not sub.accepted and len(e.broker.place_calls) == n_calls)
e = held100()
e.ex.submit_sell("005930", 30, 70_000)
e.age("005930", 31)
e.broker.status["0000001"] = BrokerOrderStatus.FILLED
e.broker.positions["005930"] = 90
e.sync()
check("20-4) FILLED + 잔고 90(목표 70 미도달) → 계속 대조 필요(SELL_PENDING)", e.st("005930").lifecycle == L.SELL_PENDING)
e = held100()
e.ex.submit_sell("005930", 30, 70_000)
e.broker.positions["005930"] = 70
e.sync()
e.age("005930", 120)
e.sync()
check("20-5) 목표 도달 상태로 타임아웃 → 증거 없으므로 orphan으로 차단 유지",
      e.ex.position_state_machine.has_orphan_order("005930")
      and e.ex.submit_sell("005930", 70, 70_000).block_code != "")

# ── 21. R4/8-F: 명령 확보 → 실행 → 보관, 복구 사건 ID ─────────────
from unittest.mock import patch  # noqa: E402


def ack_payload(e, sym="005930", qty=0):
    return json.dumps({"broker_quantity": qty, "note": "HTS", "recovery_id": e.ex.recovery_id(sym, "ERROR")})


e = _error_env()
(e.cmd_dir / "failed").write_text("경로 충돌", encoding="utf-8")        # failed/가 폴더가 아니라 파일
(e.cmd_dir / "ack_error_005930.json").write_bytes(b"{broken")
e.sync()
kept = list((e.cmd_dir / "processing").glob("ack_error_005930.*.json"))
check("21-1) failed/ 경로 충돌 → 원문은 processing/에 보존(삭제 없음)·사유 파일, ERROR 유지",
      len(kept) == 1 and kept[0].read_bytes() == b"{broken"
      and len(list((e.cmd_dir / "processing").glob("*.error.txt"))) == 1 and e.st("005930").lifecycle == L.ERROR
      and "보관 실패" in e.critical_text() and not (e.cmd_dir / "ack_error_005930.json").exists())

e = _error_env()
(e.cmd_dir / "processed").write_text("경로 충돌", encoding="utf-8")
(e.cmd_dir / "ack_error_005930.json").write_text(ack_payload(e), encoding="utf-8")
calls = []
orig_ack = e.ex.position_state_machine.acknowledge_error
e.ex.position_state_machine.acknowledge_error = lambda *a: (calls.append(a), orig_ack(*a))
e.sync()
e.sync()
check("21-2) 실행 성공·보관 실패 → '실행 성공(상태 변경됨)' 기록, 원문 processing/ 보존, 재실행 없음",
      len(calls) == 1 and e.st("005930").lifecycle != L.ERROR
      and len(list((e.cmd_dir / "processing").glob("ack_error_005930.*.json"))) == 1
      and "실행 성공(상태 변경됨)" in e.critical_text())

e = _error_env()
(e.cmd_dir / "ack_error_005930.json").write_text(ack_payload(e), encoding="utf-8")
calls = []
orig_ack = e.ex.position_state_machine.acknowledge_error
e.ex.position_state_machine.acknowledge_error = lambda *a: (calls.append(a), orig_ack(*a))
with patch("pathlib.Path.replace", side_effect=PermissionError("locked")):
    e.sync()
    e.sync()
check("21-3) 확보(processing/ 이동) 실패 → 실행하지 않음, 원문 그대로, CRITICAL 1회",
      calls == [] and e.st("005930").lifecycle == L.ERROR and (e.cmd_dir / "ack_error_005930.json").exists()
      and e.critical_text().count("명령 확보 실패") == 1)

# 21-4: GPT 재현 — 옛 명령 + 새 주문 + 재시작
e = held100_env = Env()
e.sync()
e.broker.next_result.append("ambiguous")
e.ex.submit_buy("005930", 10, 70000)
e.sync()
e.cmd_dir.mkdir(exist_ok=True)
(e.cmd_dir / "processed").write_text("경로 충돌", encoding="utf-8")      # 보관 실패로 원문이 남는 상황
(e.cmd_dir / "ack_error_005930.json").write_text(ack_payload(e), encoding="utf-8")
e.sync()
check("21-4a) 준비: 옛 ERROR 해제(보관 실패로 원문 잔존)", e.st("005930").lifecycle != L.ERROR)
sub = e.ex.submit_buy("005930", 5, 70000)                                 # 새 주문, 미체결
check("21-4b) 준비: 새 매수 접수·미체결", sub.accepted and e.st("005930").lifecycle == L.BUY_PENDING)
e2 = Env(tmpdir=e.tmp)                                                    # 재시작
e2.broker = e.broker
e2.ex.broker = e.broker
e2.ex.restore_order_recovery_blocks()
e2.sync()
e2.sync()
n = len(e2.broker.place_calls)
again = e2.ex.submit_buy("005930", 1, 70000)
check("21-4) [8-F 재현] 재시작 후 옛 명령이 새 주문의 ERROR를 풀지 않음 — ERROR·저널 유지, 추가 주문 0회",
      e2.st("005930").lifecycle == L.ERROR and "005930" in e2.journal()
      and not again.accepted and len(e2.broker.place_calls) == n)

(e2.cmd_dir / "ack_error_005930.json").write_text(ack_payload(e), encoding="utf-8")   # 재시작 전 ID로 쓴 명령
e2.sync()
check("21-5) 재시작 전 recovery_id로 쓴 명령 → 불일치로 거부(failed/), ERROR 유지",
      e2.st("005930").lifecycle == L.ERROR and "recovery_id 불일치" in " ".join(
          str(c.args[0]) for c in e2.logger.error.call_args_list))
rec = json.loads((e2.cmd_dir / "recovery_required.json").read_text(encoding="utf-8"))
cur = e2.ex.recovery_id("005930", "ERROR")
check("21-6) recovery_required.json에 현재 ID·명령 템플릿",
      any(i["recovery_id"] == cur and i["command_file"] == "ack_error_005930.json" for i in rec["items"])
      and cur != json.loads(ack_payload(e))["recovery_id"])
(e2.cmd_dir / "ack_error_005930.json").write_text(ack_payload(e2, qty=0), encoding="utf-8")
(e2.cmd_dir / "processed").unlink()
e2.sync()
check("21-7) 현재 ID로 쓴 명령은 적용 → ERROR 해제, 목록에서 제거",
      e2.st("005930").lifecycle != L.ERROR and e2.ex.recovery_id("005930", "ERROR") is None
      and not any(i["symbol"] == "005930" and i["kind"] == "ERROR" for i in json.loads(
          (e2.cmd_dir / "recovery_required.json").read_text(encoding="utf-8"))["items"]))
e = _error_env()
(e.cmd_dir / "ack_error_005930.json").write_text(json.dumps({"broker_quantity": 0, "note": "HTS"}), encoding="utf-8")
e.sync()
check("21-8) recovery_id 없는 명령 거부", e.st("005930").lifecycle == L.ERROR)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
