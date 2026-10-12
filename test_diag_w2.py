"""diag_w2.py(W2 실측 진단) 시험 — 읽기 전용·이전 스키마 유지·조회 시각 근거. 임시 폴더만, API 호출 없음.

    python test_diag_w2.py
"""
from __future__ import annotations

import contextlib
import hashlib
import io
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


def run(*argv) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        diag_w2.main(list(argv))
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

shutil.rmtree(TMP, ignore_errors=True)
passed = sum(1 for _, ok in results if ok)
print(f"\n총 {len(results)}건 중 통과 {passed}건, 실패 {len(results) - passed}건")
sys.exit(0 if passed == len(results) else 1)
