"""대시보드 검증용 임시 자료 — 실제 계좌·주문과 무관한 가짜 기록을 **지정한 임시 폴더**에만 만듭니다.

    python -m dashboard.sample_data <빈 폴더> normal|empty|stale

normal: 관리자·실행기 실행 중, 후보 0건, HOLD 종목, 자동 전략 미연결, 보유·부분 체결·취소 미확인·ERROR 종목, 중단 요청(반영 전)
stale : heartbeat 오래됨, 중단 반영됨, 상태 파일 깨짐 없음
empty : 아무 파일 없음(설정만)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

SETTINGS = """app: {name: swing-auto-trader, env: local, log_level: INFO}
broker:
  provider: kiwoom
  use_mock: false
  base_url: https://mockapi.kiwoom.com
  app_key: APPKEYSAMPLE123456
  secret_key: SECRETSAMPLE987654
  account_number: "81234567"
  account_scope_id: acct-swing
  is_paper_trading: true
cost_model: {gross_roundtrip_pct: 0.00, base_roundtrip_pct: 0.35, stress_roundtrip_pct: 0.90}
storage:
  state_file: data/state.json
  trade_log_file: logs/trades.csv
  app_log_file: logs/app.log
  position_lifecycle_log_file: logs/position_lifecycle.csv
  tracked_order_journal_file: data/tracked_order_journal.json
  run_baseline_log_file: logs/run_baseline.csv
  order_status_observation_log_file: logs/order_status_observations.jsonl
  fill_ledger_file: data/fill_ledger.jsonl
  reports_dir: reports
  commands_dir: commands
"""


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def build(root: Path, scenario: str, now: datetime) -> Path:
    root = Path(root)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config/settings.yaml").write_text(SETTINGS, encoding="utf-8")
    if scenario == "empty":
        return root
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from infra.research.scan_store import ScanStore
    from infra.watch.daemon import DaemonStore
    from infra.watch.store import WatchStore

    fresh = scenario == "normal"
    hb = now - timedelta(seconds=20 if fresh else 900)
    day = now.date().isoformat()
    prev = (now.date() - timedelta(days=1)).isoformat()
    wdir = root / "data/watch"
    with DaemonStore(wdir / "daemon.sqlite3") as ds:
        c = ds.conn
        c.execute("INSERT INTO daemon_run(run_id,pid,started_at,heartbeat_at,state,current_task,last_error) "
                  "VALUES(?,?,?,?,?,?,?)", ("wd_sample_1", 4242, _iso(now - timedelta(hours=3)), _iso(hb), "RUNNING", None, ""))
        for key, kind, td, status, fin, det in (
            (f"CLOSE_PREP|{prev}|scope", "CLOSE_PREP", prev, "COMPLETE", f"{prev}T18:10:21",
             {"prepare": {"run_id": "prep_x", "status": "COMPLETE", "yield": ""}}),
            (f"OPEN_CHECK|{day}|OPEN+5m", "OPEN_CHECK", day, "COMPLETE", f"{day}T09:05:00",
             {"signal_date": prev, "candidate_status": "NO_CANDIDATES", "candidate_reason": "후보 0건 (개장 전 저장된 스캔 1회)",
              "counts": {"candidates": 0}}),
            (f"CLOSE_PREP|{day}|scope", "CLOSE_PREP", day, "PENDING", None, {})):
            c.execute("INSERT INTO task(task_key,kind,trading_day,scope,status,attempts,failures,due_at,created_at,"
                      "started_at,finished_at,detail_json,contract_hash,config_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (key, kind, td, "scope", status, 1 if fin else 0, 0, td, td, fin, fin, json.dumps(det, ensure_ascii=False),
                       "677f9a95c245", 6))
        for sym, ok, why in (("005930", 1, []), ("010120", 0, ["ANALYSIS_HOLD:NO_TRADES"])):
            c.execute("INSERT INTO candidate_gate(stage,signal_date,contract_hash,symbol,eligible,reasons_json,evidence_json,"
                      "evaluated_at,committed_at) VALUES('SCAN',?,?,?,?,?,'{}',?,?)",
                      (prev, "677f9a95c245", sym, ok, json.dumps(why), f"{prev}T18:10:21", f"{prev}T18:10:22"))
    with WatchStore(wdir / "watch.sqlite3") as ws:
        for code, st, ast, areason in (("005930", "READY", "READY", ""), ("010120", "READY", "HOLD", "NO_TRADES:2026-04-08"),
                                       ("058470", "UNKNOWN", "UNKNOWN", "STALE")):
            ws.conn.execute("INSERT INTO readiness(target,kind,code,status,reason,detail_json,config_version,run_id,checked_at,"
                            "analysis_status,analysis_reason) VALUES(?,?,?,?,?,'{}',6,'prep_x',?,?,?)",
                            (f"STOCK:{code}", "STOCK", code, st, "" if st == "READY" else "STALE", f"{prev}T18:10:21",
                             ast, areason))
    with ScanStore(wdir / "watch_s1.sqlite3") as ss:
        ss.conn.execute("INSERT INTO scan_run(run_id,run_key,attempt,scan_at,signal_date,strategy,config_hash,universe_policy,"
                        "feature_version,market_version,after_close_min,started_at,status,contract_hash,committed_at) "
                        "VALUES('s1_x','k',1,?,?,'s1','h','u','f','m',160,?,'COMPLETE','677f9a95c245',?)",
                        (f"{prev}T18:10:21", prev, f"{prev}T18:10:20", f"{prev}T18:10:22"))
        for sym, name, sig in (("005930", "삼성전자", "FAIL"), ("010120", "LS ELECTRIC", "UNKNOWN"), ("058470", "리노공업", "FAIL")):
            ss.conn.execute("INSERT INTO s1_eval(run_id,symbol,name,market,series_id,signal_date,eligible_signal,pattern_pass,"
                            "eligibility_pass,market_pass,stop_valid,data_status,index_status,snapshot_status,no_trades_hold,"
                            "final,actionable,input_hash,codec,evidence_z) VALUES('s1_x',?,?,'KOSPI',?,?,?,'FAIL','PASS','FAIL',"
                            "'PASS','OK','OK','OK',0,1,1,'h','c',x'00')", (sym, name, f"STOCK:{sym}", prev, sig))
        # UI 1차: 005930은 판정 본문(조건별 저장 값)까지 — 전일 고가 돌파 미충족·시장 RISK_OFF
        res = {"checks": [{"name": "C_GT_MA120", "result": "PASS", "value": 12000.0, "detail": ""},
                          {"name": "PULLBACK", "result": "PASS", "value": 4, "detail": ""},
                          {"name": "CLOSE_GT_PREV_HIGH", "result": "FAIL", "value": -1500.0, "detail": ""},
                          {"name": "MARKET_REGIME", "result": "FAIL", "value": None, "detail": "RISK_OFF"}],
               "market": {"state": "RISK_OFF", "index_id": "INDEX:KOSPI:001"},
               "levels": {"stop_ref": 218000.0, "entry_cap": 231500.0, "risk_ratio_at_cap": 0.0583}}
        ss.conn.execute("UPDATE s1_eval SET codec='zd1', result_z=? WHERE run_id='s1_x' AND symbol='005930'",
                        (ss._pack(res),))
    data = root / "data"
    led = [
        {"v": 1, "event_id": "acct-swing|20261013|BUY|005930|0112293|1", "kind": "BUY", "symbol": "005930", "quantity": 1,
         "price": 229000, "price_source": "BROKER_AVG", "trade_date": prev, "occurred_at": f"{prev}T12:07:50",
         "order_id": "0112293", "note": ""},
        {"v": 1, "event_id": "acct-swing|20261013|SELL|005930|0112348|1", "kind": "SELL", "symbol": "005930", "quantity": 1,
         "price": 229000, "price_source": "ORDER_ESTIMATE", "trade_date": prev, "occurred_at": f"{prev}T12:08:01",
         "order_id": "0112348", "note": ""},
        {"v": 1, "event_id": "e3", "kind": "BUY", "symbol": "005380", "quantity": 1, "price": 210000,
         "price_source": "BROKER_AVG", "trade_date": day, "occurred_at": f"{day}T09:12:00", "order_id": "0200001", "note": ""},
    ]
    (data / "fill_ledger.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in led), encoding="utf-8")
    reg = [
        {"command_id": "m1-buy-1013b", "side": "BUY", "symbol": "005930", "quantity": 1, "event": "STARTED", "at": f"{prev}T12:07:44"},
        {"command_id": "m1-buy-1013b", "side": "BUY", "symbol": "005930", "quantity": 1, "event": "FINISHED", "status": "FILLED",
         "order_id": "0112293", "reference_price": 228750, "at": f"{prev}T12:07:50"},
        {"command_id": "m1-lc-1013", "kind": "limit-cancel", "side": "BUY", "symbol": "005930", "quantity": 1, "event": "STARTED",
         "at": f"{day}T10:00:00"},
        {"command_id": "m1-lc-1013", "kind": "limit-cancel", "side": "BUY", "symbol": "005930", "quantity": 1, "event": "PLACED",
         "order_id": "0300001", "limit_price": 206000, "at": f"{day}T10:00:02"},
        {"command_id": "m1-lc-1013", "kind": "limit-cancel", "side": "BUY", "symbol": "005930", "quantity": 1,
         "event": "FINISHED", "status": "CANCEL_UNCONFIRMED", "order_id": "0300001", "limit_price": 206000,
         "reason": "취소 응답 불명", "at": f"{day}T10:00:20"},
    ]
    (data / "paper_orders.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in reg), encoding="utf-8")
    status = {"schema": "rs1", "run_id": "rn_sample", "pid": 5151, "started_at": _iso(now - timedelta(hours=2)),
              "heartbeat_at": _iso(hb), "stage": "RUNNING", "trade_date": day, "execution_mode": "KIWOOM_PAPER",
              "account_scope": "acct-swing", "code_version": "HEAD 5c3cac8abcd0 (추적 파일 수정 없음)", "process": "app.main",
              "strategy_id": "null", "strategy_connected": False, "halted_reason": "",
              "buy_halt": None if fresh else {"request_id": "HALT-1", "requested_at": _iso(now - timedelta(minutes=30)),
                                              "applied_at": _iso(now - timedelta(minutes=29)), "reason": "점검"},
              "last_balance_at": _iso(hb), "last_balance_error": "", "last_reconcile_at": _iso(hb), "reconcile_ok": True,
              "cash": 49_700_000, "positions": [{"symbol": "005380", "quantity": 1, "average_price": 210000}],
              "tracked_orders": [{"symbol": "000660", "side": "BUY", "order_id": "0400001", "requested": 3, "filled": 1,
                                  "reference_price": 180000}],
              "unresolved_symbols": ["000660"], "recovery_symbols": ["035720"], "lifecycle": {"000660": "BUY_PENDING"},
              "counters": {"ticks": 50, "orders_sent": 0, "orders_accepted": 0, "intents_denied": 0}, "denied_codes": {}}
    (data / "runner_status.json").write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    (root / "commands").mkdir(exist_ok=True)
    halt = {"request_id": "HALT-1", "requested_at": _iso(now - timedelta(minutes=30)), "requested_by": "dashboard",
            "reason": "점검"}
    (root / "commands/halt_new_buys.json").write_text(json.dumps(halt, ensure_ascii=False), encoding="utf-8")
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    t = now.strftime("%Y-%m-%d %H:%M")
    (logs / "app.log").write_text(
        f"{t}:01,100 | INFO | [SESSION] 장중 폴링\n"
        f"{t}:02,200 | WARNING | [SESSION] 잔고 조회 실패 1회 연속 — kiwoom request failed: http=429 token=SAMPLETOKEN123456\n"
        f"{t}:03,300 | CRITICAL | [RECOVERY_REQUIRED] 035720 | ERROR | recovery_id=ERROR-035720-abcd 계좌 81234567\n",
        encoding="utf-8")
    (logs / "watch_daemon.log").write_text(
        f"{now:%Y-%m-%dT%H:%M}:00 [관리자] 실행 코드 HEAD 5c3cac8abcd0 (추적 파일 수정 없음) · Python 3.14.4 (시작 당시 체크아웃 정보)\n"
        f"{now:%Y-%m-%dT%H:%M}:05 [관리자] OPEN_CHECK|{day}|OPEN+5m → COMPLETE\n"
        f"{now:%Y-%m-%dT%H:%M}:06 [RESEARCH] ka10099 HTTP 429 — 2.0초 후 재시도 (1/4)\n", encoding="utf-8")
    return root


if __name__ == "__main__":
    from datetime import datetime as _dt
    out = build(Path(sys.argv[1]), sys.argv[2], _dt.now().replace(microsecond=0))
    print(out)
