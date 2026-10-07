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

작업 키·상태
- task_key = 종류|거래일|대상 범위. CLOSE_PREP 범위 = 감시 종목·S1 대상 코드의 해시(설정에서 종목을 더하거나 빼면 새 키 —
  같은 날 새 범위로 한 번 더 실행, 이미 받은 일봉은 다시 받지 않음). OPEN_CHECK 범위 = 확인 종류(OPEN+5m). 실행한 계산
  계약·설정 버전은 행에 기록.
- 상태: PENDING → RUNNING → COMPLETE / PARTIAL(일부 조회 실패) / YIELDED(더 급한 작업·중지·예산·시간 상한에 양보) /
  FAILED(예외) / ABORTED(프로세스 중단 — 다음 기동 때 표시) / MISSED.
- 다시 시도: PARTIAL·FAILED·ABORTED는 실패 횟수에 따라 5·15·30·60분 뒤, 실패 4번까지. YIELDED는 실패로 세지 않고 곧바로
  다음 차례. COMPLETE는 다시 실행하지 않음(보고서만 `report`로 다시 만듦).

우선순위·양보: OPEN_CHECK > CLOSE_PREP(오래된 날 먼저). 긴 준비는 대상마다 should_stop을 확인 — 실행 가능한 OPEN_CHECK·
중지 요청·하루 호출 예산·작업 시간 상한이면 양보. 한 프로세스·한 스레드·한 조회 클라이언트(호출 간격·재시도·토큰을
모든 작업이 공유) — SQLite 연결을 스레드 사이에 나누지 않음.

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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from infra.research import open_check as A5
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE, ResearchCollector
from infra.research.kiwoom_readonly import ResearchApiError, ResearchConfigError
from infra.research.s1_scanner import S1Scanner, ScanError, build_contract
from infra.research.scan_report import write_report as write_scan_report
from infra.research.scan_store import ScanStore
from infra.research.store import ResearchStore
from infra.watch.apply import file_lock, sync_file, test_point
from infra.watch.manager import WatchNotReady, listing_from_store, prepare_data
from infra.watch.store import WatchStore
from utils.trading_calendar import CalendarCoverageError, TradingCalendar

DAEMON_SCHEMA = "wd1"
CLOSE_PREP, OPEN_CHECK = "CLOSE_PREP", "OPEN_CHECK"
PENDING, RUNNING, COMPLETE, PARTIAL, YIELDED, FAILED, ABORTED, MISSED = (
    "PENDING", "RUNNING", "COMPLETE", "PARTIAL", "YIELDED", "FAILED", "ABORTED", "MISSED")
RETRYABLE = (PARTIAL, FAILED, ABORTED)
DONE = (COMPLETE, MISSED)
BACKOFF_SEC = (300, 900, 1800, 3600)
MAX_FAILURES = 4
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
"""


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
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='daemon_schema'").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO meta VALUES('daemon_schema', ?)", (DAEMON_SCHEMA,))
        elif cur[0] != DAEMON_SCHEMA:
            raise RuntimeError(f"관리자 저장소 스키마 {cur[0]} ≠ {DAEMON_SCHEMA}: {self.path}")

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
                    config_version: int | None = None, contract_hash: str | None = None) -> dict:
        with self.tx():
            t = self.task(key)
            failures = t["failures"] + (1 if status in RETRYABLE else 0)
            nxt = None
            if status in RETRYABLE and failures < MAX_FAILURES:
                nxt = _ts(now + timedelta(seconds=BACKOFF_SEC[min(failures - 1, len(BACKOFF_SEC) - 1)]))
            elif status == YIELDED:
                nxt = _ts(now)
            self.conn.execute(
                "UPDATE task SET status=?, failures=?, finished_at=?, next_retry_at=?, detail_json=?, error=?,"
                " config_version=COALESCE(?, config_version), contract_hash=COALESCE(?, contract_hash)"
                " WHERE task_key=?", (status, failures, _ts(now), nxt, json.dumps(detail, ensure_ascii=False,
                                                                                  default=str),
                                      redact(error)[:500], config_version, contract_hash, key))
            self.event(key, now, status, {"error": redact(error)[:300], "next_retry_at": nxt})
        return self.task(key)

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

    def heartbeat(self, run_id: str, now: datetime, current: str | None, last_error: str | None = None) -> None:
        self.conn.execute("UPDATE daemon_run SET heartbeat_at=?, current_task=?, last_error=COALESCE(?, last_error)"
                          " WHERE run_id=?", (_ts(now), current, None if last_error is None else redact(last_error),
                                              run_id))

    def end_run(self, run_id: str, now: datetime, state: str, last_error: str = "") -> None:
        self.conn.execute("UPDATE daemon_run SET state=?, stopped_at=?, current_task=NULL,"
                          " last_error=CASE WHEN ?='' THEN last_error ELSE ? END WHERE run_id=?",
                          (state, _ts(now), last_error, redact(last_error), run_id))

    def last_run(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM daemon_run ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return None if r is None else dict(r)

    def request_stop(self) -> bool:
        cur = self.conn.execute("UPDATE daemon_run SET stop_requested=1 WHERE state='RUNNING'")
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
    daily_call_cap: int = 3000
    max_task_sec: float = 1800.0
    open_offset_min: int = 5
    on_time_tolerance_sec: int = 120
    after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE
    lock_timeout: float = 30.0


def scope_hash(cfg) -> tuple[str, list[str], list[str]]:
    """(범위 해시, 감시 종목 코드, S1 대상 코드)."""
    active = sorted(s.code for s in cfg.active_symbols)
    s1 = sorted(s.code for s in cfg.active_symbols if s.interest_active and s.interest.s1_analysis)
    h = hashlib.sha256(json.dumps({"active": active, "s1": s1}).encode()).hexdigest()[:10]
    return h, active, s1


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
        self.log(f"[관리자] 시작 {self.run_id} (since {self.dstore.meta('since')})")
        self.hook("daemon_started")
        return self.run_id

    def stop(self, state: str = "STOPPED", error: str = "") -> None:
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

    def plan(self, now: datetime, cfg) -> list[dict]:
        """지금 실행할 수 있는 작업(우선순위 순). 작업 행을 만들고, 기한이 지난 CLOSE_PREP는 MISSED로."""
        since = date.fromisoformat(self.dstore.meta("since") or now.date().isoformat())
        try:
            days = [d for d in self.cal.trading_days_in_range(since, now.date())]
        except CalendarCoverageError:
            days = []
        scope = scope_hash(cfg)[0] if cfg is not None else "NO_CONFIG"
        out = []
        for d in days:
            dt = self.due_times(d)
            for kind in (OPEN_CHECK, CLOSE_PREP):
                due, deadline = dt[kind]
                if now < due:
                    continue
                key = (f"{kind}|{d.isoformat()}|scope:{scope}" if kind == CLOSE_PREP
                       else f"{kind}|{d.isoformat()}|OPEN+{self.s.open_offset_min}m")
                t = self.dstore.ensure_task(key, kind, d, scope if kind == CLOSE_PREP else f"OPEN+{self.s.open_offset_min}m",
                                            due, now)
                if t["status"] in DONE:
                    continue
                if kind == CLOSE_PREP and deadline is not None and now >= deadline:
                    if t["status"] != MISSED:
                        self.dstore.mark(key, MISSED, now, f"기한 {deadline} 지남 — 다음 거래일 완성 뒤라 그날 신호를 "
                                                           "만들지 않음(일봉은 다음 준비가 이어 받음)")
                    continue
                if t["status"] == RUNNING:
                    continue
                if t["status"] in RETRYABLE + (YIELDED,):
                    if t["next_retry_at"] is None or now < datetime.fromisoformat(t["next_retry_at"]):
                        continue
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
                                          " AND status NOT IN (?, ?)", DONE):
            t = datetime.fromisoformat(r[0])
            if t > now:
                cands.append(t)
        return min(cands) if cands else None

    # ── 양보 판단 ────────────────────────────────────────────
    def should_stop(self) -> str | None:
        if self.run_id and self.dstore.stop_requested(self.run_id):
            return "STOP_REQUESTED"
        if self.dstore.calls(self.now().date()) + self._task_calls() >= self.s.daily_call_cap:
            return f"CALL_BUDGET({self.s.daily_call_cap}/일)"
        if self._task_started is not None and time.monotonic() - self._task_started > self.s.max_task_sec:
            return f"TIME_BUDGET({self.s.max_task_sec:.0f}초)"
        if self._current and self._current.startswith(CLOSE_PREP):
            now = self.now()
            for d in (now.date(),):
                dt = self.due_times(d)
                if OPEN_CHECK in dt and now >= dt[OPEN_CHECK][0]:
                    key = f"{OPEN_CHECK}|{d.isoformat()}|OPEN+{self.s.open_offset_min}m"
                    t = self.dstore.task(key)
                    if t is None or t["status"] not in DONE:
                        return "PRIORITY:OPEN_CHECK"
        return None

    def _task_calls(self) -> int:
        return (getattr(self.client, "calls", 0) or 0) - self._calls0 if self._task_started is not None else 0

    # ── 한 순회 ──────────────────────────────────────────────
    def tick(self) -> dict:
        """할 일 하나를 실행(없으면 아무것도 안 함). 반환 {ran, status, next_due, stop}."""
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
            if rec:
                self.log(f"[관리자] 적용 저널 복구 {rec['action']}")
            if att["errors"]:
                self.log(f"[관리자] 설정 v{att['version']} 거부 — "
                         + ("마지막 정상 설정으로 계속, 신규 진입 관찰 차단" if state.can_monitor else "정상 설정 없음 — 대기"))
            todo = self.plan(self.now(), state.config)
            if not todo:
                return {"ran": None, "next_due": self.next_due(self.now())}
            t = todo[0]
            if not state.can_monitor:
                self.dstore.finish_task(t["task_key"], FAILED, self.now(), detail={},
                                        error="NO_VALID_CONFIG — 정상 설정이 없어 시작 안 함(설정을 고친 뒤 다시)")
                return {"ran": t["task_key"], "status": FAILED, "next_due": self.next_due(self.now())}
            status = self._run(t, rstore, wstore, state, listing, snap_id)
        return {"ran": t["task_key"], "status": status, "next_due": self.next_due(self.now())}

    def _run(self, t: dict, rstore, wstore, state, listing, snap_id) -> str:
        key = t["task_key"]
        self.dstore.start_task(key, self.run_id, self.now())
        self.dstore.heartbeat(self.run_id, self.now(), key)
        self._current, self._task_started, self._calls0 = key, time.monotonic(), getattr(self.client, "calls", 0) or 0
        self.hook("task_started")
        self.log(f"[관리자] {key} 시작(시도 {t['attempts'] + 1})")
        detail, status, err, contract = {}, FAILED, "", None
        try:
            if t["kind"] == CLOSE_PREP:
                status, detail, contract = self._close_prep(t, rstore, wstore, state, listing, snap_id)
            else:
                status, detail, contract = self._open_check(t, rstore, state)
        except (ResearchApiError, ScanError, WatchNotReady, A5.OpenCheckError, sqlite3.Error, OSError) as exc:
            status, err = FAILED, f"{type(exc).__name__}: {exc}"
        except ResearchConfigError as exc:
            status, err = FAILED, f"설정 오류(API 키·도메인) {exc}"
        except Exception as exc:                              # noqa: BLE001 — 작업 하나의 실패로 관리자를 멈추지 않음
            status, err = FAILED, f"{type(exc).__name__}: {exc}"
        except BaseException:
            self._account_calls()
            self.dstore.finish_task(key, ABORTED, self.now(), detail=detail, error="중단(Ctrl+C·종료 요청)")
            raise
        finally:
            used = self._account_calls()
        detail["calls"] = used
        row = self.dstore.finish_task(key, status, self.now(), detail=detail, error=err,
                                      config_version=state.active_version, contract_hash=contract)
        self._current = self._task_started = None
        self.log(f"[관리자] {key} → {status}" + (f" ({err})" if err else "") +
                 (f" 다음 시도 {row['next_retry_at']}" if row["next_retry_at"] and status != YIELDED else ""))
        self._write_reports(row)
        return status

    def _account_calls(self) -> int:
        used = (getattr(self.client, "calls", 0) or 0) - getattr(self, "_calls0", 0)
        if used > 0:
            self.dstore.add_calls(self.now().date(), used)
        self._calls0 = getattr(self.client, "calls", 0) or 0
        return used

    # ── 작업 ─────────────────────────────────────────────────
    def _close_prep(self, t, rstore, wstore, state, listing, snap_id) -> tuple[str, dict, str | None]:
        d = date.fromisoformat(t["trading_day"])
        close = datetime.combine(d, self.cal.session_times(d).close)
        detail: dict = {"config_version": state.active_version, "entry_blocked": state.entry_blocked}
        stop = self.should_stop()
        if stop:
            return YIELDED, {**detail, "yield": stop}, None
        collector = ResearchCollector(self.client, rstore, self.cal, after_close=self.s.after_close, log=self.log)
        snap = rstore.latest_snapshot()
        if snap is None or snap["observed_at"] < _ts(close):
            s = collector.snapshot_universe()                   # 목록 조회(2회)만 — 전체 시장 일봉 수집 없음
            detail["list_snapshot"] = s["snapshot_id"]
            listing, snap_id = listing_from_store(rstore)
            state, att, _ = sync_file(wstore, self.p.config, listing, snap_id, now=self.now, origin="DAEMON",
                                      lock_timeout=self.s.lock_timeout)
        else:
            detail["list_snapshot"] = snap["snapshot_id"]
        self.hook("after_list")
        prep = prepare_data(wstore, rstore, collector, self.cal, state, listing, now=self.now,
                            after_close=self.s.after_close, log=self.log, should_stop=self.should_stop)
        detail["prepare"] = {"run_id": prep["run_id"], "status": prep["status"], "counts": prep["counts"],
                             "yield": prep["yield_reason"]}
        if prep["status"] == "YIELDED":
            return YIELDED, {**detail, "yield": prep["yield_reason"]}, None
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
                if run is not None and detail["s1"]["signal_date"] != d.isoformat():
                    detail["s1"]["note"] = f"신호일 {detail['s1']['signal_date']} ≠ 작업일 {d} (완성 기준 확인 필요)"
                try:
                    path = write_scan_report(run, self.p.report_dir / "s1")
                    sstore.set_report_path(run["run_id"], path)
                    detail["s1"]["report"] = path
                except OSError as exc:
                    detail["s1"]["report_error"] = f"{type(exc).__name__}: {exc}"
        status = PARTIAL if prep["status"] == "PARTIAL" else COMPLETE
        return status, detail, contract

    def _scanner(self, rstore, sstore, codes: list[str], version: int | None) -> S1Scanner:
        return S1Scanner(rstore, sstore, self.cal, after_close=self.s.after_close, log=self.log,
                         selection={"kind": "watchlist", "rule": SELECTION_RULE, "codes": codes},
                         context_extra={"watch_config_version": version, "selection_rule": SELECTION_RULE})

    def _open_check(self, t, rstore, state) -> tuple[str, dict, str | None]:
        d = date.fromisoformat(t["trading_day"])
        sig = self.cal.previous_trading_day(d)
        open_at = _ts(datetime.combine(d, self.cal.session_times(d).open))
        with ScanStore(self.p.watch_scan_db) as sstore, A5.OpenCheckStore(self.p.watch_open_db) as ostore:
            runs = [r for r in sstore.runs(sig.isoformat()) if r["status"] == "COMPLETE" and r.get("committed_at")
                    and r["committed_at"] < open_at and r["scan_at"] < open_at]
            if runs:
                contract = sorted(runs, key=lambda r: r["committed_at"])[-1]["contract_hash"]
            else:                                           # 개장 전 저장된 관찰이 없음 → 현재 범위 계약으로 NO_SCAN 확정
                codes = scope_hash(state.config)[2]
                contract = build_contract(self.cal, after_close=self.s.after_close,
                                          selection={"kind": "watchlist", "rule": SELECTION_RULE,
                                                     "codes": codes})[1]
            checker = A5.OpenChecker(ostore, sstore, rstore, self.cal, contract_hash=contract,
                                     offset_min=self.s.open_offset_min,
                                     on_time_tolerance_sec=self.s.on_time_tolerance_sec, log=self.log)
            res = checker.run(d, client=self.client, now=self.now)
            detail = {"signal_date": sig.isoformat(), "contract_hash": res["set"]["contract_hash"],
                      "candidate_status": res["set"]["status"], "candidate_reason": res["set"]["reason"],
                      "counts": res["counts"], "note": res["note"], "a5_run_id": res["run_id"],
                      "config_version": state.active_version}
            try:
                detail["report"] = A5.write_report(res, self.p.report_dir / "open")
            except OSError as exc:
                detail["report_error"] = f"{type(exc).__name__}: {exc}"
        return COMPLETE, detail, res["set"]["contract_hash"]

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
        n = 0
        try:
            while max_ticks is None or n < max_ticks:
                n += 1
                r = self.tick()
                if r.get("stop"):
                    self.log("[관리자] 중지 요청 — 종료")
                    return "STOPPED"
                if r.get("ran"):
                    continue                                    # 할 일이 더 있을 수 있음 — 바로 다음 순회
                self.retry_reports()
                nd = r.get("next_due")
                wait = self.s.poll_sec if nd is None else max(1.0, min(self.s.poll_sec,
                                                                       (nd - self.now()).total_seconds()))
                self.sleep(wait)
            return "MAX_TICKS"
        except KeyboardInterrupt:
            self.log("[관리자] Ctrl+C — 종료")
            return "INTERRUPTED"


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
        if t["kind"] == CLOSE_PREP and d:
            pr = d.get("prepare") or {}
            s1 = d.get("s1") or {}
            lines += ["", f"## 마감 뒤 준비 ({t['scope']})",
                      f"- 목록 스냅숏 {d.get('list_snapshot')} · 준비 {pr.get('run_id')} {pr.get('status')} "
                      f"{json.dumps(pr.get('counts'), ensure_ascii=False)}",
                      f"- S1 관찰: {s1.get('status')} 신호일 {s1.get('signal_date')} 대상 {s1.get('codes')} "
                      f"결과 {json.dumps(s1.get('counts'), ensure_ascii=False)} actionable={s1.get('actionable')}"]
        if t["kind"] == OPEN_CHECK and d:
            lines += ["", "## 개장 확인",
                      f"- 신호일 {d.get('signal_date')} 후보 {d.get('candidate_status')} — {d.get('candidate_reason')}",
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
