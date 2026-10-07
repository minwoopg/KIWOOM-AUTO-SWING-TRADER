from __future__ import annotations

"""감시 저장소 `data/watch/watch.sqlite3` (스키마 wa1) — 설정 적용 이력·데이터 준비 상태·준비 실행 기록.

- config_version : 설정을 읽을 때마다(내용·판정이 바뀐 경우만) 한 행. 번호(version)는 시도 순서대로 늘고,
                   APPLIED(적용 성공) / REJECTED(오류 — 적용 안 함). 원문·정규화 설정·오류·경고·대조한 목록 스냅숏 보존.
                   사용 중 설정 = 가장 최근 APPLIED (최근 시도가 REJECTED여도 그대로 — 마지막 정상 설정 유지).
- readiness      : 종목·지수별 현재 데이터 준비 상태(READY / UNKNOWN + 사유). 판정한 설정 버전·준비 실행 ID를 함께.
- readiness_log  : 상태가 바뀔 때마다 한 행(이력).
- prepare_run    : 데이터 준비(지정 종목·지수만 수집) 실행 — 설정 버전과 연결.
- config_check   : 같은 버전을 다른 종목 목록 스냅숏으로 다시 검증한 기록(버전은 그대로, 목록·경고 변화 보존).
- holding_close  : 수동 보유 청산 기록(holding-close) — 이 기록 없이 보유 정보가 사라진 설정은 적용하지 않음(W1-R1).
- symbol_risk    : 종목별 위험 자격 — 최신 종목 목록으로 설정을 읽을 때마다 갱신(W1-R2). 바뀌면 symbol_risk_log.
readiness는 가격 데이터 준비(status)와 S1 분석 준비(analysis_status)를 따로 둠(W1-R3).
wa1 → wa2: 열 때 백업 후 열·표 추가(기존 기록 그대로, 분석 준비는 다음 준비 때 채움).
연구 수집 DB(research.sqlite3)와 별도 파일 — 시세·일봉은 연구 DB를 함께 씀(다시 받지 않음).
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

WATCH_DB_SCHEMA = "wa2"
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
  detail_json TEXT NOT NULL, config_version INTEGER NOT NULL, run_id TEXT, checked_at TEXT NOT NULL,
  analysis_status TEXT, analysis_reason TEXT);
CREATE TABLE IF NOT EXISTS readiness_log(
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
  config_version INTEGER NOT NULL, run_id TEXT, checked_at TEXT NOT NULL, analysis_status TEXT, analysis_reason TEXT);
CREATE TABLE IF NOT EXISTS prepare_run(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE, config_version INTEGER NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, targets_json TEXT NOT NULL DEFAULT '[]',
  counts_json TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '');
"""
_SCHEMA_WA2 = """
CREATE TABLE IF NOT EXISTS config_check(
  check_id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
  list_snapshot_id INTEGER, warnings_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holding_close(
  close_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, closed_at TEXT NOT NULL, holding_json TEXT NOT NULL,
  from_version INTEGER, origin TEXT NOT NULL, state TEXT NOT NULL, used_version INTEGER);
CREATE TABLE IF NOT EXISTS symbol_risk(
  code TEXT PRIMARY KEY, status TEXT NOT NULL, flags_json TEXT NOT NULL, list_snapshot_id INTEGER,
  config_version INTEGER, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS symbol_risk_log(
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, status TEXT NOT NULL, flags_json TEXT NOT NULL,
  list_snapshot_id INTEGER, config_version INTEGER, checked_at TEXT NOT NULL);
"""
CLOSE_OPEN, CLOSE_USED, CLOSE_VOID = "OPEN", "USED", "VOID"


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
        self.backup_path: str | None = None
        cur = self.conn.execute("SELECT value FROM meta WHERE key='watch_schema'").fetchone()
        if cur is None:
            self.conn.executescript(_SCHEMA_WA2)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('watch_schema', ?)", (WATCH_DB_SCHEMA,))
        elif cur[0] == "wa1":
            from infra.research.store import sqlite_backup
            self.backup_path = sqlite_backup(self.conn, self.path, "wa1")
            with self.tx():
                for t in ("readiness", "readiness_log"):
                    cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")}
                    for c in ("analysis_status", "analysis_reason"):
                        if c not in cols:
                            self.conn.execute(f"ALTER TABLE {t} ADD COLUMN {c} TEXT")
                for stmt in _SCHEMA_WA2.strip().split(";"):
                    if stmt.strip():
                        self.conn.execute(stmt)
                self.conn.execute("UPDATE meta SET value=? WHERE key='watch_schema'", (WATCH_DB_SCHEMA,))
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
                    and last["errors_json"] == errs and last["config_hash"] == config_hash
                    and last["warnings_json"] == warns):
                self._check(at, last["version"], status, list_snapshot_id, warns)
                return last["version"], False
            cur = self.conn.execute(
                "INSERT INTO config_version(attempted_at, origin, source_path, raw_sha, status, config_hash,"
                " errors_json, warnings_json, raw_text, config_json, list_snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (_ts(at), origin, source_path, raw_sha, status, config_hash, errs, warns, raw_text,
                 None if config is None else json.dumps(config, ensure_ascii=False, sort_keys=True), list_snapshot_id))
            self._check(at, cur.lastrowid, status, list_snapshot_id, warns)
            return cur.lastrowid, True

    def _check(self, at, version, status, snapshot_id, warns) -> None:
        """같은 버전·같은 목록이면 생략, 목록 스냅숏이 바뀌면 다시 검증한 기록을 남김 (W1-R2)."""
        last = self.conn.execute("SELECT version, list_snapshot_id FROM config_check ORDER BY check_id DESC LIMIT 1"
                                 ).fetchone()
        if last is not None and (last[0], last[1]) == (version, snapshot_id):
            return
        self.conn.execute("INSERT INTO config_check(at, version, status, list_snapshot_id, warnings_json)"
                          " VALUES(?,?,?,?,?)", (_ts(at), version, status, snapshot_id, warns))

    def config_checks(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM config_check ORDER BY check_id")]

    # ── 수동 보유 청산 기록 (W1-R1) ──────────────────────────
    def record_close(self, code: str, holding: dict, *, at: datetime, from_version: int | None, origin: str) -> int:
        with self.tx():
            self.conn.execute("UPDATE holding_close SET state=? WHERE code=? AND state=?", (CLOSE_VOID, code, CLOSE_OPEN))
            return self.conn.execute(
                "INSERT INTO holding_close(code, closed_at, holding_json, from_version, origin, state)"
                " VALUES(?,?,?,?,?,?)", (code, _ts(at), json.dumps(holding, sort_keys=True), from_version, origin,
                                         CLOSE_OPEN)).lastrowid

    def open_close(self, code: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM holding_close WHERE code=? AND state=? ORDER BY close_id DESC LIMIT 1",
                              (code, CLOSE_OPEN)).fetchone()
        return None if r is None else {**dict(r), "holding": json.loads(r["holding_json"])}

    def settle_closes(self, used: dict[str, int], void_codes: list[str], version: int) -> None:
        with self.tx():
            for code, cid in used.items():
                self.conn.execute("UPDATE holding_close SET state=?, used_version=? WHERE close_id=?",
                                  (CLOSE_USED, version, cid))
            for code in void_codes:
                self.conn.execute("UPDATE holding_close SET state=? WHERE code=? AND state=?",
                                  (CLOSE_VOID, code, CLOSE_OPEN))

    def closes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM holding_close ORDER BY close_id")]

    # ── 위험 자격 (W1-R2) ────────────────────────────────────
    def set_risk(self, rows: list[dict], *, snapshot_id: int | None, version: int | None, at: datetime) -> int:
        changed = 0
        with self.tx():
            for r in rows:
                flags = json.dumps(r["flags"], ensure_ascii=False)
                old = self.conn.execute("SELECT status, flags_json, list_snapshot_id FROM symbol_risk WHERE code=?",
                                        (r["code"],)).fetchone()
                self.conn.execute("INSERT OR REPLACE INTO symbol_risk(code, status, flags_json, list_snapshot_id,"
                                  " config_version, checked_at) VALUES(?,?,?,?,?,?)",
                                  (r["code"], r["status"], flags, snapshot_id, version, _ts(at)))
                if old is None or (old["status"], old["flags_json"]) != (r["status"], flags):
                    changed += 1
                    self.conn.execute("INSERT INTO symbol_risk_log(code, status, flags_json, list_snapshot_id,"
                                      " config_version, checked_at) VALUES(?,?,?,?,?,?)",
                                      (r["code"], r["status"], flags, snapshot_id, version, _ts(at)))
        return changed

    def risk(self) -> dict[str, dict]:
        out = {}
        for r in self.conn.execute("SELECT * FROM symbol_risk"):
            d = dict(r)
            d["flags"] = json.loads(d.pop("flags_json"))
            out[d["code"]] = d
        return out

    def risk_log(self, code: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM symbol_risk_log", ()
        if code:
            q, a = q + " WHERE code=?", (code,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY log_id", a)]

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
                old = self.conn.execute("SELECT status, reason, analysis_status, analysis_reason FROM readiness"
                                        " WHERE target=?", (r["target"],)).fetchone()
                key = (r["status"], r["reason"], r["analysis_status"], r["analysis_reason"])
                self.conn.execute(
                    "INSERT OR REPLACE INTO readiness(target, kind, code, status, reason, detail_json, config_version,"
                    " run_id, checked_at, analysis_status, analysis_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r["target"], r["kind"], r["code"], r["status"], r["reason"],
                     json.dumps(r.get("detail", {}), ensure_ascii=False, default=str), version, run_id, _ts(at),
                     r["analysis_status"], r["analysis_reason"]))
                if old is None or tuple(old) != key:
                    changed += 1
                    self.conn.execute("INSERT INTO readiness_log(target, status, reason, config_version, run_id,"
                                      " checked_at, analysis_status, analysis_reason) VALUES(?,?,?,?,?,?,?,?)",
                                      (r["target"], r["status"], r["reason"], version, run_id, _ts(at),
                                       r["analysis_status"], r["analysis_reason"]))
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
