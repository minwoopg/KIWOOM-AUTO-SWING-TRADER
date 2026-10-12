"""W2 실측 진단 (읽기 전용 — DB를 바꾸지 않음, API 호출 없음). 스윙 레포 루트에서:
    python diag_w2.py [종목코드=005930] [신호일=2026-10-08]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
code = sys.argv[1] if len(sys.argv) > 1 else "005930"
sig = sys.argv[2] if len(sys.argv) > 2 else "2026-10-08"


def ro(p):
    return sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True)


def q(conn, sql, *a):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, a)]


print(f"# W2 진단 {datetime.now().isoformat(timespec='seconds')} · 종목 {code} · 신호일 {sig}\n")

print("## 1. 준비 상태(watch.sqlite3 readiness)")
w = ro(ROOT / "data/watch/watch.sqlite3")
for r in q(w, "SELECT target,status,reason,analysis_status,analysis_reason,checked_at,run_id,detail_json FROM readiness"
              " WHERE target IN (?,?,?)", f"STOCK:{code}", "INDEX:KOSPI:001", "INDEX:KOSDAQ:101"):
    d = json.loads(r.pop("detail_json") or "{}")
    print(r)
    print("   detail:", json.dumps(d, ensure_ascii=False, default=str)[:800])
print("\n   준비 실행:", q(w, "SELECT run_id,started_at,finished_at,status,counts_json,note FROM prepare_run ORDER BY seq DESC LIMIT 3"))

print("\n## 2. 연구 DB 시계열(research.sqlite3)")
r = ro(ROOT / "data/research/research.sqlite3")
for sid in (f"STOCK:{code}", "INDEX:KOSPI:001", "INDEX:KOSDAQ:101"):
    s = q(r, "SELECT series_id,revision,first_date,last_date,fetched_at,verified_base_dt,verified_at,coverage,"
             "integrity,updated_at FROM series WHERE series_id=?", sid)
    print(s[0] if s else f"{sid}: 없음")
    for b in q(r, "SELECT * FROM bar WHERE series_id=? ORDER BY date DESC LIMIT 3", sid):
        print("   bar", {k: b[k] for k in b if k not in ("series_id",)})
    for e in q(r, "SELECT at,event,detail_json FROM series_event WHERE series_id=? ORDER BY event_id DESC LIMIT 4", sid):
        print("   event", e["at"], e["event"], e["detail_json"][:300])

print("\n## 3. 연구 계산 입력(그 시각 기준)")
try:
    from infra.research.store import ResearchStore
    with ResearchStore(ROOT / "data/research/research.sqlite3") as rs:
        for when in (None, datetime.fromisoformat(f"{sig}T18:10:06")):
            try:
                ser = rs.research_series(f"STOCK:{code}", as_of=when)
                last = ser.bars[-3:]
                print(f"as_of={when}: rev={ser.revision} integrity={ser.integrity} time_proof={ser.time_proof} "
                      f"unproven={ser.unproven_bars} last={[b.date.isoformat() for b in last]} "
                      f"available_at={[(k.isoformat(), v.isoformat()) for k, v in sorted(ser.available_at.items())[-3:]]}")
            except Exception as exc:                       # noqa: BLE001
                print(f"as_of={when}: {type(exc).__name__}: {exc}")
except Exception as exc:                                   # noqa: BLE001
    print("ResearchStore 열기 실패", type(exc).__name__, exc)

print("\n## 4. 관리자 DB(daemon.sqlite3)")
dm = ro(ROOT / "data/watch/daemon.sqlite3")
for t in q(dm, "SELECT task_key,status,attempts,started_at,finished_at,config_version,contract_hash,detail_json"
               " FROM task ORDER BY trading_day, kind"):
    d = json.loads(t.pop("detail_json") or "{}")
    print(t)
    print("   detail:", json.dumps(d, ensure_ascii=False, default=str)[:900])
print("   call_usage:", q(dm, "SELECT * FROM call_usage ORDER BY day"))
for g in q(dm, "SELECT stage,signal_date,symbol,eligible,reasons_json,config_version,evaluated_at,committed_at"
               " FROM candidate_gate ORDER BY gate_id"):
    print("   gate", g)
print("   runs:", q(dm, "SELECT run_id,started_at,heartbeat_at,stopped_at,state,last_error FROM daemon_run ORDER BY rowid"))

print("\n## 5. S1 관찰(watch_s1.sqlite3)")
ss = ro(ROOT / "data/watch/watch_s1.sqlite3")
for run in q(ss, "SELECT run_id,signal_date,status,scan_at,committed_at,contract_hash FROM scan_run WHERE signal_date=?", sig):
    print(run)
try:
    from infra.research.scan_store import ScanStore
    with ScanStore(ROOT / "data/watch/watch_s1.sqlite3") as sst:
        for run in sst.runs(sig):
            for e in sst.evals(run["run_id"]):
                if e["symbol"] == code:
                    print("   eval", {k: e[k] for k in e if k not in ("result", "evidence")})
                    print("   evidence.stock", json.dumps((e["evidence"] or {}).get("stock"), ensure_ascii=False,
                                                         default=str)[:600])
except Exception as exc:                                   # noqa: BLE001
    print("ScanStore 읽기 실패", type(exc).__name__, exc)
