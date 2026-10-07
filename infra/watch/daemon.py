from __future__ import annotations

"""W2 조회 전용 상시 실행 관리자 — 지정 종목 일봉 준비·S1 관찰·개장 확인을 거래일 달력에 맞춰 자동 실행 (주문 없음).

작업 (거래일 D, 시각은 Asia/Seoul — utils.time_utils.now_local 규약)
| 작업 | 실행 가능 시각 | 하는 일 |
|---|---|---|
| CLOSE_PREP | D 정규장 종료 + 완성 지연(잠정 160분) 뒤, D가 "가장 최근 완성 거래일"인 동안 | 종목 목록 갱신(그날 마감 뒤 목록이 없을 때만, 2회) → 설정 반영(적용 잠금) → 지정 종목·지수 일봉 준비 → 지정 종목 S1 관찰 → 보고서 |
| OPEN_CHECK | D 개장 + offset_min(달력의 실제 개장 — 특수 개장일 포함) 뒤 | 직전 거래일 지정 종목 S1 관찰의 개장 전 저장 완료 PASS 후보 가격 기록(A5-1 규칙 그대로 — 놓치면 MISSED, 마감 뒤 응답은 AFTER_CLOSE) |
- CLOSE_PREP(D)를 다음 거래일 완성 시각까지 못 하면 MISSED — 지난 시각의 신호를 그 뒤에 만들지 않음(다음 CLOSE_PREP가 일봉을
  이어 받음). 늦게(다음 개장 뒤) 실행된 S1 관찰은 스캐너 규칙대로 actionable=0이라 개장 후보가 되지 않음.
- OPEN_CHECK(D)는 늦게 실행돼도 A5 규칙대로 누락·지연을 기록(당시 가격으로 채우지 않음).
- 관리자를 처음 켠 날(meta.since) 이전 거래일의 작업은 만들지 않음.

작업 키·상태 (W2 검토 R4·R2)
- task_key = 종류|거래일|대상 범위. CLOSE_PREP 범위 = `v2:<해시>` — 감시·S1 대상 코드 + S1 계산 계약 해시(전략·설정·달력·
  after_close·선정 코드) + analysis_basis + history_sessions(prep_scope). 관심 가격대·수동 보유 값은 넣지 않음. 범위가 바뀌면
  같은 날 새 키로 한 번 더(이미 받은 일봉은 다시 받지 않음, 이전 COMPLETE 행 보존). OPEN_CHECK 범위 = 확인 종류(OPEN+5m).
  실행한 계산 계약·실제로 쓴 설정 버전은 행에 기록.
- 상태: PENDING → RUNNING → COMPLETE / PARTIAL(일부 조회 실패) / YIELDED(양보·미룸) / FAILED(예외) /
  ABORTED(프로세스 중단 — 다음 기동 때 표시) / MISSED / SUPERSEDED(목록 갱신 뒤 범위가 바뀌어 새 키 작업이 맡음).
- 다시 시도: PARTIAL·FAILED·ABORTED는 실패 1·2·3번째 뒤 5·15·30분, 실패 4번째(총 4회 시도)면 더 예약 안 함. YIELDED는
  실패로 세지 않고 사유별 시각(retry_at): CALL_BUDGET → 다음 날 00:00, TIME_BUDGET → poll_sec 뒤, PRIORITY·STOP → 지금.
  다시 시도 시각 전에는 계획에 나오지 않음. COMPLETE는 다시 실행하지 않음(보고서만 `report`로 다시 만듦).

우선순위·양보·예산 (R3): OPEN_CHECK > CLOSE_PREP(오래된 날 먼저). 조회 클라이언트의 guard가 실제 요청 직전마다(토큰·연속조회·
429/401 재시도 포함) request_guard를 부름 — 중지 요청·하루 상한(CLOSE_PREP는 cap − open_check_reserve)·작업 시간 상한·지금 실행
가능한 OPEN_CHECK면 RequestStopped(보내지 않음). 통과하면 보내기 전에 요청일 사용량 +1 저장. 한 프로세스·한 스레드·한 조회
클라이언트(호출 간격·재시도·토큰을 이 프로세스의 모든 작업이 공유 — 다른 조회 프로세스와는 별개) — SQLite 연결을 스레드 사이에
나누지 않음.

운영 진입 게이트 (R1): S1 계산 PASS(관찰 DB)와 운영 진입 자격(candidate_gate)을 따로 저장. SCAN 근거(관찰 직후·개장 전 설정 변경
때 다시)가 개장 전에 통과 ∧ 개장 확인 때 현재 상태로 통과해야 개장 후보. 근거 없으면 NO_GATE_EVIDENCE로 제외.

중복·재시작: 관리자 전체는 `<daemon DB>.lock` OS 잠금(이미 있으면 바로 종료 코드 2). 설정 반영은 CLI와 같은 설정 적용 잠금.
기동 때 남은 RUNNING 작업은 ABORTED로 바꾸고 다시 시도 대상. 상태(daemon_run)는 매 순회 heartbeat — 저장 실패는
정상으로 보이지 않게 예외로 올림(상위 루프가 기록 시도 후 종료).
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from infra.research import open_check as A5
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE, ResearchCollector
from infra.research.kiwoom_readonly import ReadOnlyResearchClient, RequestStopped, ResearchApiError, ResearchConfigError
from infra.research.s1_scanner import S1Scanner, ScanError, build_contract, calendar_version
from infra.research.scan_report import write_report as write_scan_report
from infra.research.scan_store import ScanStore
from infra.research.store import ResearchStore, sqlite_backup
from infra.watch.apply import file_lock, sync_file, test_point
from infra.watch.manager import WatchNotReady, analysis_basis, entry_gate, listing_from_store, prepare_data
from infra.watch.store import WatchStore
from utils.trading_calendar import CalendarCoverageError, TradingCalendar

DAEMON_SCHEMA = "wd3"
# wd1 → wd2: candidate_gate 표·daemon_run 진척/중지 요청 시각 열 추가. wd2 → wd3: candidate_gate.committed_at(저장 완료 시각,
# W2 재검토 I1) 추가 — 이전 행은 NULL(추정해 채우지 않음 = 저장 완료 증거 없음). 기존 행은 모두 보존.
_UPGRADABLE = ("wd1", "wd2")
CLOSE_PREP, OPEN_CHECK = "CLOSE_PREP", "OPEN_CHECK"
PENDING, RUNNING, COMPLETE, PARTIAL, YIELDED, FAILED, ABORTED, MISSED, SUPERSEDED = (
    "PENDING", "RUNNING", "COMPLETE", "PARTIAL", "YIELDED", "FAILED", "ABORTED", "MISSED", "SUPERSEDED")
RETRYABLE = (PARTIAL, FAILED, ABORTED)
DONE = (COMPLETE, MISSED, SUPERSEDED)
BACKOFF_SEC = (300, 900, 1800)    # 실패 1·2·3번째 뒤 5·15·30분 — 실패 4번째(총 4회 시도)면 더 예약 안 함
MAX_FAILURES = 4
SCOPE_VERSION = "v2"              # CLOSE_PREP 범위 = 대상 코드 + S1 계산 계약 + 준비 기준 (W2 검토 R4)
CAL_OK, CAL_UNAVAILABLE = "OK", "CALENDAR_UNAVAILABLE"
SELECTION_RULE = "interest.enabled ∧ interest.s1_analysis (지정 종목 — 사용자가 선택)"
_SENSITIVE = re.compile(r"(?i)(token|appkey|app_key|secret\w*|authorization|acnt\w*|account\w*)([\"']?\s*[:=]\s*)([^\s,}\"']+)")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task(
  task_key TEXT PRIMARY KEY, kind TEXT NOT NULL, trading_day TEXT NOT NULL, scope TEXT NOT NULL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
  due_at TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, next_retry_at TEXT,
  owner_run TEXT, config_version INTEGER, contract_hash TEXT, detail_json TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '', report_path TEXT, report_error TEXT);
CREATE INDEX IF NOT EXISTS ix_task_day ON task(trading_day, kind);
CREATE TABLE IF NOT EXISTS task_event(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, at TEXT NOT NULL, event TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS daemon_run(
  run_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL,
  stopped_at TEXT, state TEXT NOT NULL, current_task TEXT, last_error TEXT NOT NULL DEFAULT '',
  stop_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS call_usage(day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_gate(
  gate_id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, signal_date TEXT NOT NULL, open_at TEXT,
  contract_hash TEXT NOT NULL, symbol TEXT NOT NULL, scan_run_id TEXT, eligible INTEGER NOT NULL,
  reasons_json TEXT NOT NULL, config_version INTEGER, latest_version INTEGER, latest_status TEXT,
  list_snapshot_id INTEGER, evidence_json TEXT NOT NULL, evaluated_at TEXT NOT NULL, task_key TEXT, committed_at TEXT);
CREATE INDEX IF NOT EXISTS ix_gate ON candidate_gate(signal_date, contract_hash, symbol, stage);
"""
_ADD_COLS = {"daemon_run": (("progress", "TEXT"), ("stop_requested_at", "TEXT")),
             "candidate_gate": (("committed_at", "TEXT"),)}


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(microsecond=0).isoformat(timespec="seconds")


def redact(text: str) -> str:
    """로그 문자열의 토큰·앱키·계좌 값 가림."""
    return _SENSITIVE.sub(lambda m: m.group(1) + m.group(2) + "***", str(text))


class DaemonStore:
    """`data/watch/daemon.sqlite3` (wd1) — 작업 상태·사건·관리자 실행·호출 사용량. 감시·연구 DB와 별도 파일."""

    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        cur = None
        if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            cur = self.conn.execute("SELECT value FROM meta WHERE key='daemon_schema'").fetchone()
        if cur is not None and cur[0] != DAEMON_SCHEMA and cur[0] not in _UPGRADABLE:
            raise RuntimeError(f"관리자 저장소 스키마 {cur[0]} ≠ {DAEMON_SCHEMA}: {self.path}")
        self.upgrade_backup: str | None = None
        if cur is not None and cur[0] != DAEMON_SCHEMA:     # 올리기 전 복구용 사본(SQLite 백업 API) — 열 추가 뒤 원본으로 못 돌아감
            self.upgrade_backup = sqlite_backup(self.conn, self.path, cur[0])
        self.conn.executescript(_SCHEMA)                    # CREATE IF NOT EXISTS — 기존 표·행은 그대로
        with self.tx():
            for table, cols in _ADD_COLS.items():
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols:
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES('daemon_schema', ?)", (DAEMON_SCHEMA,))
            if cur is not None and cur[0] != DAEMON_SCHEMA:
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES('upgraded_from', ?)", (cur[0],))
                self.conn.execute("INSERT OR REPLACE INTO meta VALUES('upgrade_backup', ?)", (self.upgrade_backup,))

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

    # meta
    def meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if r is None else r[0]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))

    # tasks
    @staticmethod
    def _task(r) -> dict:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json") or "{}")
        return d

    def task(self, key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM task WHERE task_key=?", (key,)).fetchone()
        return None if r is None else self._task(r)

    def tasks(self, day: str | None = None, *, limit: int = 200) -> list[dict]:
        q, a = "SELECT * FROM task", ()
        if day:
            q, a = q + " WHERE trading_day=?", (day,)
        return [self._task(r) for r in self.conn.execute(q + " ORDER BY trading_day DESC, kind LIMIT ?", (*a, limit))]

    def ensure_task(self, key: str, kind: str, day: date, scope: str, due: datetime, now: datetime) -> dict:
        self.conn.execute("INSERT OR IGNORE INTO task(task_key, kind, trading_day, scope, status, due_at, created_at)"
                          " VALUES(?,?,?,?,?,?,?)", (key, kind, day.isoformat(), scope, PENDING, _ts(due), _ts(now)))
        return self.task(key)

    def event(self, key: str, at: datetime, event: str, detail: dict | None = None) -> None:
        self.conn.execute("INSERT INTO task_event(task_key, at, event, detail_json) VALUES(?,?,?,?)",
                          (key, _ts(at), event, json.dumps(detail or {}, ensure_ascii=False, default=str)))

    def events(self, key: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM task_event", ()
        if key:
            q, a = q + " WHERE task_key=?", (key,)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY event_id", a)]

    def start_task(self, key: str, run_id: str, now: datetime) -> None:
        with self.tx():
            self.conn.execute("UPDATE task SET status=?, attempts=attempts+1, started_at=?, owner_run=?, error=''"
                              " WHERE task_key=?", (RUNNING, _ts(now), run_id, key))
            self.event(key, now, "START", {"run_id": run_id})

    def finish_task(self, key: str, status: str, now: datetime, *, detail: dict, error: str = "",
                    config_version: int | None = None, contract_hash: str | None = None,
                    retry_at: datetime | None = None) -> dict:
        """retry_at: YIELDED일 때 다시 실행 가능한 시각(양보 사유별 — WatchDaemon.retry_at). 없으면 지금."""
        with self.tx():
            t = self.task(key)
            failures = t["failures"] + (1 if status in RETRYABLE else 0)
            nxt = None
            if status in RETRYABLE and failures < MAX_FAILURES:
                nxt = _ts(now + timedelta(seconds=BACKOFF_SEC[min(failures - 1, len(BACKOFF_SEC) - 1)]))
            elif status == YIELDED:
                nxt = _ts(retry_at or now)
            self.conn.execute(
                "UPDATE task SET status=?, failures=?, finished_at=?, next_retry_at=?, detail_json=?, error=?,"
                " config_version=COALESCE(?, config_version), contract_hash=COALESCE(?, contract_hash)"
                " WHERE task_key=?", (status, failures, _ts(now), nxt, json.dumps(detail, ensure_ascii=False,
                                                                                  default=str),
                                      redact(error)[:500], config_version, contract_hash, key))
            self.event(key, now, status, {"error": redact(error)[:300], "next_retry_at": nxt})
        return self.task(key)

    def defer(self, key: str, now: datetime, retry_at: datetime, why: str) -> bool:
        """시작하지 않고 미룸(예산 소진 등 — 시도 수를 늘리지 않음). 같은 미룸이 이미 기록돼 있으면 아무것도 안 함.
        반환: 새로 기록했는지."""
        with self.tx():
            t = self.task(key)
            if t["status"] == YIELDED and t["next_retry_at"] == _ts(retry_at) and t["error"] == why:
                return False
            self.conn.execute("UPDATE task SET status=?, next_retry_at=?, error=? WHERE task_key=?",
                              (YIELDED, _ts(retry_at), why, key))
            self.event(key, now, "DEFERRED", {"why": why, "next_retry_at": _ts(retry_at)})
        return True

    def mark(self, key: str, status: str, now: datetime, why: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE task SET status=?, finished_at=?, error=? WHERE task_key=?",
                              (status, _ts(now), why, key))
            self.event(key, now, status, {"why": why})

    def set_report(self, key: str, path: str | None, error: str | None) -> None:
        self.conn.execute("UPDATE task SET report_path=?, report_error=? WHERE task_key=?", (path, error, key))

    def abort_running(self, now: datetime, why: str) -> list[str]:
        with self.tx():
            keys = [r[0] for r in self.conn.execute("SELECT task_key FROM task WHERE status=?", (RUNNING,))]
            for k in keys:
                t = self.task(k)
                failures = t["failures"] + 1
                nxt = _ts(now) if failures < MAX_FAILURES else None
                self.conn.execute("UPDATE task SET status=?, failures=?, finished_at=?, next_retry_at=?, error=?"
                                  " WHERE task_key=?", (ABORTED, failures, _ts(now), nxt, why, k))
                self.event(k, now, ABORTED, {"why": why})
        return keys

    # daemon runs
    def begin_run(self, now: datetime) -> str:
        with self.tx():
            self.conn.execute("UPDATE daemon_run SET state='CRASHED', stopped_at=? WHERE state='RUNNING'", (_ts(now),))
            n = self.conn.execute("SELECT COUNT(*) FROM daemon_run").fetchone()[0] + 1
            run_id = f"wd_{now:%Y%m%d_%H%M%S}_{n}"
            self.conn.execute("INSERT INTO daemon_run(run_id, pid, started_at, heartbeat_at, state)"
                              " VALUES(?,?,?,?,?)", (run_id, os.getpid(), _ts(now), _ts(now), "RUNNING"))
        return run_id

    def heartbeat(self, run_id: str, now: datetime, current: str | None, last_error: str | None = None,
                  progress: str | None = None) -> None:
        self.conn.execute("UPDATE daemon_run SET heartbeat_at=?, current_task=?, last_error=COALESCE(?, last_error),"
                          " progress=? WHERE run_id=?",
                          (_ts(now), current, None if last_error is None else redact(last_error), progress, run_id))

    def end_run(self, run_id: str, now: datetime, state: str, last_error: str = "") -> None:
        self.conn.execute("UPDATE daemon_run SET state=?, stopped_at=?, current_task=NULL,"
                          " last_error=CASE WHEN ?='' THEN last_error ELSE ? END WHERE run_id=?",
                          (state, _ts(now), last_error, redact(last_error), run_id))

    def last_run(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM daemon_run ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return None if r is None else dict(r)

    def request_stop(self, now: datetime | None = None) -> bool:
        cur = self.conn.execute("UPDATE daemon_run SET stop_requested=1, stop_requested_at=COALESCE(stop_requested_at, ?)"
                                " WHERE state='RUNNING'", (_ts(now),))
        return cur.rowcount > 0

    def stop_requested(self, run_id: str) -> bool:
        r = self.conn.execute("SELECT stop_requested FROM daemon_run WHERE run_id=?", (run_id,)).fetchone()
        return bool(r and r[0])

    # call budget
    def add_calls(self, day: date, n: int) -> None:
        if n:
            self.conn.execute("INSERT INTO call_usage VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET calls=calls+?",
                              (day.isoformat(), n, n))

    def calls(self, day: date) -> int:
        r = self.conn.execute("SELECT calls FROM call_usage WHERE day=?", (day.isoformat(),)).fetchone()
        return 0 if r is None else r[0]

    # 운영 진입 게이트 근거 (W2 검토 R1) — S1 계산 PASS(관찰 DB)와 따로 저장
    def add_gates(self, rows: list[dict]) -> list[int]:
        """한 트랜잭션으로 저장하고 gate_id 목록을 돌려줌. committed_at은 여기서 쓰지 않음 — 커밋 뒤 잰 시각을
        mark_gates_committed로 따로 기록(그 사이 강제 종료면 NULL = 저장 완료 증거 없음, I1)."""
        ids = []
        with self.tx():
            for r in rows:
                ids.append(self.conn.execute(
                    "INSERT INTO candidate_gate(stage, signal_date, open_at, contract_hash, symbol, scan_run_id, eligible,"
                    " reasons_json, config_version, latest_version, latest_status, list_snapshot_id, evidence_json,"
                    " evaluated_at, task_key) VALUES(:stage,:signal_date,:open_at,:contract_hash,:symbol,:scan_run_id,"
                    ":eligible,:reasons_json,:config_version,:latest_version,:latest_status,:list_snapshot_id,"
                    ":evidence_json,:evaluated_at,:task_key)",
                    {**r, "eligible": int(bool(r["eligible"])),
                     "reasons_json": json.dumps(r["reasons"], ensure_ascii=False),
                     "evidence_json": json.dumps(r["evidence"], ensure_ascii=False, default=str)}).lastrowid)
        return ids

    def mark_gates_committed(self, ids: list[int], at: str) -> None:
        if ids:
            self.conn.execute(f"UPDATE candidate_gate SET committed_at=? WHERE gate_id IN ({','.join('?' * len(ids))})"
                              " AND committed_at IS NULL", (at, *ids))

    @staticmethod
    def _gate(r) -> dict:
        d = dict(r)
        d["reasons"] = json.loads(d.pop("reasons_json"))
        d["evidence"] = json.loads(d.pop("evidence_json"))
        return d

    def gates(self, signal_date: str | None = None, *, stage: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM candidate_gate WHERE 1=1", []
        if signal_date:
            q, a = q + " AND signal_date=?", a + [signal_date]
        if stage:
            q, a = q + " AND stage=?", a + [stage]
        return [self._gate(r) for r in self.conn.execute(q + " ORDER BY gate_id", a)]

    def latest_gate(self, signal_date: str, contract_hash: str, symbol: str, *, stage: str = "SCAN",
                    before: str | None = None) -> dict | None:
        """before가 있으면 판정(evaluated_at)과 저장 완료(committed_at, 커밋 뒤 잰 시각·초 올림) 모두 before 전인 행 중 마지막
        (I1 — 저장 완료 증거가 없거나 개장 이후면 그 행으로 자격을 회복시키지 않음)."""
        q = ("SELECT * FROM candidate_gate WHERE signal_date=? AND contract_hash=? AND symbol=? AND stage=?"
             + (" AND evaluated_at<? AND committed_at IS NOT NULL AND committed_at<?" if before else "")
             + " ORDER BY evaluated_at DESC, gate_id DESC LIMIT 1")
        r = self.conn.execute(q, (signal_date, contract_hash, symbol, stage,
                                  *((before, before) if before else ()))).fetchone()
        return None if r is None else self._gate(r)

    def pending_gate_sets(self, now: datetime) -> list[tuple[str, str, str]]:
        """개장 전인 (신호일, 계약, 개장 시각) — 설정이 바뀌면 개장 전에 다시 판정할 대상."""
        return [tuple(r) for r in self.conn.execute(
            "SELECT DISTINCT signal_date, contract_hash, open_at FROM candidate_gate WHERE stage='SCAN' AND open_at>?",
            (_ts(now),))]


@dataclass
class DaemonPaths:
    config: Path
    watch_db: Path
    research_db: Path
    daemon_db: Path
    watch_scan_db: Path
    watch_open_db: Path
    report_dir: Path
    log_file: Path | None = None


@dataclass
class DaemonSettings:
    poll_sec: float = 60.0
    daily_call_cap: int = 3000          # 요청일(관리자 시계 날짜) 기준 하루 실제 요청 상한 — 토큰 발급·연속조회·재시도 포함
    open_check_reserve: int = 100       # 마감 준비는 cap − reserve에서 멈춤 → 개장 확인 몫(R3). 개장 확인도 cap은 넘지 않음
    max_task_sec: float = 1800.0
    open_offset_min: int = 5
    on_time_tolerance_sec: int = 120
    after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE
    lock_timeout: float = 30.0
    heartbeat_sec: float = 15.0         # 작업 중 요청 경계에서 heartbeat·진척 갱신 간격(관리자 시계)
    max_busy_ticks: int = 50            # 쉬지 않고 연달아 실행한 순회 상한 — 넘으면 poll_sec 한 번 쉼(안전장치)


def scope_hash(cfg) -> tuple[str, list[str], list[str]]:
    """(대상 코드 해시, 감시 종목 코드, S1 대상 코드)."""
    active = sorted(s.code for s in cfg.active_symbols)
    s1 = sorted(s.code for s in cfg.active_symbols if s.interest_active and s.interest.s1_analysis)
    h = hashlib.sha256(json.dumps({"active": active, "s1": s1}).encode()).hexdigest()[:10]
    return h, active, s1


def _selection(codes: list[str]) -> dict:
    return {"kind": "watchlist", "rule": SELECTION_RULE, "codes": codes}


def prep_scope(cfg, calendar: TradingCalendar, after_close: timedelta) -> tuple[str, dict]:
    """CLOSE_PREP 작업 범위 (W2 검토 R4) — 결과를 바꾸는 것만: 대상 코드 + S1 계산 계약(전략·설정 해시·달력·완성 지연·
    선정 코드 포함) + 분석 준비 기준(analysis_basis) + 준비 이력 길이. 관심 가격대·수동 보유 수량/가격·알림 설정은 넣지 않음
    (바뀌어도 같은 날 다시 준비하지 않음). 반환 (scope 문자열 'v2:<해시>', 근거)."""
    _, active, s1 = scope_hash(cfg)
    contract = build_contract(calendar, after_close=after_close, selection=_selection(s1))[1]
    basis = {"active": active, "s1": s1, "s1_contract": contract, "analysis_basis": analysis_basis(cfg),
             "history_sessions": cfg.monitor.history_sessions, "after_close_sec": int(after_close.total_seconds())}
    h = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:10]
    return f"{SCOPE_VERSION}:{h}", basis


GATE_RULE = ("운영 진입 게이트 = 설정 정상(거부·미해결 저널 없음) ∧ 관심 켜짐 ∧ s1_analysis ∧ 위험 자격 OK(최신 목록) ∧ "
             "가격 데이터 READY ∧ S1 분석 READY ∧ analysis_basis 일치. SCAN 단계(관찰 직후·개장 전 설정 변경 때 다시) "
             "근거가 개장 전에 통과 ∧ 개장 확인 때 현재 상태로 다시 통과해야 개장 후보")


class WatchDaemon:
    """한 순회(tick)씩 실행 — 실제 루프는 run_forever. 시계·잠자기·조회 클라이언트는 주입(시험은 가짜)."""

    def __init__(self, paths: DaemonPaths, calendar: TradingCalendar, client, *, settings: DaemonSettings | None = None,
                 now: Callable[[], datetime], sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print, hook: Callable[[str], None] = test_point) -> None:
        self.p, self.cal, self.client = paths, calendar, client
        self.s = settings or DaemonSettings()
        self.now, self.sleep, self.hook = now, sleep, hook
        self._log_fn = log
        self.dstore = DaemonStore(paths.daemon_db)
        self.run_id: str | None = None
        self._task_started: float | None = None
        self._current: str | None = None
        self._current_kind: str | None = None
        self._task_used = 0
        self._last_hb: datetime | None = None
        self._last_notice: list[str] = []
        self._last_resume: dict | None = None
        if isinstance(client, ReadOnlyResearchClient) and (
                client.guard is None or isinstance(getattr(client.guard, "__self__", None), WatchDaemon)):
            client.guard = self.request_guard               # 모든 실제 요청 직전 검사(R3) — CLI는 make_client(guard=)로

    def log(self, msg: str) -> None:
        line = redact(f"{_ts(self.now())} {msg}")
        self._log_fn(line)
        if self.p.log_file is not None:
            try:
                self.p.log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.p.log_file, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    # ── 시작·종료 ────────────────────────────────────────────
    def start(self) -> str:
        now = self.now()
        if self.dstore.meta("since") is None:
            self.dstore.set_meta("since", now.date().isoformat())
        aborted = self.dstore.abort_running(now, "이전 관리자 프로세스가 작업 중 종료됨(강제 종료·정전 등)")
        self.run_id = self.dstore.begin_run(now)
        for k in aborted:
            self.log(f"[관리자] 중단된 작업 {k} → ABORTED, 다시 시도 대상")
        up = self.dstore.meta("upgraded_from")
        if up:
            self.log(f"[관리자] 관리자 DB {up} → {DAEMON_SCHEMA} (기존 작업 보존 — 이전 형식 CLOSE_PREP 키는 새 범위 키로 "
                     "한 번 다시 실행될 수 있음, 게이트 근거 없는 이전 후보는 유효로 보지 않음)")
        self.log(f"[관리자] 시작 {self.run_id} (since {self.dstore.meta('since')}) — 설정 poll {self.s.poll_sec:.0f}초·"
                 f"하루 상한 {self.s.daily_call_cap}·개장 확인 예약 {self.s.open_check_reserve}·작업 시간 상한 "
                 f"{self.s.max_task_sec:.0f}초·완성 지연 {int(self.s.after_close.total_seconds() // 60)}분·개장 +"
                 f"{self.s.open_offset_min}분·달력 {calendar_version(self.cal)}·스키마 {DAEMON_SCHEMA}")
        self.hook("daemon_started")
        return self.run_id

    def stop(self, state: str = "STOPPED", error: str = "") -> None:
        if getattr(self.client, "__dict__", {}).get("guard") == self.request_guard:
            self.client.guard = None
        if self.run_id:
            self.dstore.end_run(self.run_id, self.now(), state, error)
        self.dstore.close()

    # ── 계획 ─────────────────────────────────────────────────
    def _session(self, d: date):
        try:
            return self.cal.session_times(d)
        except CalendarCoverageError:
            return None

    def due_times(self, d: date) -> dict[str, tuple[datetime, datetime | None]]:
        """거래일 d의 (실행 가능 시각, 기한). 기한 None = 없음."""
        st = self._session(d)
        if st is None:
            return {}
        close = datetime.combine(d, st.close)
        prep_due = close + self.s.after_close
        try:
            nxt = self.cal.next_trading_day(d)
            nst = self._session(nxt)
            prep_deadline = None if nst is None else datetime.combine(nxt, nst.close) + self.s.after_close
        except CalendarCoverageError:
            prep_deadline = None
        return {CLOSE_PREP: (prep_due, prep_deadline),
                OPEN_CHECK: (datetime.combine(d, st.open) + timedelta(minutes=self.s.open_offset_min), None)}

    def prep_scope(self, cfg) -> tuple[str, dict]:
        return prep_scope(cfg, self.cal, self.s.after_close)

    def open_key(self, d: date) -> str:
        return f"{OPEN_CHECK}|{d.isoformat()}|OPEN+{self.s.open_offset_min}m"

    @staticmethod
    def _runnable(t: dict, now: datetime) -> bool:
        """지금 시작할 수 있는 작업인지 — 끝남·실행 중·다시 시도 시각 전(양보·미룸 포함)이면 아님 (R2)."""
        if t["status"] in DONE or t["status"] == RUNNING:
            return False
        if t["status"] in RETRYABLE + (YIELDED,):
            return t["next_retry_at"] is not None and now >= datetime.fromisoformat(t["next_retry_at"])
        return True

    def _calendar_status(self, problem: str | None, now: datetime) -> None:
        cur = self.dstore.meta("calendar_status")
        old = json.loads(cur) if cur else {}
        status = CAL_OK if problem is None else CAL_UNAVAILABLE
        if old.get("status") == status and old.get("message", "") == (problem or ""):
            return
        self.dstore.set_meta("calendar_status", json.dumps({"status": status, "message": problem or "",
                                                            "since": _ts(now)}, ensure_ascii=False))
        if problem:
            self.log(f"[관리자] {CAL_UNAVAILABLE}: {problem} — config/trading_calendar 갱신 전까지 그 범위 작업을 만들지 "
                     "않음(멈춘 것이 아님; status에 표시)")
        elif old:
            self.log("[관리자] 거래일 달력 정상 — 작업 계획 재개")

    def calendar_status(self) -> dict:
        cur = self.dstore.meta("calendar_status")
        return json.loads(cur) if cur else {"status": "UNKNOWN", "message": ""}

    def plan(self, now: datetime, cfg) -> list[dict]:
        """지금 실행할 수 있는 작업(우선순위 순). 작업 행을 만들고, 기한이 지난 CLOSE_PREP는 MISSED로."""
        since = date.fromisoformat(self.dstore.meta("since") or now.date().isoformat())
        problem = None
        try:
            days = list(self.cal.trading_days_in_range(since, now.date()))
        except CalendarCoverageError as exc:
            days, problem = [], f"거래일 달력이 {since}~{now.date()}를 포함하지 않음({exc})"
        if problem is None:
            try:
                self.cal.next_trading_day(now.date())
            except CalendarCoverageError as exc:
                problem = f"거래일 달력에 {now.date()} 다음 거래일이 없음({exc}) — 마감 준비 기한·다음 예정 계산 불가"
        self._calendar_status(problem, now)
        scope = self.prep_scope(cfg)[0] if cfg is not None else "NO_CONFIG"
        out = []
        for d in days:
            dt = self.due_times(d)
            for kind in (OPEN_CHECK, CLOSE_PREP):
                due, deadline = dt[kind]
                if now < due:
                    continue
                key = f"{kind}|{d.isoformat()}|scope:{scope}" if kind == CLOSE_PREP else self.open_key(d)
                t = self.dstore.ensure_task(key, kind, d, scope if kind == CLOSE_PREP else f"OPEN+{self.s.open_offset_min}m",
                                            due, now)
                if t["status"] in DONE:
                    continue
                if kind == CLOSE_PREP and deadline is not None and now >= deadline:
                    if t["status"] != MISSED:
                        self.dstore.mark(key, MISSED, now, f"기한 {deadline} 지남 — 다음 거래일 완성 뒤라 그날 신호를 "
                                                           "만들지 않음(일봉은 다음 준비가 이어 받음)")
                    continue
                if self._runnable(t, now):
                    out.append(t)
        prio = {OPEN_CHECK: 0, CLOSE_PREP: 1}
        return sorted(out, key=lambda t: (prio[t["kind"]], t["trading_day"]))

    def next_due(self, now: datetime) -> datetime | None:
        """다음에 깨어날 시각(오늘·다음 거래일의 실행 가능 시각, 다시 시도 시각 중 가장 이른 미래)."""
        cands = []
        try:
            d = now.date() if self._session(now.date()) else self.cal.next_trading_day(now.date())
            for day in (d, self.cal.next_trading_day(d)):
                for due, _ in self.due_times(day).values():
                    if due > now:
                        cands.append(due)
        except CalendarCoverageError:
            pass
        for r in self.dstore.conn.execute("SELECT next_retry_at FROM task WHERE next_retry_at IS NOT NULL"
                                          f" AND status NOT IN ({','.join('?' * len(DONE))})", DONE):
            t = datetime.fromisoformat(r[0])
            if t > now:
                cands.append(t)
        return min(cands) if cands else None

    # ── 호출 예산·양보 (R2·R3) ───────────────────────────────
    def budget_limit(self, kind: str | None) -> int:
        return self.s.daily_call_cap - (self.s.open_check_reserve if kind == CLOSE_PREP else 0)

    def budget_reason(self, kind: str | None, now: datetime) -> str | None:
        used, lim = self.dstore.calls(now.date()), self.budget_limit(kind)
        if used >= lim:
            return f"CALL_BUDGET({now.date()} 요청 {used}회 ≥ {kind or '-'} 한도 {lim})"
        return None

    def retry_at(self, reason: str, now: datetime) -> datetime:
        """양보 사유별 다시 실행 가능한 시각 (R2)."""
        if reason.startswith("CALL_BUDGET"):
            return datetime.combine(now.date() + timedelta(days=1), dtime(0))   # 다음 예산 창(요청일 자정)
        if reason.startswith("TIME_BUDGET"):
            return now + timedelta(seconds=self.s.poll_sec)                     # 받은 만큼 저장됨 — 한 번 쉬고 이어서
        return now          # PRIORITY: 계획이 OPEN_CHECK를 먼저 고름 / STOP_REQUESTED: 프로세스가 끝나고 다음 기동 때

    def _open_check_waiting(self, now: datetime) -> bool:
        """지금 실행할 수 있는 OPEN_CHECK가 기다리는지(실패 대기·예산 소진·이미 끝남이면 아님 — 무한 양보 방지)."""
        d = now.date()
        dt = self.due_times(d)
        if OPEN_CHECK not in dt or now < dt[OPEN_CHECK][0]:
            return False
        if d < date.fromisoformat(self.dstore.meta("since") or d.isoformat()):
            return False
        if self.budget_reason(OPEN_CHECK, now):
            return False
        t = self.dstore.task(self.open_key(d))
        return t is None or self._runnable(t, now)

    def yield_reason(self, now: datetime | None = None) -> str | None:
        now = now or self.now()
        if self.run_id and self.dstore.stop_requested(self.run_id):
            return "STOP_REQUESTED"
        b = self.budget_reason(self._current_kind, now)
        if b:
            return b
        if self._task_started is not None and time.monotonic() - self._task_started > self.s.max_task_sec:
            return f"TIME_BUDGET({self.s.max_task_sec:.0f}초)"
        if self._current_kind == CLOSE_PREP and self._open_check_waiting(now):
            return "PRIORITY:OPEN_CHECK"
        return None

    def should_stop(self) -> str | None:
        """대상 사이 검사(prepare_data) — 요청 경계 검사(request_guard)와 같은 규칙."""
        return self.yield_reason()

    def request_guard(self, api_id: str) -> None:
        """실제 요청 직전마다(토큰·연속조회 페이지·429/401 재시도 포함) — 막히면 RequestStopped(그 요청은 보내지 않음).
        통과하면 보내기 전에 요청일 사용량을 1 올려 저장(강제 종료돼도 빠지지 않음 — 실패한 요청도 셈, 보수적)."""
        now = self.now()
        why = self.yield_reason(now)
        if why:
            raise RequestStopped(why)
        self.dstore.add_calls(now.date(), 1)
        self._task_used += 1
        if self.run_id and (self._last_hb is None or (now - self._last_hb).total_seconds() >= self.s.heartbeat_sec):
            self._last_hb = now
            self.dstore.heartbeat(self.run_id, now, self._current,
                                  progress=f"{self._current or '-'} 요청 {self._task_used}회 · 마지막 {api_id}")

    # ── 운영 진입 게이트 (R1) ─────────────────────────────────
    def _gate_eval(self, state, wstore, codes: list[str], snap_id) -> dict[str, tuple[bool, list[str], dict]]:
        ready_all, risk_all = wstore.readiness(), wstore.risk()
        basis = None if state.config is None else analysis_basis(state.config)
        out = {}
        for code in codes:
            sym = None if state.config is None else state.config.symbol(code)
            r, rk = ready_all.get(f"STOCK:{code}"), risk_all.get(code)
            if state.config is None:
                why = ["NO_VALID_CONFIG"]
            elif sym is None:
                why = ["NOT_WATCHED"]                       # 설정에서 빠짐
            else:
                _, why = entry_gate(state, sym, r, rk, snapshot_id=snap_id)
                if sym.interest_active and not sym.interest.s1_analysis:
                    why = why + ["S1_OFF"]
                if not sym.active:
                    why = why + ["INACTIVE"]                # 관심·보유 모두 없음 — 감시 안 함
            ev = {"block_reason": state.block_reason, "analysis_basis": basis,
                  "readiness": None if r is None else {
                      **{k: r.get(k) for k in ("status", "reason", "analysis_status", "analysis_reason",
                                               "config_version", "checked_at")},
                      "analysis_basis": (r.get("detail") or {}).get("analysis_basis")},
                  "risk": None if rk is None else {k: rk.get(k) for k in ("status", "flags", "list_snapshot_id",
                                                                          "config_version", "checked_at")}}
            out[code] = (not why, why, ev)
        return out

    def _gate_row(self, stage: str, sig: str, open_at, contract: str, code: str, run_id, state, snap_id,
                  ok: bool, why: list[str], ev: dict, task_key: str) -> dict:
        latest = state.latest
        return {"stage": stage, "signal_date": sig, "open_at": open_at, "contract_hash": contract, "symbol": code,
                "scan_run_id": run_id, "eligible": ok, "reasons": why, "config_version": state.active_version,
                "latest_version": None if latest is None else latest["version"],
                "latest_status": None if latest is None else latest["status"], "list_snapshot_id": snap_id,
                "evidence": ev, "evaluated_at": _ts(self.now()), "task_key": task_key}

    def _store_gates(self, rows: list[dict]) -> list[int]:
        """저장(커밋) → 커밋 뒤 시각을 재서 초 올림으로 committed_at 기록 (S1 committed_at과 같은 방식, I1)."""
        ids = self.dstore.add_gates(rows)
        self.hook("gate_committed")
        t = self.now()
        if t.microsecond:
            t = t.replace(microsecond=0) + timedelta(seconds=1)
        self.dstore.mark_gates_committed(ids, _ts(t))
        return ids

    def _resume_admit(self, cset: dict, pending: list[dict], a5_run_id: str, fresh: bool, wstore, state, snap_id,
                      task_key: str) -> dict[str, list[str]]:
        """매 개장 확인 실행에서 가격 조회 전 (W2 재검토 R6·R7): 확정 목록 중 아직 가격이 없는 후보마다
        ① 원래 후보(신호일·확정 계약·종목·대상 개장)의 개장 전 SCAN 근거 — 판정·저장 완료 모두 개장 전(I1, latest_gate) — 가
        통과했고 ② 지금 운영 자격도 통과해야 조회. 근거가 없으면(이전 형식 wd2 행을 개장 뒤 이전해 committed_at NULL 등)
        NO_GATE_EVIDENCE — 저장 완료 시각을 추정하거나 source.gate가 있다고 통과시키지 않음. 판정은 RESUME 근거로 저장하고,
        한 번 제외한 후보는 그날 다시 넣지 않음(개장 뒤 회복 없음). 이번 실행에서 막 확정한 목록이면 확정 직전 OPEN 판정
        (같은 두 검사)과 같은 상태라 다시 기록하지 않음."""
        if fresh:
            return {}
        sig, contract, open_at = cset["signal_date"], cset["contract_hash"], cset["open_at"]
        cur = self._gate_eval(state, wstore, [c["symbol"] for c in pending], snap_id)
        earlier = {g["symbol"] for g in self.dstore.gates(sig, stage="RESUME")
                   if g["contract_hash"] == contract and g["open_at"] == open_at and not g["eligible"]}
        rows, skip = [], {}
        for c in pending:
            ok, why, ev = cur[c["symbol"]]
            g = self.dstore.latest_gate(sig, contract, c["symbol"], before=open_at)
            proof = (["NO_GATE_EVIDENCE"] if g is None else [] if g["eligible"] else [f"SCAN:{r}" for r in g["reasons"]])
            reasons = (proof + [f"CURRENT:{r}" for r in why]
                       + (["EXCLUDED_EARLIER"] if c["symbol"] in earlier else []))
            rows.append(self._gate_row("RESUME", sig, open_at, contract, c["symbol"], c["run_id"], state, snap_id,
                                       not reasons, reasons, {**ev, "a5_run_id": a5_run_id, "set_id": cset["set_id"],
                                                              "scan_gate_id": g and g["gate_id"],
                                                              "scan_committed_at": g and g["committed_at"]},
                                       task_key))
            if reasons:
                skip[c["symbol"]] = reasons
        self._store_gates(rows)
        self._last_resume = {"evaluated_at": _ts(self.now()), "a5_run_id": a5_run_id,
                             "config_version": state.active_version, "checked": [c["symbol"] for c in pending],
                             "excluded": skip}
        return skip

    def _record_scan_gates(self, run: dict, contract: str, codes: list[str], wstore, state, snap_id,
                           task_key: str) -> dict:
        ctx = run["context"]
        ev = self._gate_eval(state, wstore, codes, snap_id)
        self._store_gates([self._gate_row("SCAN", ctx["signal_date"], ctx.get("next_open"), contract, c,
                                          run["run_id"], state, snap_id, *ev[c], task_key) for c in codes])
        return {"eligible": [c for c in codes if ev[c][0]], "blocked": {c: ev[c][1] for c in codes if not ev[c][0]},
                "config_version": state.active_version}

    def regate(self, wstore, state, snap_id) -> int:
        """개장 전 관찰의 게이트를 지금 상태로 다시 판정 — 결과·설정 버전이 바뀐 종목만 새 근거 행 (설정을 고친 뒤 재평가,
        개장 전 설정 오류·관심 해제 반영). 개장 뒤에는 하지 않음(소급 없음). 반환: 새 행 수."""
        now = self.now()
        n = 0
        for sig, contract, _open_at in self.dstore.pending_gate_sets(now):
            latest: dict[str, dict] = {}
            for g in self.dstore.gates(sig, stage="SCAN"):
                if g["contract_hash"] == contract:
                    latest[g["symbol"]] = g
            ev = self._gate_eval(state, wstore, sorted(latest), snap_id)
            rows = []
            for code, g in latest.items():
                ok, why, e = ev[code]
                if ((ok, why, state.active_version) != (bool(g["eligible"]), g["reasons"], g["config_version"])
                        or g.get("committed_at") is None):          # 저장 완료 증거 없는 행(이전 형식·기록 전 중단)도 다시

                    rows.append(self._gate_row("SCAN", sig, g["open_at"], contract, code, g["scan_run_id"], state,
                                               snap_id, ok, why, {**e, "regate_of": g["gate_id"]}, "REGATE"))
            if rows:
                self._store_gates(rows)
                n += len(rows)
                self.log(f"[관리자] 개장 전 게이트 재판정 {sig}/{contract}: " + ", ".join(
                    f"{r['symbol']}={'통과' if r['eligible'] else ','.join(r['reasons'])}" for r in rows))
        return n

    def _open_gate(self, draft: dict, cands: list[dict], wstore, state, snap_id, task_key: str
                   ) -> tuple[list[dict], dict]:
        """개장 확인 후보 확정 직전: 개장 전 SCAN 근거(마지막) 통과 ∧ 지금 상태 통과만 남김. 나머지는 사유와 함께 기록."""
        sig, contract, open_at = draft["signal_date"], draft["contract_hash"], draft["open_at"]
        cur = self._gate_eval(state, wstore, [c["symbol"] for c in cands], snap_id)
        kept, excl, rows = [], [], []
        for c in cands:
            g = self.dstore.latest_gate(sig, contract, c["symbol"], before=open_at)
            if g is None:
                why = ["NO_GATE_EVIDENCE"]                  # 이전 형식·근거 없음 — 유효로 추정하지 않음
            elif not g["eligible"]:
                why = [f"SCAN:{r}" for r in g["reasons"]]
            else:
                ok, w, _ = cur[c["symbol"]]
                why = [] if ok else [f"OPEN:{r}" for r in w]
            rows.append(self._gate_row("OPEN", sig, open_at, contract, c["symbol"], c["run_id"], state, snap_id,
                                       not why, why, {**cur[c["symbol"]][2], "scan_gate_id": g and g["gate_id"],
                                                      "scan_evaluated_at": g and g["evaluated_at"]}, task_key))
            if why:
                excl.append({"symbol": c["symbol"], "signal_id": c["signal_id"], "run_id": c["run_id"], "reasons": why})
            else:
                kept.append(c)
        if rows:
            self._store_gates(rows)
        return kept, {"rule": GATE_RULE, "evaluated_at": _ts(self.now()), "config_version": state.active_version,
                      "calc_pass": len(cands), "kept": [c["symbol"] for c in kept], "excluded": excl}

    # ── 한 순회 ──────────────────────────────────────────────
    def tick(self) -> dict:
        """할 일 하나를 실행(없으면 아무것도 안 함). 반환 {ran, status, next_due, stop, deferred}."""
        now = self.now()
        self.dstore.heartbeat(self.run_id, now, None)
        if self.dstore.stop_requested(self.run_id):
            return {"ran": None, "stop": True}
        with ResearchStore(self.p.research_db) as rstore, WatchStore(self.p.watch_db) as wstore:
            listing, snap_id = listing_from_store(rstore)
            try:
                state, att, rec = sync_file(wstore, self.p.config, listing, snap_id, now=self.now,
                                            origin="DAEMON", lock_timeout=self.s.lock_timeout)
            except Exception as exc:                          # noqa: BLE001 — 설정 반영 실패도 기록하고 다음 순회
                self.log(f"[관리자] 설정 반영 실패 {type(exc).__name__}: {exc} — 다음 순회에 다시")
                self.dstore.heartbeat(self.run_id, self.now(), None, f"설정 반영 실패 {type(exc).__name__}: {exc}")
                return {"ran": None, "error": str(exc), "next_due": self.now() + timedelta(seconds=self.s.poll_sec)}
            notice = []
            if rec:
                notice.append(f"[관리자] 적용 저널 복구 {rec['action']}" + ("" if rec["resolved"] else
                              " — 미해결: 일반 적용 중단·신규 진입 차단 (restore 또는 resolve-journal --keep-file)"))
            if att["errors"]:
                notice.append(f"[관리자] 설정 v{att['version']} 거부 — "
                              + ("마지막 정상 설정으로 계속, 신규 진입 관찰 차단" if state.can_monitor
                                 else "정상 설정 없음 — 대기"))
            if notice and notice != self._last_notice:      # 같은 상태를 매 순회 다시 쓰지 않음
                for m in notice:
                    self.log(m)
            self._last_notice = notice
            self.regate(wstore, state, snap_id)
            todo = self.plan(self.now(), state.config)
            deferred = []
            for t in todo:
                if not state.can_monitor:
                    self.dstore.finish_task(t["task_key"], FAILED, self.now(), detail={},
                                            error="NO_VALID_CONFIG — 정상 설정이 없어 시작 안 함(설정을 고친 뒤 다시)")
                    return {"ran": t["task_key"], "status": FAILED, "next_due": self.next_due(self.now())}
                why = self.budget_reason(t["kind"], self.now())
                if why:                                       # 시작하지 않고 다음 예산 창까지 미룸(시도 수 그대로)
                    at = self.retry_at(why, self.now())
                    if self.dstore.defer(t["task_key"], self.now(), at, why):
                        self.log(f"[관리자] {t['task_key']} 시작 안 함 — {why}; {at}부터 다시")
                    deferred.append(t["task_key"])
                    continue
                status = self._run(t, rstore, wstore, state, listing, snap_id)
                return {"ran": t["task_key"], "status": status, "deferred": deferred,
                        "next_due": self.next_due(self.now())}
        return {"ran": None, "deferred": deferred, "next_due": self.next_due(self.now())}

    def _run(self, t: dict, rstore, wstore, state, listing, snap_id) -> str:
        key = t["task_key"]
        self.dstore.start_task(key, self.run_id, self.now())
        self.dstore.heartbeat(self.run_id, self.now(), key)
        self._current, self._current_kind, self._task_started = key, t["kind"], time.monotonic()
        self._task_used, self._last_hb = 0, self.now()
        self.hook("task_started")
        self.log(f"[관리자] {key} 시작(시도 {t['attempts'] + 1})")
        detail: dict = {"config_version": state.active_version}
        status, err, contract = FAILED, "", None
        try:
            if t["kind"] == CLOSE_PREP:
                status, contract = self._close_prep(t, detail, rstore, wstore, state, listing, snap_id)
            else:
                status, contract = self._open_check(t, detail, rstore, wstore, state, snap_id)
        except RequestStopped as exc:                         # 요청 경계에서 양보 — 실패 아님
            status, detail["yield"] = YIELDED, exc.reason
        except (ResearchApiError, ScanError, WatchNotReady, A5.OpenCheckError, sqlite3.Error, OSError) as exc:
            status, err = FAILED, f"{type(exc).__name__}: {exc}"
        except ResearchConfigError as exc:
            status, err = FAILED, f"설정 오류(API 키·도메인) {exc}"
        except Exception as exc:                              # noqa: BLE001 — 작업 하나의 실패로 관리자를 멈추지 않음
            status, err = FAILED, f"{type(exc).__name__}: {exc}"
        except BaseException:
            detail["calls"] = self._task_used
            self.dstore.finish_task(key, ABORTED, self.now(), detail=detail, error="중단(Ctrl+C·종료 요청)",
                                    config_version=detail.get("config_version"))
            self._clear_current()
            raise
        detail["calls"] = self._task_used
        retry = None
        if status == SUPERSEDED:
            err = detail.get("superseded", "")
        if status == YIELDED:
            err = f"YIELD {detail.get('yield')}"
            retry = self.retry_at(detail.get("yield") or "", self.now())
        row = self.dstore.finish_task(key, status, self.now(), detail=detail, error=err,
                                      config_version=detail.get("config_version"), contract_hash=contract,
                                      retry_at=retry)
        self._clear_current()
        self.log(f"[관리자] {key} → {status}" + (f" ({err})" if err else "") +
                 (f" 다음 시도 {row['next_retry_at']}" if row["next_retry_at"] else ""))
        self._write_reports(row)
        return status

    def _clear_current(self) -> None:
        self._current = self._current_kind = self._task_started = None

    # ── 작업 ─────────────────────────────────────────────────
    def _close_prep(self, t, detail, rstore, wstore, state, listing, snap_id) -> tuple[str, str | None]:
        d = date.fromisoformat(t["trading_day"])
        close = datetime.combine(d, self.cal.session_times(d).close)
        detail["entry_blocked"] = state.entry_blocked
        stop = self.should_stop()
        if stop:
            raise RequestStopped(stop)
        collector = ResearchCollector(self.client, rstore, self.cal, after_close=self.s.after_close, log=self.log)
        snap = rstore.latest_snapshot()
        if snap is None or snap["observed_at"] < _ts(close):
            s = collector.snapshot_universe()                   # 목록 조회(2회)만 — 전체 시장 일봉 수집 없음
            detail["list_snapshot"] = s["snapshot_id"]
            listing, snap_id = listing_from_store(rstore)
            state, att, _ = sync_file(wstore, self.p.config, listing, snap_id, now=self.now, origin="DAEMON",
                                      lock_timeout=self.s.lock_timeout)
            detail["config_version"], detail["entry_blocked"] = state.active_version, state.entry_blocked
        else:
            detail["list_snapshot"] = snap["snapshot_id"]
        if state.config is None:
            raise WatchNotReady(state.block_reason)
        scope_now = self.prep_scope(state.config)[0]
        if scope_now != t["scope"]:                             # 목록 갱신 뒤 다시 읽은 설정의 범위가 키와 다름
            detail["superseded"] = f"범위 {t['scope']} → {scope_now} — 새 키 작업으로 대체(이 키로는 준비·관찰 안 함)"
            return SUPERSEDED, None
        self.hook("after_list")
        prep = prepare_data(wstore, rstore, collector, self.cal, state, listing, now=self.now,
                            after_close=self.s.after_close, log=self.log, should_stop=self.should_stop)
        detail["prepare"] = {"run_id": prep["run_id"], "status": prep["status"], "counts": prep["counts"],
                             "yield": prep["yield_reason"]}
        if prep["status"] == "YIELDED":
            raise RequestStopped(prep["yield_reason"])
        self.hook("after_prepare")
        _, _active, s1_codes = scope_hash(state.config)
        contract = None
        if not s1_codes:
            detail["s1"] = {"status": "NO_TARGETS", "note": "S1 분석 대상(관심 켜짐·s1_analysis) 종목 없음"}
        else:
            with ScanStore(self.p.watch_scan_db) as sstore:
                scanner = self._scanner(rstore, sstore, s1_codes, state.active_version)
                res = scanner.run(self.now(), now=self.now)
                contract = scanner.contract_hash
                run = res if res["status"] == "COMPLETE" else sstore.load_run(res["run_id"])
                detail["s1"] = {"status": res["status"], "run_id": res["run_id"], "contract_hash": contract,
                                "signal_date": run["context"]["signal_date"] if run else None,
                                "codes": s1_codes, "selection_rule": SELECTION_RULE,
                                "counts": {k: run["counts"].get(k) for k in ("universe", "signals", "by_signal",
                                                                             "data_hold_total")} if run else None,
                                "actionable": bool(run["context"].get("next_open")
                                                   and run["context"]["scan_at"] < run["context"]["next_open"])
                                if run else None}
                if run is not None and res["status"] == "COMPLETE":
                    # 계산 PASS(관찰 DB)와 운영 진입 자격(관리자 DB candidate_gate)을 따로 저장 (R1)
                    detail["s1"]["gate"] = self._record_scan_gates(run, contract, s1_codes, wstore, state, snap_id,
                                                                   t["task_key"])
                if run is not None and detail["s1"]["signal_date"] != d.isoformat():
                    detail["s1"]["note"] = f"신호일 {detail['s1']['signal_date']} ≠ 작업일 {d} (완성 기준 확인 필요)"
                try:
                    path = write_scan_report(run, self.p.report_dir / "s1")
                    sstore.set_report_path(run["run_id"], path)
                    detail["s1"]["report"] = path
                except OSError as exc:
                    detail["s1"]["report_error"] = f"{type(exc).__name__}: {exc}"
        status = PARTIAL if prep["status"] == "PARTIAL" else COMPLETE
        return status, contract

    def _scanner(self, rstore, sstore, codes: list[str], version: int | None) -> S1Scanner:
        return S1Scanner(rstore, sstore, self.cal, after_close=self.s.after_close, log=self.log,
                         selection=_selection(codes),
                         context_extra={"watch_config_version": version, "selection_rule": SELECTION_RULE})

    def _open_check(self, t, detail, rstore, wstore, state, snap_id) -> tuple[str, str | None]:
        d = date.fromisoformat(t["trading_day"])
        sig = self.cal.previous_trading_day(d)
        open_at = _ts(datetime.combine(d, self.cal.session_times(d).open))
        detail["signal_date"] = sig.isoformat()
        with ScanStore(self.p.watch_scan_db) as sstore, A5.OpenCheckStore(self.p.watch_open_db) as ostore:
            old = ostore.get_set(d)
            if old is not None and "gate" not in old["source"]:
                # 이 판(W2 검토 R1) 전에 게이트 없이 확정된 후보 — 유효로 추정하지 않음, 가격 조회 안 함
                detail.update({"contract_hash": old["contract_hash"], "candidate_status": old["status"],
                               "candidate_reason": old["reason"], "gate": "LEGACY_NO_EVIDENCE",
                               "note": "게이트 근거 없이 확정된 이전 형식 후보 — 운영 표본으로 쓰지 않음(조회 0회)"})
                return COMPLETE, old["contract_hash"]
            runs = [r for r in sstore.runs(sig.isoformat()) if r["status"] == "COMPLETE" and r.get("committed_at")
                    and r["committed_at"] < open_at and r["scan_at"] < open_at]
            if runs:
                contract = sorted(runs, key=lambda r: r["committed_at"])[-1]["contract_hash"]
            else:                                           # 개장 전 저장된 관찰이 없음 → 현재 범위 계약으로 NO_SCAN 확정
                codes = scope_hash(state.config)[2]
                contract = build_contract(self.cal, after_close=self.s.after_close, selection=_selection(codes))[1]
            checker = A5.OpenChecker(ostore, sstore, rstore, self.cal, contract_hash=contract,
                                     offset_min=self.s.open_offset_min,
                                     on_time_tolerance_sec=self.s.on_time_tolerance_sec, log=self.log,
                                     gate=lambda draft, cands: self._open_gate(draft, cands, wstore, state, snap_id,
                                                                               t["task_key"]),
                                     admit=lambda cset, pending, rid, fresh: self._resume_admit(
                                         cset, pending, rid, fresh, wstore, state, snap_id, t["task_key"]))
            self._last_resume = None
            res = checker.run(d, client=self.client, now=self.now)
            g = res["set"]["source"].get("gate") or {}
            detail.update({"contract_hash": res["set"]["contract_hash"],
                           "candidate_status": res["set"]["status"], "candidate_reason": res["set"]["reason"],
                           "gate": {"calc_pass": g.get("calc_pass"), "kept": g.get("kept"),
                                    "excluded": g.get("excluded")},
                           "current_gate": self._last_resume or {"basis": "후보 확정 때 판정(이번 실행)",
                                                                 "a5_run_id": res["run_id"]},
                           "counts": res["counts"], "note": res["note"], "a5_run_id": res["run_id"]})
            try:
                detail["report"] = A5.write_report(res, self.p.report_dir / "open")
            except OSError as exc:
                detail["report_error"] = f"{type(exc).__name__}: {exc}"
        return COMPLETE, res["set"]["contract_hash"]

    # ── 보고서 ───────────────────────────────────────────────
    def _write_reports(self, row: dict) -> None:
        try:
            path = write_daily_report(self.dstore, self.p, row["trading_day"])
            self.dstore.set_report(row["task_key"], path, None)
        except OSError as exc:
            self.dstore.set_report(row["task_key"], None, f"{type(exc).__name__}: {exc}")
            self.log(f"[관리자] 일일 보고서 실패 {exc} — 작업 결과는 저장됨, `report --day {row['trading_day']}`로 다시")

    def retry_reports(self) -> int:
        n = 0
        for r in self.dstore.conn.execute("SELECT task_key, trading_day FROM task WHERE report_error IS NOT NULL"):
            self._write_reports({"task_key": r[0], "trading_day": r[1]})
            n += 1
        return n

    # ── 루프 ─────────────────────────────────────────────────
    def run_forever(self, *, max_ticks: int | None = None) -> str:
        """중지 요청·Ctrl+C까지. 반환: 종료 상태."""
        n = busy = 0
        try:
            while max_ticks is None or n < max_ticks:
                n += 1
                r = self.tick()
                if r.get("stop"):
                    self.log("[관리자] 중지 요청 — 종료")
                    return "STOPPED"
                if r.get("ran"):
                    busy += 1
                    if busy < self.s.max_busy_ticks:
                        continue                                # 할 일이 더 있을 수 있음 — 바로 다음 순회
                    self.log(f"[관리자] 쉬지 않고 {busy}회 실행 — {self.s.poll_sec:.0f}초 쉼(안전장치)")
                busy = 0
                self.retry_reports()
                nd = r.get("next_due")
                wait = self.s.poll_sec if nd is None else max(1.0, min(self.s.poll_sec,
                                                                       (nd - self.now()).total_seconds()))
                self.sleep(wait)
            return "MAX_TICKS"
        except KeyboardInterrupt:
            self.log("[관리자] Ctrl+C — 종료")
            return "INTERRUPTED"

    def run_until_idle(self, *, max_runs: int = 200) -> str:
        """지금 실행할 작업이 없을 때까지만(미래 시각의 예산 회복·다시 시도는 기다리지 않음). max_runs = 안전 상한."""
        for _ in range(max_runs):
            r = self.tick()
            if r.get("stop"):
                return "STOPPED"
            if not r.get("ran"):
                self.retry_reports()
                return "IDLE"
        self.log(f"[관리자] until-idle 안전 상한 {max_runs}회 — 종료")
        return "IDLE_CAP"


def _current_gate_text(cg) -> str:
    if not cg:
        return "기록 없음"
    if "excluded" not in cg:
        return f"{cg.get('basis')} (실행 {cg.get('a5_run_id')})"
    ex = cg["excluded"]
    return (f"{cg['evaluated_at']} 실행 {cg['a5_run_id']} 설정 v{cg['config_version']} 미확인 {cg['checked']} → "
            + ("모두 통과" if not ex else "제외(가격 미조회) " + json.dumps(ex, ensure_ascii=False)))


def write_daily_report(dstore: DaemonStore, p: DaemonPaths, day: str) -> str:
    """그날 작업·설정 버전·준비 상태·S1 관찰·개장 확인 요약(관찰 기록 — 체결 아님)."""
    tasks = dstore.tasks(day)
    lines = [f"# 지정 종목 조회 전용 운영 — {day}", "",
             "> 주문 없음. S1은 관찰 신호, 개장 확인은 관찰 가격이며 실제 체결이 아닙니다. 수동 보유는 증권사 잔고가 아닙니다.", ""]
    lines += ["| 작업 | 상태 | 시도 | 설정 버전 | 계약 | 시작 | 끝 | 오류 |", "|---|---|---|---|---|---|---|---|"]
    for t in tasks:
        lines.append(f"| {t['kind']} | {t['status']} | {t['attempts']} | {t['config_version'] or '-'} | "
                     f"{t['contract_hash'] or '-'} | {t['started_at'] or '-'} | {t['finished_at'] or '-'} | "
                     f"{(t['error'] or '-')[:80]} |")
    for t in tasks:
        d = t["detail"]
        if t["kind"] == CLOSE_PREP and d and not d.get("superseded"):
            pr = d.get("prepare") or {}
            s1 = d.get("s1") or {}
            lines += ["", f"## 마감 뒤 준비 ({t['scope']})",
                      f"- 목록 스냅숏 {d.get('list_snapshot')} · 준비 {pr.get('run_id')} {pr.get('status')} "
                      f"{json.dumps(pr.get('counts'), ensure_ascii=False)}",
                      f"- S1 관찰(계산): {s1.get('status')} 신호일 {s1.get('signal_date')} 대상 {s1.get('codes')} "
                      f"결과 {json.dumps(s1.get('counts'), ensure_ascii=False)} actionable={s1.get('actionable')}",
                      f"- 운영 진입 게이트(설정 v{d.get('config_version')}): "
                      + (f"통과 {(s1.get('gate') or {}).get('eligible')} · 제외 "
                         f"{json.dumps((s1.get('gate') or {}).get('blocked'), ensure_ascii=False)}"
                         if s1.get("gate") else "기록 없음") + " — 계산 PASS라도 게이트 제외면 개장 후보 아님"]
        if t["kind"] == OPEN_CHECK and d:
            lines += ["", "## 개장 확인",
                      f"- 신호일 {d.get('signal_date')} 후보 {d.get('candidate_status')} — {d.get('candidate_reason')}",
                      "- 진입 게이트(후보 확정 때): " + (d["gate"] if isinstance(d.get("gate"), str) else
                                       f"계산 PASS {(d.get('gate') or {}).get('calc_pass')} · 운영 후보 "
                                       f"{(d.get('gate') or {}).get('kept')} · 제외 "
                                       f"{json.dumps((d.get('gate') or {}).get('excluded'), ensure_ascii=False)}"),
                      "- 마지막 실행 시점 자격: " + _current_gate_text(d.get("current_gate")),
                      f"- 시각·조회·판정 {json.dumps(d.get('counts'), ensure_ascii=False)} {d.get('note') or ''}"]
    out = Path(p.report_dir) / "daily"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"watch_{day}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@contextmanager
def daemon_lock(daemon_db: Path):
    """관리자 중복 기동 방지 — 기다리지 않음(이미 있으면 ConfigLockTimeout)."""
    with file_lock(Path(str(daemon_db) + ".lock"), timeout=0, what="상시 실행 관리자"):
        yield
