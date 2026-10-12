"""W2 실측 진단 — 읽기 전용(모든 DB를 `mode=ro`로만 엶: 만들기·스키마 올리기·백업·쓰기 없음, API 호출 없음).

    python diag_w2.py [종목코드=005930] [신호일=2026-10-08] [--as-of 2026-10-08T18:10:06] [--root 폴더]

- 1·2번은 **지금** DB 상태(마지막 준비 판정, 현재 revision의 최근 봉).
- 3번 '그 시각 기준' 조회의 시각:
  * `--as-of`(한국 시간, 시간대 없는 ISO)가 있으면 그 시각.
  * 없으면 그 신호일 CLOSE_PREP가 기록한 준비 실행의 **완료 시각**(근사 — 당시 판정 그 순간을 입증하지 않음).
  * 둘 다 없으면 시각을 추정하지 않고 '그 시각 기준' 조회를 건너뜀.
- 다시 계산·스캔·후보 생성을 하지 않음. 관리자가 도는 중이면 DB마다 다른 순간을 볼 수 있음 — 결정적 대조는 중지 뒤 사본으로.
- `--root`: data/watch·data/research가 있는 프로젝트(또는 사본) 루트. export-db 산출물에는 연구·감시 설정 DB가 없어 이 진단 전체를
  돌릴 수 없음.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from infra.research.store import open_readonly  # noqa: E402


def q(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]


def _open(path: Path, label: str):
    try:
        return open_readonly(path)
    except (FileNotFoundError, OSError) as exc:
        print(f"   [{label}] {exc}")
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W2 실측 진단(읽기 전용)")
    ap.add_argument("code", nargs="?", default="005930")
    ap.add_argument("signal_date", nargs="?", default="2026-10-08")
    ap.add_argument("--as-of", help="'그 시각 기준' 조회 시각(한국 시간 ISO, 예 2026-10-08T18:10:06)")
    ap.add_argument("--root", default=str(Path.cwd()), help="data/ 가 있는 프로젝트(또는 사본) 루트")
    a = ap.parse_args(argv)
    root, code, sig = Path(a.root), a.code, a.signal_date
    wdb, rdb = root / "data/watch/watch.sqlite3", root / "data/research/research.sqlite3"
    ddb, sdb = root / "data/watch/daemon.sqlite3", root / "data/watch/watch_s1.sqlite3"
    print(f"# W2 진단 {datetime.now().isoformat(timespec='seconds')} · 종목 {code} · 신호일 {sig} · 읽기 전용\n")

    print("## 1. 준비 상태 — 지금(마지막 판정) (watch.sqlite3 readiness)")
    w = _open(wdb, "watch")
    if w:
        for r in q(w, "SELECT target,status,reason,analysis_status,analysis_reason,checked_at,run_id,detail_json"
                      " FROM readiness WHERE target IN (?,?,?)", f"STOCK:{code}", "INDEX:KOSPI:001", "INDEX:KOSDAQ:101"):
            d = json.loads(r.pop("detail_json") or "{}")
            print(r)
            print("   detail:", json.dumps(d, ensure_ascii=False, default=str)[:800])
        print("\n   준비 실행:", q(w, "SELECT run_id,started_at,finished_at,status,counts_json,note FROM prepare_run"
                                   " ORDER BY seq DESC LIMIT 3"))

    print("\n## 2. 연구 DB 시계열 — 지금(현재 revision) (research.sqlite3)")
    r = _open(rdb, "research")
    if r:
        for sid in (f"STOCK:{code}", "INDEX:KOSPI:001", "INDEX:KOSDAQ:101"):
            s = q(r, "SELECT series_id,revision,first_date,last_date,fetched_at,verified_base_dt,verified_at,coverage,"
                     "integrity,updated_at FROM series WHERE series_id=?", sid)
            print(s[0] if s else f"{sid}: 없음")
            for b in q(r, "SELECT * FROM bar WHERE series_id=? ORDER BY date DESC LIMIT 3", sid):
                print("   bar", {k: b[k] for k in b if k != "series_id"})
            for e in q(r, "SELECT at,event,detail_json FROM series_event WHERE series_id=? ORDER BY event_id DESC"
                          " LIMIT 4", sid):
                print("   event", e["at"], e["event"], e["detail_json"][:300])

    d = _open(ddb, "daemon")
    tasks = [] if d is None else q(d, "SELECT task_key,kind,trading_day,status,attempts,started_at,finished_at,"
                                      "config_version,contract_hash,detail_json FROM task ORDER BY trading_day, kind")
    as_of, basis = None, ""
    if a.as_of:
        as_of, basis = datetime.fromisoformat(a.as_of), "--as-of 지정"
    else:
        prep = next((json.loads(t["detail_json"] or "{}").get("prepare") for t in tasks
                     if t["kind"] == "CLOSE_PREP" and t["trading_day"] == sig
                     and json.loads(t["detail_json"] or "{}").get("prepare")), None)
        fin = None
        if prep and w:
            row = q(w, "SELECT finished_at FROM prepare_run WHERE run_id=?", prep.get("run_id"))
            fin = row[0]["finished_at"] if row and row[0]["finished_at"] else None
        if fin:
            as_of, basis = datetime.fromisoformat(fin), f"준비 실행 {prep['run_id']} 완료 시각(근사 — 판정 순간 자체 아님)"

    print("\n## 3. 연구 계산 입력")
    try:
        from infra.research.store import ResearchStore
        with ResearchStore(rdb, readonly=True) as rs:
            whens = [(None, "지금(현재 revision)")]
            if as_of is not None:
                whens.append((as_of, basis))
            else:
                print(f"   그 시각 기준: 신호일 {sig}의 준비 완료 기록 없음 — 시각을 추정하지 않음(--as-of로 지정)")
            for when, label in whens:
                try:
                    ser = rs.research_series(f"STOCK:{code}", as_of=when)
                    last = ser.bars[-3:]
                    print(f"as_of={when} [{label}]: rev={ser.revision} integrity={ser.integrity} time_proof="
                          f"{ser.time_proof} unproven={ser.unproven_bars} last={[b.date.isoformat() for b in last]} "
                          f"available_at={[(k.isoformat(), v.isoformat()) for k, v in sorted(ser.available_at.items())[-3:]]}")
                except Exception as exc:                   # noqa: BLE001
                    print(f"as_of={when} [{label}]: {type(exc).__name__}: {exc}")
    except Exception as exc:                               # noqa: BLE001
        print("   연구 DB를 읽기 전용으로 열 수 없음:", type(exc).__name__, exc)

    print("\n## 4. 관리자 DB(daemon.sqlite3)")
    if d:
        for t in tasks:
            det = json.loads(t.pop("detail_json") or "{}")
            print(t)
            print("   detail:", json.dumps(det, ensure_ascii=False, default=str)[:900])
        print("   call_usage:", q(d, "SELECT * FROM call_usage ORDER BY day"))
        for g in q(d, "SELECT stage,signal_date,symbol,eligible,reasons_json,config_version,evaluated_at,committed_at"
                      " FROM candidate_gate ORDER BY gate_id"):
            print("   gate", g)
        print("   runs:", q(d, "SELECT run_id,started_at,heartbeat_at,stopped_at,state,last_error FROM daemon_run"
                               " ORDER BY rowid"))

    print("\n## 5. S1 관찰(watch_s1.sqlite3)")
    try:
        from infra.research.scan_store import ScanStore
        with ScanStore(sdb, readonly=True) as sst:
            for run in sst.runs(sig):
                print({k: run.get(k) for k in ("run_id", "signal_date", "status", "scan_at", "committed_at",
                                                "contract_hash")})
                for e in sst.evals(run["run_id"]):
                    if e["symbol"] == code:
                        print("   eval", {k: e[k] for k in e if k not in ("result", "evidence")})
                        print("   evidence.stock", json.dumps((e["evidence"] or {}).get("stock"), ensure_ascii=False,
                                                             default=str)[:600])
    except Exception as exc:                               # noqa: BLE001
        print("   관찰 DB를 읽기 전용으로 열 수 없음:", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
