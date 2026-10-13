"""diag_w2.py(W2 실측 진단) 시험 — 읽기 전용·이전 스키마 유지·조회 시각 근거. 임시 폴더만, API 호출 없음.

    python test_diag_w2.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import diag_w2  # noqa: E402
from infra.research.scan_store import ScanStore  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from infra.watch.daemon import COMPLETE, DaemonStore  # noqa: E402
from infra.watch.store import WatchStore  # noqa: E402

results: list[tuple[str, bool]] = []


def check(label: str, cond: bool) -> None:
    results.append((label, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")


TMP = Path(tempfile.mkdtemp(prefix="diag_w2_"))


def make_root(name: str, *, with_prep: bool = True) -> Path:
    root = TMP / name
    (root / "data/watch").mkdir(parents=True)
    (root / "data/research").mkdir(parents=True)
    ResearchStore(root / "data/research/research.sqlite3").close()
    ScanStore(root / "data/watch/watch_s1.sqlite3").close()
    with WatchStore(root / "data/watch/watch.sqlite3") as w:
        rid = w.begin_prepare(1, datetime(2026, 10, 12, 18, 10, 3), ["STOCK:005930"])
        w.finish_prepare(rid, datetime(2026, 10, 12, 18, 10, 7), "COMPLETE", {"fetch": {"APPEND": 1}})
    with DaemonStore(root / "data/watch/daemon.sqlite3") as ds:
        if with_prep:
            key = "CLOSE_PREP|2026-10-12|scope:v2:test"
            ds.ensure_task(key, "CLOSE_PREP", date(2026, 10, 12), "v2:test", datetime(2026, 10, 12, 18, 10),
                           datetime(2026, 10, 12, 18, 10))
            ds.finish_task(key, COMPLETE, datetime(2026, 10, 12, 18, 10, 8), detail={"prepare": {"run_id": rid}})
    return root


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


RC: list[int] = []


def run(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        RC.append(diag_w2.main(list(argv)))
    return buf.getvalue()


# 1. 읽기 전용 — 파일 목록·내용 그대로, 없는 DB는 만들지 않음
R1 = make_root("ro")
before = snapshot(R1)
out1 = run("005930", "2026-10-12", "--root", str(R1))
R0 = TMP / "empty"
R0.mkdir()
out0 = run("005930", "2026-10-12", "--root", str(R0))
check("1) 진단은 모든 DB를 읽기 전용으로 엶 — 실행 전후 파일 목록·SHA256 같음, DB가 없는 폴더에는 아무 파일도 만들지 않고 '없음'만 표시",
      snapshot(R1) == before and "## 5." in out1 and list(R0.rglob("*")) == [] and "DB 파일 없음" in out0)

# 2. 이전 스키마는 올리지 않음(백업도 만들지 않음)
R2 = make_root("old")
for db, key, old in (("data/research/research.sqlite3", "schema_version", "r4"),
                     ("data/watch/watch_s1.sqlite3", "scan_schema", "s3")):
    with sqlite3.connect(R2 / db) as cx:
        cx.execute("UPDATE meta SET value=? WHERE key=?", (old, key))
before2 = snapshot(R2)
out2 = run("005930", "2026-10-12", "--root", str(R2))
check("2) 연구 DB r4·관찰 DB s3(이전 판)는 올리지 않고 불일치를 출력 — 파일·백업 생성 없음(이전 진단은 r4→r5로 바꾸고 백업을 만들었음)",
      snapshot(R2) == before2 and "읽기 전용이라 올리지 않음" in out2 and "r4" in out2 and "s3" in out2
      and not list(R2.rglob("*.bak-*")))

# 3. 조회 시각 근거: 기록된 준비 완료 시각 / --as-of / 기록 없으면 추정 안 함
out3a = run("005930", "2026-10-12", "--root", str(R1))
out3b = run("005930", "2026-10-12", "--root", str(R1), "--as-of", "2026-10-12T18:10:06")
R3 = make_root("noprep", with_prep=False)
out3c = run("005930", "2026-10-12", "--root", str(R3))
check("3) '그 시각 기준' 조회 시각 = 그 신호일 CLOSE_PREP가 기록한 준비 실행의 완료 시각(근사로 표시) 또는 --as-of — 고정 시각을 쓰지 않음",
      "as_of=2026-10-12 18:10:07 [준비 실행 prep_" in out3a and "근사" in out3a
      and "as_of=2026-10-12 18:10:06 [--as-of 지정]" in out3b and "18:10:07 [" not in out3b)
check("4) 그 신호일의 준비 완료 기록이 없으면 시각을 추정하지 않음 — 지금(현재 revision)만 보여 주고 --as-of 안내",
      "시각을 추정하지 않음" in out3c and "as_of=None [지금(현재 revision)]" in out3c and "[준비 실행" not in out3c)

# 5. 판정 기준 시각이 기록돼 있으면 그 값(정확)을 씀
R5 = make_root("evaluated")
with WatchStore(R5 / "data/watch/watch.sqlite3") as w:
    rid5 = w.prepare_runs(1)[0]["run_id"]
    w.finish_prepare(rid5, datetime(2026, 10, 12, 18, 10, 7), "COMPLETE", {}, evaluated_as_of=datetime(2026, 10, 12, 18, 10, 7))
out5 = run("005930", "2026-10-12", "--root", str(R5))
check("5) 준비 실행에 판정 기준 시각(evaluated_as_of)이 있으면 그 값(기록값)으로 조회 — 완료 시각 근사보다 우선",
      "as_of=2026-10-12 18:10:07 [준비 실행" in out5 and "판정 기준 시각(기록값)" in out5)

# 6. 입력 검증·요약·종료 코드
before6 = snapshot(R1)
out6a = run("005930", "2026-10-12", "--root", str(R1), "--as-of", "2026-10-12T18:10:06+09:00")
rc6a = RC[-1]
out6b = run("005930", "2026-13-40", "--root", str(R1))
rc6b = RC[-1]
run("005930", "2026-10-12", "--root", str(R1))
rc6c = RC[-1]
out6d = run("005930", "2026-10-12", "--root", str(R0))
rc6d = RC[-1]
check("6) 입력 검증: 시간대가 붙은 --as-of·잘못된 신호일은 사용법 오류(종료 코드 2, 아무것도 열지 않음)",
      rc6a == 2 and "시간대" in out6a and rc6b == 2 and "사용법 오류" in out6b and snapshot(R1) == before6)
out6c = run("005930", "2026-10-12", "--root", str(R1))
check("7) 끝에 구역별 요약과 종료 코드 — 빠진 DB·시계열이 있으면 MISSING으로 표시하고 1(부분 진단), 성공처럼 0을 내지 않음",
      rc6c == 1 and "series MISSING(시계열 없음)" in out6c and "watch OK" in out6c
      and rc6d == 1 and "## 요약:" in out6d and "watch MISSING" in out6d and "부분 진단" in out6d)

# 8. --out: UTF-8 파일로 직접 저장(Windows `>` 리다이렉트 cp949 실패 대응), data/ 안은 거부
O8 = TMP / "out" / "diag.txt"
before8 = snapshot(R1)
msg8 = run("005930", "2026-10-12", "--root", str(R1), "--out", str(O8))
rc8 = RC[-1]
txt8 = O8.read_text(encoding="utf-8") if O8.exists() else ""
msg8b = run("005930", "2026-10-12", "--root", str(R1), "--out", str(R1 / "data" / "x.txt"))
rc8b = RC[-1]
check("8) --out은 결과를 UTF-8 파일로 저장('—' 등 cp949 밖 문자 포함, 종료 코드 그대로), data/ 안 경로는 거부(2) — DB 그대로",
      rc8 == 1 and "## 1. 준비 상태 — 지금" in txt8 and "## 요약:" in txt8 and "UTF-8" in msg8
      and rc8b == 2 and not (R1 / "data" / "x.txt").exists() and snapshot(R1) == before8)

# 9. --out 기존 파일 거부 — DB 하드링크·이전 보고서를 자르지 않음 (GPT 검토 88686b9 P1)
before9 = snapshot(R1)
rdb9 = R1 / "data/research/research.sqlite3"
size9 = rdb9.stat().st_size
link9 = TMP / "out" / "link.txt"
os.link(rdb9, link9)
msg9 = run("005930", "2026-10-12", "--root", str(R1), "--out", str(link9))
rc9 = RC[-1]
old9 = O8.read_bytes()
run("005930", "2026-10-12", "--root", str(R1), "--out", str(O8))
rc9b = RC[-1]
check("9) --out 대상이 이미 있으면(DB 하드링크·이전 보고서) 거부(2) — 원본 DB 크기·내용과 이전 보고서 그대로",
      rc9 == 2 and "이미 있음" in msg9 and rdb9.stat().st_size == size9 and snapshot(R1) == before9
      and rc9b == 2 and O8.read_bytes() == old9)

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
