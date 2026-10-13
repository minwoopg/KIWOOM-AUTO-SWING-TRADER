"""W2 실측 진단 — 읽기 전용(모든 DB를 `mode=ro`로만 엶: 만들기·스키마 올리기·백업·쓰기 없음, API 호출 없음).

    python diag_w2.py [종목코드=005930] [신호일=2026-10-08] [--as-of 2026-10-08T18:10:06] [--root 폴더]

- 1·2번은 **지금** DB 상태(마지막 준비 판정, 현재 revision의 최근 봉).
- 3번 '그 시각 기준' 조회의 시각:
  * `--as-of`(한국 시간, 시간대 없는 ISO)가 있으면 그 시각.
  * 없으면 그 신호일 CLOSE_PREP가 기록한 준비 실행의 **판정 기준 시각**(evaluated_as_of — 감시 DB wa3부터 기록, 정확).
  * 그 열이 비어 있으면(wa3 이전 실행) 준비 실행의 **완료 시각**(근사 — 당시 판정 그 순간을 입증하지 않음).
  * 모두 없으면 시각을 추정하지 않고 '그 시각 기준' 조회를 건너뜀.
- 입력 검증: `--as-of`는 시간대 없는 한국 시간만(`+09:00` 등 시간대가 붙으면 사용법 오류 2), 신호일은 실제 날짜(아니면 2).
- 끝에 구역별 요약(OK·MISSING·FAILED). 종료 코드: 0 = 모두 OK, 1 = 일부 없음·실패(부분 진단), 2 = 사용법 오류.
- `--out 파일`: 결과를 UTF-8 파일로 직접 저장(Windows에서 `>` 리다이렉트는 cp949로 인코딩돼 실패·깨짐). 프로젝트 `data/` 안은 거부(2).
  콘솔 출력은 표현 못 하는 문자를 바꿔 찍고 멈추지 않음.
- 다시 계산·스캔·후보 생성을 하지 않음. 관리자가 도는 중이면 DB마다 다른 순간을 볼 수 있음 — 결정적 대조는 중지 뒤 사본으로.
- `--root`: data/watch·data/research가 있는 프로젝트(또는 사본) 루트. export-db 산출물에는 연구·감시 설정 DB가 없어 이 진단 전체를
  돌릴 수 없음.
"""
import argparse
import contextlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from infra.research.store import open_readonly  # noqa: E402


def q(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a)]


STATUS: dict[str, str] = {}


def _open(path: Path, label: str):
    try:
        return open_readonly(path)
    except (FileNotFoundError, OSError) as exc:
        print(f"   [{label}] {exc}")
        STATUS[label] = "MISSING" if isinstance(exc, FileNotFoundError) else f"FAILED {type(exc).__name__}"
        return None


def _naive_kst(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    if t.tzinfo is not None:
        raise ValueError("시간대가 붙은 시각은 받지 않음 — 한국 시간을 시간대 없이(예 2026-10-08T18:10:06)")
    return t


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W2 실측 진단(읽기 전용)")
    ap.add_argument("code", nargs="?", default="005930")
    ap.add_argument("signal_date", nargs="?", default="2026-10-08")
    ap.add_argument("--as-of", help="'그 시각 기준' 조회 시각(한국 시간 ISO, 예 2026-10-08T18:10:06)")
    ap.add_argument("--root", default=str(Path.cwd()), help="data/ 가 있는 프로젝트(또는 사본) 루트")
    ap.add_argument("--out", help="결과를 UTF-8 파일로 저장(예 logs\\diag_20261013.txt)")
    a = ap.parse_args(argv)
    if a.out:
        out = Path(a.out).resolve()
        data = (Path(a.root) / "data").resolve()
        if out == data or data in out.parents:
            print(f"[사용법 오류] --out은 data/ 밖이어야 함: {out}")
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh, contextlib.redirect_stdout(fh):
            rc = _main(a)
        print(f"[진단] {out} 에 저장 (UTF-8) · 종료 코드 {rc}")
        return rc
    return _main(a)


def _main(a) -> int:
    try:
        date.fromisoformat(a.signal_date)
        as_of_arg = _naive_kst(a.as_of) if a.as_of else None
    except ValueError as exc:
        print(f"[사용법 오류] {exc} (신호일 {a.signal_date!r}, --as-of {a.as_of!r})")
        return 2
    STATUS.clear()
    root, code, sig = Path(a.root), a.code, a.signal_date
    wdb, rdb = root / "data/watch/watch.sqlite3", root / "data/research/research.sqlite3"
    ddb, sdb = root / "data/watch/daemon.sqlite3", root / "data/watch/watch_s1.sqlite3"
    print(f"# W2 진단 {datetime.now().isoformat(timespec='seconds')} · 종목 {code} · 신호일 {sig} · 읽기 전용\n")

    print("## 1. 준비 상태 — 지금(마지막 판정) (watch.sqlite3 readiness)")
    w = _open(wdb, "watch")
    if w:
        STATUS["watch"] = "OK"
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
        STATUS["research"] = "OK"
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
    if d:
        STATUS["daemon"] = "OK"
    tasks = [] if d is None else q(d, "SELECT task_key,kind,trading_day,status,attempts,started_at,finished_at,"
                                      "config_version,contract_hash,detail_json FROM task ORDER BY trading_day, kind")
    as_of, basis = None, ""
    if as_of_arg is not None:
        as_of, basis = as_of_arg, "--as-of 지정"
    else:
        preps = [json.loads(t["detail_json"] or "{}").get("prepare") for t in tasks
                 if t["kind"] == "CLOSE_PREP" and t["trading_day"] == sig]
        preps = [x for x in preps if x and x.get("run_id")]
        prep = preps[-1] if preps else None
        if prep and w:
            cols = {c[1] for c in w.execute("PRAGMA table_info(prepare_run)")}
            sel = "finished_at" + (", evaluated_as_of" if "evaluated_as_of" in cols else "")
            row = q(w, f"SELECT {sel} FROM prepare_run WHERE run_id=?", prep["run_id"])
            if row and row[0].get("evaluated_as_of"):
                as_of, basis = (datetime.fromisoformat(row[0]["evaluated_as_of"]),
                                f"준비 실행 {prep['run_id']} 판정 기준 시각(기록값)")
            elif row and row[0]["finished_at"]:
                as_of, basis = (datetime.fromisoformat(row[0]["finished_at"]),
                                f"준비 실행 {prep['run_id']} 완료 시각(근사 — 판정 순간 자체 아님)")

    print("\n## 3. 연구 계산 입력")
    STATUS["series"] = "OK"
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
                    STATUS["series"] = "MISSING(시계열 없음)" if isinstance(exc, KeyError) else f"FAILED {type(exc).__name__}"
    except Exception as exc:                               # noqa: BLE001
        print("   연구 DB를 읽기 전용으로 열 수 없음:", type(exc).__name__, exc)
        STATUS["series"] = "MISSING" if isinstance(exc, FileNotFoundError) else f"FAILED {type(exc).__name__}"

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
    STATUS["s1"] = "OK"
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
        STATUS["s1"] = "MISSING" if isinstance(exc, FileNotFoundError) else f"FAILED {type(exc).__name__}"
    bad = {k: v for k, v in STATUS.items() if v != "OK"}
    print("\n## 요약: " + " · ".join(f"{k} {v}" for k, v in STATUS.items())
          + (" — 부분 진단(종료 코드 1)" if bad else " — 모두 OK"))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                       # noqa: BLE001
        pass
    sys.exit(main())
