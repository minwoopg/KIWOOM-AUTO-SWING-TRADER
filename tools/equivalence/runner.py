"""compare.py가 각 레포에서 실행하는 시나리오 러너 (orig=단타 TradingService / new=OrderExecutor)."""
import sys, os, json, tempfile, logging
from datetime import timedelta, datetime
from pathlib import Path
from unittest.mock import patch
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "."); sys.path.insert(0, HERE)
mode = sys.argv[1]; scen_name = sys.argv[2]
from common import ScriptedBroker, snapshot
from scenarios import SCENARIOS
from domain.models import BrokerOrderStatus
from infra.storage.tracked_order_journal import TrackedOrderJournalStore

tmp = tempfile.mkdtemp()
cmd_dir = Path(tmp) / "commands"; cmd_dir.mkdir()
broker = ScriptedBroker()
WATCH = ["005930", "000660", "010170", "006260", "080220"]

def build():
    if mode == "orig":
        from test_run_once_integration import build_minimal_settings
        settings = build_minimal_settings(tmp)
        from app.main import build_trading_service
        from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
        from infra.storage.state_store import JsonStateStore
        lg = build_app_logger(settings.storage.app_log_file, "INFO")
        svc = build_trading_service(settings, broker, lg, TradeCsvLogger(settings.storage.trade_log_file),
                                    SignalCsvLogger(settings.storage.signal_log_file), JsonStateStore(settings.storage.state_file))
        return svc
    else:
        from testing_helpers import build_minimal_settings
        settings = build_minimal_settings(tmp)
        from domain.service.order_executor import OrderExecutor
        from infra.storage.logger import TradeCsvLogger, build_app_logger, PositionLifecycleLogger
        from infra.storage.state_store import JsonStateStore
        lg = build_app_logger(settings.storage.app_log_file, "INFO")
        ss = JsonStateStore(settings.storage.state_file)
        state, hp = ss.load()
        ex = OrderExecutor(settings=settings, broker=broker, state=state, highest_price=hp, state_store=ss,
                           app_logger=lg, trade_logger=TradeCsvLogger(settings.storage.trade_log_file),
                           position_lifecycle_logger=PositionLifecycleLogger(settings.storage.position_lifecycle_log_file),
                           commands_dir=cmd_dir, on_first_fill_buy=lambda s,q,c: fills.append((s,q)),
                           on_sell_closed=lambda s,c: closes.append(s))
        return ex

fills, closes = [], []
obj = build()
def psm(): return obj._position_state_machine
def journal():
    try: return TrackedOrderJournalStore(obj.settings.storage.tracked_order_journal_file).load_all()
    except Exception as e: return {"CORRUPT": None}
out = []
FIX_NOW = None
os.chdir(tmp)  # commands/ 상대경로 (원본은 cwd 기준)
for step in SCENARIOS[scen_name]:
    k = step[0]; res = None
    if k == "sync":
        bal = broker.get_account_balance()
        if mode == "orig":
            with patch.object(type(obj), "_apply_first_fill_buy_side_effects", lambda self, s, filled_quantity: (self._pending_buy_side_effects.pop(s, None), fills.append((s, filled_quantity)))), \
                 patch.object(type(obj), "_apply_deferred_sell_side_effects", lambda self, s: (self._pending_sell_side_effects.pop(s, None), closes.append(s))):
                obj._sync_position_state_machine_shadow(bal)
        else:
            obj.sync_with_balance(bal, WATCH)
    elif k == "buy":
        _, sym, qty = step
        if mode == "orig":
            price = 1_000_000 // qty if qty else 10000
            import domain.service.trading_service as ts
            with patch.object(ts, "now_kst", lambda: datetime(2026, 9, 28, 10, 0)):
                obj._last_buy_signal_at.pop(sym, None)
                res = obj._try_buy(sym, price, broker.get_account_balance())
            res = res or ""
        else:
            price = 1_000_000 // qty if qty else 10000
            res = obj.submit_buy(sym, qty, price).block_code
    elif k == "sell":
        _, sym, qty, forced = step
        if mode == "orig":
            obj._try_sell(sym, qty, 10000, "강제청산 테스트" if forced else "일반 청산", 10000, force=forced)
            res = broker.calls[-1] if broker.calls else None
        else:
            obj.submit_sell(sym, qty, 10000, exit_reason="x", avg_buy_price=10000, forced=forced)
            res = broker.calls[-1] if broker.calls else None
    elif k == "set":
        broker.positions[step[1]] = step[2]
    elif k == "next":
        broker.next_result.append(step[1])
    elif k == "age":
        st = psm().get(step[1]); st.pending_since = st.pending_since - timedelta(seconds=step[2])
    elif k == "stale":
        st = psm().get(step[1])
        for a in ("pending_since", "partial_fill_since", "requested_at"):
            if getattr(st, a): setattr(st, a, getattr(st, a) - timedelta(seconds=200))
    elif k == "status":
        oid = f"{step[1]:07d}"
        broker.status[oid] = RuntimeError("api down") if step[2] == "RAISE" else BrokerOrderStatus[step[2]]
    elif k == "ack_error":
        (Path("commands") / f"ack_error_{step[1]}.json").write_text(json.dumps({"broker_quantity": step[2], "note": "t"}), encoding="utf-8")
    elif k == "ack_orphan":
        (Path("commands") / f"ack_orphan_{step[1]}.json").write_text(json.dumps({"note": "t"}), encoding="utf-8")
    elif k == "restart":
        obj = build()
    snap = snapshot(psm(), obj.state.unresolved_order_intents, journal(), obj._pending_buy_side_effects, obj._pending_sell_side_effects, broker.calls)
    snap["step"] = list(step); snap["res"] = res if not isinstance(res, tuple) else list(res)
    snap["fills"] = list(fills); snap["closes"] = list(closes)
    snap["recovery_failed"] = obj._journal_recovery_failed
    out.append(snap)
print(json.dumps(out, ensure_ascii=False, default=str))
