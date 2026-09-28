# -*- coding: utf-8 -*-
"""스윙 상태 모델·저장소 회귀 테스트 (스윙 분리 4라운드, 2026-09-28)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, ".")

from domain.position.lifecycle import PositionLifecycle as L
from domain.position.swing_state import (
    SWING_STATE_SCHEMA_VERSION, PositionMeta, SwingState, SwingStateFormatError,
)
from domain.service.order_executor import OrderExecutor
from infra.storage.logger import TradeCsvLogger
from infra.storage.swing_state_store import SwingStateCorruptError, SwingStateStore
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


def raises(fn, exc) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


def tmp_store():
    d = tempfile.mkdtemp()
    return SwingStateStore(f"{d}/state.json"), Path(d)


# ── 1. 모델 검증 ─────────────────────────────────────────────
check("1-1) 기본 메타 생성", PositionMeta("005930").origin == "ORDER")
check("1-2) 빈 symbol 거부", raises(lambda: PositionMeta(" "), SwingStateFormatError))
check("1-3) 허용되지 않은 origin 거부", raises(lambda: PositionMeta("005930", origin="HTS"), SwingStateFormatError))
check("1-4) 손절가 0/음수/실수 거부", all(raises(lambda v=v: PositionMeta("005930", stop_price=v), SwingStateFormatError)
                                    for v in (0, -1, 100.5)))
check("1-5) meta는 dict만", raises(lambda: PositionMeta("005930", meta=[1]), SwingStateFormatError))

# ── 2. 저장·복원 왕복 ────────────────────────────────────────
store, d = tmp_store()
st = SwingState()
st.unresolved_order_intents["005930"] = {"side": "BUY", "quantity": 10, "order_id": "", "created_at": "x"}
st.last_order_id_by_symbol["005930"] = "0000123"
st.upsert_position_meta(PositionMeta("000660", strategy_id="s1", stop_price=170000,
                                     meta={"high_close": 185000}))
st.last_session_date = "2026-09-28"
store.save(st)
loaded, hp = store.load()
check("2-1) 주문 의도 복원", loaded.unresolved_order_intents == st.unresolved_order_intents)
check("2-2) 마지막 주문번호 복원", loaded.last_order_id_by_symbol == {"005930": "0000123"})
m = loaded.positions["000660"]
check("2-3) 포지션 메타 복원", m.strategy_id == "s1" and m.stop_price == 170000 and m.meta == {"high_close": 185000})
check("2-4) 마지막 기동 거래일 복원", loaded.last_session_date == "2026-09-28")
check("2-5) 호환용 두 번째 반환값은 빈 dict", hp == {})
raw = json.loads((d / "state.json").read_text(encoding="utf-8"))
check("2-6) schema_version 기록", raw["schema_version"] == SWING_STATE_SCHEMA_VERSION)
check("2-7) 수량·가격 필드를 상태에 중복 저장하지 않음",
      not any(k in raw["positions"]["000660"] for k in ("quantity", "avg_price", "average_price")))
created = loaded.positions["000660"].created_at
loaded.upsert_position_meta(PositionMeta("000660", strategy_id="s1", stop_price=175000))
check("2-8) 메타 갱신 시 created_at 유지", loaded.positions["000660"].created_at == created)
check("2-9) 메타 삭제", loaded.remove_position_meta("000660") is not None and "000660" not in loaded.positions)
check("2-10) 파일 없으면 빈 상태", SwingStateStore(f"{tempfile.mkdtemp()}/none.json").load()[0].positions == {})

# ── 3. fail-closed ───────────────────────────────────────────
def write(text):
    s, dd = tmp_store()
    (dd / "state.json").write_text(text, encoding="utf-8")
    return s


check("3-1) JSON 깨짐 → SwingStateCorruptError", raises(lambda: write("{bad").load(), SwingStateCorruptError))
check("3-2) 단타 RuntimeState 형식(schema_version 없음) 거부",
      raises(lambda: write(json.dumps({"bought_symbols_today": [], "unresolved_order_intents": {}})).load(),
             SwingStateCorruptError))
check("3-3) 다른 schema_version 거부",
      raises(lambda: write(json.dumps({"schema_version": 99})).load(), SwingStateCorruptError))
check("3-4) intents 값이 dict가 아니면 거부",
      raises(lambda: write(json.dumps({"schema_version": 1, "unresolved_order_intents": {"A": 1}})).load(),
             SwingStateCorruptError))
check("3-5) 포지션 키와 symbol 불일치 거부",
      raises(lambda: write(json.dumps({"schema_version": 1, "positions": {"A": {"symbol": "B"}}})).load(),
             SwingStateCorruptError))
check("3-6) 알 수 없는 메타 필드 거부(오타 방지)",
      raises(lambda: write(json.dumps({"schema_version": 1, "positions": {"A": {"symbol": "A", "stop": 1}}})).load(),
             SwingStateCorruptError))
s, dd = tmp_store()
s.save(SwingState())
before = (dd / "state.json").read_text(encoding="utf-8")
bad = SwingState()
bad.positions["X"] = Mock(to_dict=Mock(side_effect=RuntimeError("boom")))
try:
    s.save(bad)
except RuntimeError:
    pass
check("3-7) 저장 중 실패해도 기존 파일 유지", (dd / "state.json").read_text(encoding="utf-8") == before)
check("3-8) 임시 파일 남지 않음", list(dd.glob("*.tmp")) == [])

# ── 4. OrderExecutor와 함께 동작 ──────────────────────────────
tmp = tempfile.mkdtemp()
settings = build_minimal_settings(tmp)
broker = ScriptedBroker()


def make_executor():
    store = SwingStateStore(settings.storage.state_file)
    state, hp = store.load()
    return OrderExecutor(settings=settings, broker=broker, state=state, highest_price=hp,
                         state_store=store, app_logger=Mock(),
                         trade_logger=TradeCsvLogger(settings.storage.trade_log_file),
                         commands_dir=Path(tmp) / "commands")


ex = make_executor()
ex.sync_with_balance(broker.get_account_balance())
sub = ex.submit_buy("005930", 10, 70000)
check("4-1) SwingState로 매수 접수", sub.accepted)
on_disk = SwingStateStore(settings.storage.state_file).load()[0]
check("4-2) 주문 의도가 SwingState 파일에 기록됨", "005930" in on_disk.unresolved_order_intents)
ex2 = make_executor()
check("4-3) 재시작 시 SwingState의 주문 의도로 ERROR 복원",
      ex2.position_state_machine.get("005930").lifecycle == L.ERROR)
broker.positions["005930"] = 10
ex.sync_with_balance(broker.get_account_balance())
ex.sync_with_balance(broker.get_account_balance())
ex.save_state()
check("4-4) 체결 확정 후 주문 의도 정리가 파일에 반영",
      "005930" not in SwingStateStore(settings.storage.state_file).load()[0].unresolved_order_intents)

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
