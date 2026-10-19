# -*- coding: utf-8 -*-
"""D1 (GPT 10/19 검토 3장): 입력 재확인이 선행 마감 준비(스캔)보다 먼저 돌면 NO_SCAN으로 끝내지 않고 기다림.

    python test_input_check_wait.py

재현(10/16 실제 DB): 08:41:09 INPUT_CHECK → COMPLETE(NO_SCAN) → 08:41:10 전날 CLOSE_PREP 시작 → 08:41:26 스캔 완료 → 재확인 없음.
가짜 시계·가짜 키움(test_watch_daemon.py 장치), 임시 폴더만.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

_HEAD = (Path(__file__).resolve().parent / "test_watch_daemon.py").read_text(encoding="utf-8").split("# ── 1. 일정")[0]
exec(compile(_HEAD, "test_watch_daemon_head", "exec"))

D108 = date(2026, 10, 8)
IK = "INPUT_CHECK|2026-10-08|OPEN-20m"


def late_morning(name: str, start=datetime(2026, 10, 8, 8, 41), symbols=None):
    """10/7 마감 준비를 하지 못한 채(관리자 꺼짐) 10/8 아침에 관리자를 켬."""
    e = env(name, datetime(2026, 10, 7, 12, 0), symbols or [S_5930])
    d = daemon(e)
    d.dstore.set_meta("since", "2026-10-07")
    d.dstore.conn.execute("DELETE FROM task")
    at(e, start)
    return e, d


def drive(e, d, until: datetime, step_sec=61, max_ticks=60):
    """tick을 돌리며 실행 순서 기록. 할 일이 없으면 시계를 step만큼 진행."""
    seq = []
    for _ in range(max_ticks):
        r = d.tick()
        if r.get("ran"):
            seq.append((e["clock"].t.strftime("%H:%M:%S"), r["ran"].split("|")[0], r["status"]))
        else:
            if e["clock"].t >= until:
                break
            e["clock"].t += timedelta(seconds=step_sec)
    return seq


def ic_task(e):
    with D.DaemonStore(e["paths"].daemon_db) as ds:
        return ds.task(IK)


def prep_tasks(e, day="2026-10-07"):
    with D.DaemonStore(e["paths"].daemon_db) as ds:
        return [t for t in ds.tasks(day) if t["kind"] == "CLOSE_PREP"]


# 1. 늦은 아침 기동 → 스캔 없음 → 마감 준비 → 기한 전 실제 비교
E1, d1 = late_morning("late")
seq1 = drive(E1, d1, datetime(2026, 10, 8, 8, 54))
t1 = ic_task(E1)
rows1 = d1.dstore.input_checks("2026-10-08")
p1 = prep_tasks(E1)
kinds1 = [k for _, k, _ in seq1]
first_ic = next(i for i, x in enumerate(seq1) if x[1] == "INPUT_CHECK")
check("1-1) 08:41 기동: 입력 재확인이 먼저 돌아도 스캔이 없고 10/7 마감 준비가 남아 있으면 NO_SCAN으로 끝내지 않음(YIELDED WAIT_SCAN) "
      "→ 마감 준비 완료 → 다시 입력 재확인(실제 비교) → STABLE, 승인 [005930], 모두 08:55 전",
      seq1[first_ic][2] == "YIELDED" and any(k == "CLOSE_PREP" and s == "COMPLETE" for _, k, s in seq1)
      and t1["status"] == "COMPLETE" and t1["detail"]["check_mode"] == "COMPARED"
      and t1["detail"]["approved"] == ["005930"] and sorted(r["status"] for r in rows1) == ["STABLE", "STABLE"]
      and all(r["committed_at"] < "2026-10-08T08:55:00" for r in rows1) and t1["finished_at"] < "2026-10-08T08:55:00")
prep_idx = next(i for i, x in enumerate(seq1) if x[1] == "CLOSE_PREP" and x[2] == "COMPLETE")
ic_runs_between = [x for x in seq1[first_ic + 1:prep_idx] if x[1] == "INPUT_CHECK"]
prep_yields = [x for x in seq1 if x[1] == "CLOSE_PREP" and x[2] == "YIELDED"]
check("1-2) 우선순위 역전 없음: 기다리는 입력 재확인이 마감 준비를 양보시키지 않음(마감 준비 YIELDED 0), 스캔 대기 중 입력 재확인은 "
      "마감 준비 뒤 순서, 마감 준비 완료 전 재실행 없음",
      not prep_yields and not ic_runs_between and len([x for x in seq1 if x[1] == "INPUT_CHECK"]) == 2)
rep1 = Path(t1["report_path"]).read_text(encoding="utf-8")
check("1-3) 보고서·작업 기록에 방식 표시: 실제 비교(입력 창 다시 조회). 대기 이력은 사건으로 남음",
      "실제 비교" in rep1 and any("WAIT_SCAN" in json.dumps(tuple(ev), ensure_ascii=False, default=str)
                                   for ev in d1.dstore.conn.execute("SELECT * FROM task_event WHERE task_key=?", (IK,)))
      if d1.dstore.conn.execute("SELECT name FROM sqlite_master WHERE name='task_event'").fetchone()
      else "실제 비교" in rep1)
d1.stop()

# 1-4. 마감 준비가 오래 걸리는 경우(여러 번 양보) — 순서·선점 직접 확인
E7, d7 = late_morning("long_prep")
for _ in range(3):
    if (ic_task(E7) or {}).get("status") == "YIELDED":
        break
    d7.tick()
w7 = ic_task(E7)
pk = prep_tasks(E7)[0]["task_key"]
d7.dstore.defer(pk, E7["clock"].t, E7["clock"].t, "TIME_BUDGET(시험)")     # 마감 준비가 받은 만큼 저장하고 쉼 — 다시 실행 가능
at(E7, E7["clock"].t + timedelta(seconds=61))                              # 입력 재확인 재시도 시각도 지남
d7._current_kind = "CLOSE_PREP"
yr = d7.yield_reason(E7["clock"].t)
d7._current_kind = None
r7 = d7.tick()
check("1-4) 둘 다 다시 실행 가능할 때: 실행 중인 마감 준비는 스캔을 기다리는 입력 재확인 때문에 양보하지 않음(PRIORITY 없음), "
      "다음 실행도 마감 준비가 먼저(입력 재확인이 반복 선점하지 않음)",
      w7["status"] == "YIELDED" and yr is None and (r7["ran"] or "").startswith("CLOSE_PREP"))
d7.stop()

# 2. 대기 중 시계가 기한을 넘기면 MISSED(신규 진입 불가)
E2, d2 = late_morning("deadline", start=datetime(2026, 10, 8, 8, 54))
E2["fake"].fail["005930"] = ["RC"] * 50            # 마감 준비가 끝나지 않게(조회 실패 반복)
seq2 = drive(E2, d2, datetime(2026, 10, 8, 8, 58), step_sec=61)
t2 = ic_task(E2)
check("2-1) 08:54 기동·마감 준비가 기한(08:55)까지 못 끝남 → 입력 재확인 MISSED(그날 승인 없음), 08:55 뒤 확인을 근거로 쓰지 않음",
      t2["status"] == "MISSED" and "승인되지 않음" in (t2["error"] or "") and not d2.dstore.input_checks("2026-10-08"))
d2.stop()

# 3. 정상 스캔의 후보 0건 → 조회 없이 완료(기다리지 않음)
E3 = env("nocand", datetime(2026, 10, 7, 19, 0), [{"code": "000660", "interest": {"enabled": True, "s1_analysis": True}}])
d3 = daemon(E3)
d3.dstore.set_meta("since", "2026-10-07")
d3.dstore.conn.execute("DELETE FROM task")
d3.tick()
d3.tick()
n3 = len(E3["fake"].calls)
at(E3, datetime(2026, 10, 8, 8, 40))
d3.tick()
t3 = ic_task(E3)
check("3-1) 유효한 스캔의 후보 0건 → 조회 없이 COMPLETE(NO_CANDIDATES), 방식 '조회 생략(후보 0건)' — 기다리지 않음",
      t3["status"] == "COMPLETE" and t3["detail"]["result"] == "NO_CANDIDATES"
      and t3["detail"]["check_mode"] == "SKIPPED_NO_CANDIDATES"
      and not [c for c in api_codes(E3, n3) if c[0] in ("ka10081", "ka20006")])
d3.stop()

# 4. 기다릴 마감 준비가 없으면(이미 MISSED 등) 바로 NO_SCAN
E4, d4 = late_morning("noscan_final")
d4.dstore.ensure_task("CLOSE_PREP|2026-10-07|scope:x", "CLOSE_PREP", date(2026, 10, 7), "x",
                      datetime(2026, 10, 7, 20, 30), datetime(2026, 10, 8, 8, 41))
d4.dstore.mark("CLOSE_PREP|2026-10-07|scope:x", "SUPERSEDED", datetime(2026, 10, 8, 8, 41), "test")
d4.dstore.set_meta("since", "2026-10-08")                          # 10/7 작업은 새로 만들지 않음
r4 = d4.tick()
t4 = ic_task(E4)
check("4-1) 신호일 마감 준비가 끝났거나(SUPERSEDED) 운영 시작 전이라 기다릴 대상이 없으면 NO_SCAN으로 완료 — 방식 '조회 생략(스캔 없음)'",
      t4["status"] == "COMPLETE" and t4["detail"]["result"] == "NO_SCAN"
      and t4["detail"]["check_mode"] == "SKIPPED_NO_SCAN")
d4.stop()

# 5. 재시작 — 대기 중 관리자를 껐다 켜도 작업·후보 중복 없음
E5, d5 = late_morning("restart")
r5a = d5.tick()                                                    # OPEN_CHECK 10/7 등 먼저일 수 있음
for _ in range(3):
    if (ic_task(E5) or {}).get("status") == "YIELDED":
        break
    d5.tick()
w5 = ic_task(E5)
d5.stop()
d5b = daemon(E5)
seq5 = drive(E5, d5b, datetime(2026, 10, 8, 8, 54))
with D.DaemonStore(E5["paths"].daemon_db) as ds:
    ic_rows = [t for t in ds.tasks("2026-10-08") if t["kind"] == "INPUT_CHECK"]
rows5 = d5b.dstore.input_checks("2026-10-08")
check("5-1) 스캔 대기(YIELDED WAIT_SCAN) 중 관리자 재시작 → 같은 작업 키 하나로 이어서 완료, 비교 행은 종목·지수 1벌(중복 없음)",
      w5["status"] == "YIELDED" and "WAIT_SCAN" in (w5["error"] or "") and len(ic_rows) == 1
      and ic_rows[0]["status"] == "COMPLETE" and sorted((r["role"], r["status"]) for r in rows5) ==
      [("INDEX", "STABLE"), ("STOCK", "STABLE")])
d5b.stop()

# 6. 기존 경로 유지 — 마감 준비가 이미 끝난 정상 아침(08:40)은 대기 없이 1회로
E6 = env("normal", datetime(2026, 10, 7, 19, 0), [S_5930])
d6 = daemon(E6)
d6.dstore.set_meta("since", "2026-10-07")
d6.dstore.conn.execute("DELETE FROM task")
for _ in range(3):
    d6.tick()
at(E6, datetime(2026, 10, 8, 8, 40))
r6 = d6.tick()
t6 = ic_task(E6)
check("6-1) 정상 아침(전날 스캔 완료): 08:40 한 번에 실제 비교 완료(대기 없음)",
      r6["ran"] == IK and t6["status"] == "COMPLETE" and t6["detail"]["check_mode"] == "COMPARED" and t6["attempts"] == 1)
d6.stop()

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
