from __future__ import annotations

"""감시 저장소 `data/watch/watch.sqlite3` (스키마 wa1) — 설정 적용 이력·데이터 준비 상태·준비 실행 기록.

- config_version : 설정을 읽을 때마다(내용·판정이 바뀐 경우만) 한 행. 번호(version)는 시도 순서대로 늘고,
                   APPLIED(적용 성공) / REJECTED(오류 — 적용 안 함). 원문·정규화 설정·오류·경고·대조한 목록 스냅숏 보존.
                   사용 중 설정 = 가장 최근 APPLIED (최근 시도가 REJECTED여도 그대로 — 마지막 정상 설정 유지).
- readiness      : 종목·지수별 현재 데이터 준비 상태(READY / UNKNOWN + 사유). 판정한 설정 버전·준비 실행 ID를 함께.
- readiness_log  : 상태가 바뀔 때마다 한 행(이력).
- prepare_run    : 데이터 준비(지정 종목·지수만 수집) 실행 — 설정 버전과 연결.
연구 수집 DB(research.sqlite3)와 별도 파일 — 시세·일봉은 연구 DB를 함께 씀(다시 받지 않음).
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

WATCH_DB_SCHEMA = "wa1"
APPLIED, REJECTED = "APPLIED", "REJECTED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS config_version(
  version INTEGER PRIMARY KEY AUTOINCREMENT, attempted_at TEXT NOT NULL, origin TEXT NOT NULL,
  source_path TEXT NOT NULL, raw_sha TEXT NOT NULL, status TEXT NOT NULL, config_hash TEXT,
  errors_json TEXT NOT NULL, warnings_json TEXT NOT NULL, raw_text TEXT, config_json TEXT,
  list_snapshot_id INTEGER);
CREATE TABLE IF NOT EXISTS readiness(
  target TEXT PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  detail_json TEXT NOT NULL, config_version INTEGER NOT NULL, run_id TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS readiness_log(
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  config_version INTEGER NOT NULL, run_id TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prepare_run(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE, config_version INTEGER NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, targets_json TEXT NOT NULL DEFAULT '[]',
  counts_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '');
"""


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(microsecond=0).isoformat(timespec="seconds")


class WatchStore:
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='watch_schema'").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES('watch_schema', ?)", (WATCH_DB_SCHEMA,))
        elif cur[0] != WATCH_DB_SCHEMA:
            raise RuntimeError(f"감시 저장소 스키마 {cur[0]} ≠ {WATCH_DB_SCHEMA}: {self.path}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ── 설정 이력 ────────────────────────────────────────────
    @staticmethod
    def _version(r) -> dict:
        d = dict(r)
        d["errors"] = json.loads(d.pop("errors_json"))
        d["warnings"] = json.loads(d.pop("warnings_json"))
        d["config"] = None if d["config_json"] is None else json.loads(d["config_json"])
        return d

    def record_attempt(self, *, at: datetime, origin: str, source_path: str, raw_sha: str, status: str,
                       config_hash: str | None, errors: list, warnings: list, raw_text: str | None,
                       config: dict | None, list_snapshot_id: int | None) -> tuple[int, bool]:
        """(버전, 새로 기록했는지). 직전 시도와 원문·판정·오류가 같으면 새 행을 만들지 않음(같은 파일 반복 확인)."""
        errs, warns = json.dumps(errors, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False)
        with self.tx():
            last = self.conn.execute("SELECT * FROM config_version ORDER BY version DESC LIMIT 1").fetchone()
            if (last is not None and last["raw_sha"] == raw_sha and last["status"] == status
                    and last["errors_json"] == errs and last["config_hash"] == config_hash):
                return last["version"], False
            cur = self.conn.execute(
                "INSERT INTO config_version(attempted_at, origin, source_path, raw_sha, status, config_hash,"
                " errors_json, warnings_json, raw_text, config_json, list_snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (_ts(at), origin, source_path, raw_sha, status, config_hash, errs, warns, raw_text,
                 None if config is None else json.dumps(config, ensure_ascii=False, sort_keys=True), list_snapshot_id))
            return cur.lastrowid, True

    def latest_attempt(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM config_version ORDER BY version DESC LIMIT 1").fetchone()
        return None if r is None else self._version(r)

    def active_version(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM config_version WHERE status=? ORDER BY version DESC LIMIT 1", (APPLIED,)).fetchone()
        return None if r is None else self._version(r)

    def history(self, limit: int = 20) -> list[dict]:
        return [self._version(r) for r in self.conn.execute(
            "SELECT * FROM config_version ORDER BY version DESC LIMIT ?", (limit,))]

    # ── 데이터 준비 ──────────────────────────────────────────
    def begin_prepare(self, version: int, at: datetime, targets: list[str]) -> str:
        with self.tx():
            seq = self.conn.execute(
                "INSERT INTO prepare_run(config_version, started_at, status, targets_json) VALUES(?,?,?,?)",
                (version, _ts(at), "RUNNING", json.dumps(targets))).lastrowid
            run_id = f"prep_{at:%Y%m%d}_{seq}"
            self.conn.execute("UPDATE prepare_run SET run_id=? WHERE seq=?", (run_id, seq))
        return run_id

    def finish_prepare(self, run_id: str, at: datetime, status: str, counts: dict, note: str = "") -> None:
        self.conn.execute("UPDATE prepare_run SET finished_at=?, status=?, counts_json=?, note=? WHERE run_id=?",
                          (_ts(at), status, json.dumps(counts, ensure_ascii=False), note, run_id))

    def prepare_runs(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM prepare_run ORDER BY seq DESC LIMIT ?", (limit,))]

    def set_readiness(self, rows: list[dict], *, version: int, run_id: str | None, at: datetime) -> int:
        """상태 저장. 바뀐 대상만 이력에 한 행. 반환: 바뀐 수. 다른 대상의 기록은 건드리지 않음."""
        changed = 0
        with self.tx():
            for r in rows:
                old = self.conn.execute("SELECT status, reason FROM readiness WHERE target=?", (r["target"],)).fetchone()
                self.conn.execute(
                    "INSERT OR REPLACE INTO readiness(target, kind, code, status, reason, detail_json, config_version,"
                    " run_id, checked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (r["target"], r["kind"], r["code"], r["status"], r["reason"],
                     json.dumps(r.get("detail", {}), ensure_ascii=False, default=str), version, run_id, _ts(at)))
                if old is None or (old["status"], old["reason"]) != (r["status"], r["reason"]):
                    changed += 1
                    self.conn.execute("INSERT INTO readiness_log(target, status, reason, config_version, run_id,"
                                      " checked_at) VALUES(?,?,?,?,?,?)",
                                      (r["target"], r["status"], r["reason"], version, run_id, _ts(at)))
        return changed

    def readiness(self) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT * FROM readiness ORDER BY target"):
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out[d["target"]] = d
        return out

    def readiness_log(self, target: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM readiness_log", ()
        if target:
            q, a = q + " WHERE target=?", (target,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY log_id", a)]
