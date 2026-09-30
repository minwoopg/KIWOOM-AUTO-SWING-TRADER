from __future__ import annotations

"""연구 데이터 저장소 (A2, SQLite 한 파일 — 기본 data/research/research.sqlite3, git 제외). 스키마 r2.

담는 것
- universe_snapshot / universe_row : 날짜별 종목 목록. `snapshot_date`와 **`observed_at`(실제 조회 시각)**,
  장 단계, 분류 정책 버전을 함께 저장. 원래 응답 전체는 gzip으로 raw_gz에 보존.
- series / bar : 종목·지수 일봉. 원천 정수(부호만 뗀 값)·품질(NO_TRADES / INVALID:…)을 그대로 두고
  배율(지수 ÷100)·단위(백만원 → 원)는 읽을 때 적용. run_type: BACKFILL(과거 일괄) / FORWARD(매일 수집).
- series_revision : 값의 판(revision)마다 조정 기준(upd_stkpc_tp·base_dt)·활성 시각·대체 시각.
- bar_history : 대체된 revision의 봉 전부(값·시각 그대로). bar_invalid_raw : INVALID 행 원문.
  series_event : INIT·EXTEND·REBASE·APPEND·VERIFY_FAILED 등 변경 기록.
- job / job_item : 재개 가능한 백필. 작업마다 base_dt 고정, 종목 단위로 한 트랜잭션에 저장.

시각 세 가지 (A2-R1, r2) — 수정 기준일·수집 종류·값 확보 시각은 서로 다른 정보입니다.
- received_at   : **이 값**(이 revision의 그 날짜 봉)을 실제로 받은 응답의 수신 시각. 페이지마다 다름.
                  BACKFILL도 실제 수신 시각을 기록합니다.
- available_at  : 이 값을 계산에 쓸 수 있게 된 시각 = max(received_at, 그 revision의 활성 시각).
                  재수집(REBASE)한 새 값은 새 revision이 활성화된 시각부터만 씁니다.
- first_ready_at: 그 날짜의 완성 봉을 처음 확보한 시각(모든 revision 통틀어). **기록용** — 새 값의
                  사용 가능 시각으로 쓰지 않습니다.
시점 조회: `research_series(sid, as_of=X)`는 X에 활성이던 revision의 봉 중 available_at ≤ X인 것만 돌려줍니다.
→ 나중에 정정된 값이 과거 평가에 섞이지 않고, 과거 평가를 당시 값으로 그대로 재현합니다.
`as_of=None`은 현재 revision 전체 — 과거 날짜를 현재 조정 기준으로 보는 **가정 분석**(주봉 ASSUMED_DELAY)용.

수정주가 기준 (A2 보완 4, A2-R2)
- 한 종목 시계열은 **한 번의 연속 조회(같은 base_dt, 모든 페이지)** 로만 채웁니다.
- 기존 봉이 있는 시계열의 교체(`replace_series`)는 **후보를 검증한 뒤에만** 활성화합니다:
  비어 있지 않음 · 저장된 마지막 날짜까지 포함 · 필요한 시작일(또는 저장된 첫 날짜)까지 포함 ·
  변경을 발견한 첫 페이지 값과 일치. 하나라도 실패하면 값·조정 메타는 그대로 두고
  integrity=REBASE_REQUIRED와 VERIFY_FAILED 기록을 남깁니다(정상 갱신으로 집계하지 않음).
  REBASE_REQUIRED인 시계열에는 새 날짜를 붙이지 않습니다(append 거부).
- 검증을 통과했고 겹치는 구간이 모두 같으면 없는 날짜만 추가(EXTEND, revision 유지),
  하나라도 다르거나 사라졌으면 기존 봉 전부를 bar_history로 옮기고 새 revision으로 통째 교체(REBASE).
  → 한 시계열(한 revision) 안에 서로 다른 조정 기준이 섞이지 않음.
- 수정주가는 과거 실제 체결가격이 아닙니다(분할·증자 등으로 과거 가격이 다시 계산된 값).

r1 → r2 이전: 기존 DB를 열면 자동으로 바꿉니다(한 트랜잭션). r1의 봉별 fetched_at(첫 페이지 수신 시각)을
received_at으로, max(fetched_at, series.updated_at)을 available_at으로(보수적 — 실제보다 늦을 수는 있어도
이르지 않음), ready_at 또는 fetched_at을 first_ready_at으로 옮기고, 현재 revision 행을 만듭니다.
"""

import gzip
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from domain.research.series import ResearchBar
from infra.research.kiwoom_rows import NO_TRADES, RawBar, SourceSpec, to_research_bar

SCHEMA_VERSION = "r2"
BACKFILL, FORWARD = "BACKFILL", "FORWARD"
COVERAGE_OK, LISTED_AFTER_START, HISTORY_END, PAGE_CAP = "OK", "LISTED_AFTER_START", "HISTORY_END", "PAGE_CAP"
PENDING, DONE, SHORTFALL, ERROR = "PENDING", "DONE", "SHORTFALL", "ERROR"
INTEGRITY_OK, REBASE_REQUIRED = "OK", "REBASE_REQUIRED"


class IntegrityError(RuntimeError):
    """재검증이 필요한 시계열에 새 날짜를 붙이려 함."""


_BAR_BODY = """
  open_raw INTEGER, high_raw INTEGER, low_raw INTEGER, close_raw INTEGER, volume INTEGER, trade_value_raw INTEGER,
  quality TEXT NOT NULL, run_type TEXT NOT NULL, received_at TEXT NOT NULL, available_at TEXT NOT NULL,
  first_ready_at TEXT NOT NULL"""

_SCHEMA_TABLES = f"""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS universe_snapshot(
  snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL, observed_at TEXT NOT NULL, observed_end_at TEXT NOT NULL,
  market_phase TEXT NOT NULL, policy_version TEXT NOT NULL, source TEXT NOT NULL,
  row_count INTEGER NOT NULL, summary_json TEXT NOT NULL, raw_gz BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS universe_row(
  snapshot_id INTEGER NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, market_code TEXT NOT NULL,
  market TEXT, index_id TEXT, security_type TEXT NOT NULL, type_basis TEXT NOT NULL,
  audit_info TEXT, state TEXT, order_warning TEXT, company_class TEXT, reg_day TEXT,
  list_count INTEGER, last_price INTEGER, risk_flags TEXT NOT NULL, collect INTEGER NOT NULL,
  eligible_now INTEGER NOT NULL, exclusions TEXT NOT NULL,
  PRIMARY KEY(snapshot_id, code)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS series(
  series_id TEXT PRIMARY KEY, kind TEXT NOT NULL, code TEXT NOT NULL, api_id TEXT NOT NULL,
  price_scale INTEGER NOT NULL, trade_value_unit INTEGER NOT NULL, volume_unit TEXT NOT NULL,
  revision INTEGER NOT NULL, adj_upd_stkpc_tp TEXT, adj_base_dt TEXT NOT NULL, fetched_at TEXT NOT NULL,
  job_id TEXT, verified_base_dt TEXT NOT NULL, verified_at TEXT NOT NULL,
  first_date TEXT, last_date TEXT, required_from TEXT, coverage TEXT NOT NULL, coverage_detail TEXT NOT NULL,
  no_trades_count INTEGER NOT NULL, invalid_count INTEGER NOT NULL, updated_at TEXT NOT NULL,
  integrity TEXT NOT NULL DEFAULT 'OK', integrity_detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS bar_invalid_raw(
  series_id TEXT NOT NULL, revision INTEGER NOT NULL, date TEXT NOT NULL, raw_json TEXT NOT NULL,
  PRIMARY KEY(series_id, revision, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS series_event(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, series_id TEXT NOT NULL, at TEXT NOT NULL,
  event TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job(
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, base_dt TEXT NOT NULL,
  upd_stkpc_tp TEXT NOT NULL, required_from TEXT NOT NULL, max_pages INTEGER NOT NULL,
  snapshot_id INTEGER, status TEXT NOT NULL, params_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS job_item(
  job_id TEXT NOT NULL, series_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, code TEXT NOT NULL,
  reg_day TEXT, status TEXT NOT NULL, pages INTEGER NOT NULL, first_date TEXT, last_date TEXT,
  reason TEXT NOT NULL, attempts INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(job_id, series_id)) WITHOUT ROWID;
"""
_SCHEMA_R2 = f"""
CREATE TABLE IF NOT EXISTS series_revision(
  series_id TEXT NOT NULL, revision INTEGER NOT NULL, upd_stkpc_tp TEXT, base_dt TEXT NOT NULL, job_id TEXT,
  reason TEXT NOT NULL, activated_at TEXT NOT NULL, superseded_at TEXT,
  PRIMARY KEY(series_id, revision)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bar(
  series_id TEXT NOT NULL, date TEXT NOT NULL,{_BAR_BODY}, revision INTEGER NOT NULL,
  PRIMARY KEY(series_id, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bar_history(
  series_id TEXT NOT NULL, revision INTEGER NOT NULL, date TEXT NOT NULL,{_BAR_BODY},
  superseded_at TEXT NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(series_id, revision, date)) WITHOUT ROWID;
"""
_COLS = ("open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw, quality, run_type, received_at, "
         "available_at, first_ready_at")


def _ts(dt: datetime | None) -> str | None:
    """조회 기준 시각 — 초 미만은 버림(보수적)."""
    return None if dt is None else dt.replace(microsecond=0).isoformat(timespec="seconds")


def _ts_up(dt: datetime | None) -> str | None:
    """확보·사용 가능 시각 — 초 미만은 올림(보수적: 실제보다 이르게 기록하지 않음)."""
    if dt is None:
        return None
    if dt.microsecond:
        dt = dt.replace(microsecond=0) + timedelta(seconds=1)
    return dt.isoformat(timespec="seconds")


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _d(s: str | None) -> date | None:
    return None if s is None else date.fromisoformat(s)


@dataclass(frozen=True)
class AdjustmentBasis:
    upd_stkpc_tp: str | None        # 종목 "1"(수정주가). 지수는 None
    base_dt: str                    # 조회 기준일 YYYYMMDD


@dataclass(frozen=True)
class FetchedBar:
    raw: RawBar
    received_at: datetime           # 이 행이 들어 있던 페이지의 수신 시각

    @property
    def date(self) -> date:
        return self.raw.date


@dataclass(frozen=True)
class StoredBar:
    raw: RawBar
    run_type: str
    received_at: datetime
    available_at: datetime
    first_ready_at: datetime
    revision: int


@dataclass(frozen=True)
class SeriesMeta:
    series_id: str
    kind: str
    code: str
    api_id: str
    price_scale: int
    trade_value_unit: int
    volume_unit: str
    revision: int
    adj_upd_stkpc_tp: str | None
    adj_base_dt: str
    fetched_at: datetime
    job_id: str | None
    verified_base_dt: str
    verified_at: datetime
    first_date: date | None
    last_date: date | None
    required_from: date | None
    coverage: str
    coverage_detail: str
    no_trades_count: int
    invalid_count: int
    integrity: str = INTEGRITY_OK
    integrity_detail: str = ""

    @property
    def spec(self) -> SourceSpec:
        from infra.research.kiwoom_rows import INDEX_DAILY, STOCK_DAILY
        base = STOCK_DAILY if self.kind == "STOCK" else INDEX_DAILY
        return SourceSpec(self.kind, self.api_id, base.list_key, self.price_scale, self.trade_value_unit,
                          self.volume_unit)


@dataclass(frozen=True)
class ResearchSeries:
    """연구 계산 입력. available_at은 주봉 OBSERVED 모드의 data_ready_at으로 그대로 넘깁니다."""
    bars: list[ResearchBar]
    available_at: dict[date, datetime]
    first_ready_at: dict[date, datetime]
    run_type: dict[date, str]
    revision: int | None
    basis: AdjustmentBasis | None
    integrity: str                  # 현재 조회: series.integrity / 시점 조회: "AS_OF"(당시 활성 revision)
    as_of: datetime | None
    meta: SeriesMeta = field(repr=False, default=None)


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_TABLES)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if cur is None:
            self.conn.executescript(_SCHEMA_R2)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        elif cur[0] == "r1":
            self._migrate_r1_to_r2()
        elif cur[0] != SCHEMA_VERSION:
            raise RuntimeError(f"연구 저장소 스키마 {cur[0]} ≠ {SCHEMA_VERSION}: {self.path}")
        else:
            self.conn.executescript(_SCHEMA_R2)

    def _migrate_r1_to_r2(self) -> None:
        c = self.conn
        with self.tx():
            cols = {r[1] for r in c.execute("PRAGMA table_info(series)")}
            if "integrity" not in cols:
                c.execute("ALTER TABLE series ADD COLUMN integrity TEXT NOT NULL DEFAULT 'OK'")
                c.execute("ALTER TABLE series ADD COLUMN integrity_detail TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE bar RENAME TO bar_r1")
            c.execute("ALTER TABLE bar_history RENAME TO bar_history_r1")
            for stmt in _SCHEMA_R2.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            c.execute(
                f"INSERT INTO bar(series_id, date, {_COLS}, revision) SELECT b.series_id, b.date, b.open_raw,"
                " b.high_raw, b.low_raw, b.close_raw, b.volume, b.trade_value_raw, b.quality, b.run_type,"
                " b.fetched_at, MAX(b.fetched_at, s.updated_at), COALESCE(b.ready_at, b.fetched_at), b.revision"
                " FROM bar_r1 b JOIN series s ON s.series_id = b.series_id")
            c.execute(
                f"INSERT INTO bar_history(series_id, revision, date, {_COLS}, superseded_at, reason)"
                " SELECT series_id, revision, date, open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw,"
                " quality, run_type, fetched_at, fetched_at, COALESCE(ready_at, fetched_at), superseded_at, reason"
                " FROM bar_history_r1")
            c.execute("INSERT OR IGNORE INTO series_revision SELECT series_id, revision, NULL, '', NULL,"
                      " 'MIGRATED_R1_HISTORY', MIN(fetched_at), MAX(superseded_at) FROM bar_history_r1"
                      " GROUP BY series_id, revision")
            c.execute("INSERT OR IGNORE INTO series_revision SELECT series_id, revision, adj_upd_stkpc_tp,"
                      " adj_base_dt, job_id, 'MIGRATED_R1', updated_at, NULL FROM series")
            c.execute("DROP TABLE bar_r1")
            c.execute("DROP TABLE bar_history_r1")
            c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_from_r1', ?)",
                      (datetime.now().isoformat(timespec="seconds"),))

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

    # ── 종목 목록 ─────────────────────────────────────────────
    def save_snapshot(self, *, snapshot_date: date, observed_at: datetime, observed_end_at: datetime,
                      market_phase: str, policy_version: str, source: str, records, raw_pages: list,
                      summary: dict) -> int:
        raw_gz = gzip.compress(json.dumps(raw_pages, ensure_ascii=False).encode("utf-8"))
        with self.tx():
            cur = self.conn.execute(
                "INSERT INTO universe_snapshot(snapshot_date, observed_at, observed_end_at, market_phase,"
                " policy_version, source, row_count, summary_json, raw_gz) VALUES(?,?,?,?,?,?,?,?,?)",
                (snapshot_date.isoformat(), _ts(observed_at), _ts_up(observed_end_at), market_phase,
                 policy_version, source, len(records), json.dumps(summary, ensure_ascii=False), raw_gz))
            sid = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO universe_row VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(sid, r.code, r.name, r.market_code, r.market, r.index_id, r.security_type, r.type_basis,
                  r.audit_info, r.state, r.order_warning, r.company_class,
                  r.reg_day.isoformat() if r.reg_day else None, r.list_count, r.last_price,
                  json.dumps(list(r.risk_flags), ensure_ascii=False), int(r.collect), int(r.eligible_now),
                  json.dumps(list(r.exclusions), ensure_ascii=False))
                 for r in records if r.market is not None])      # 주식 행만 열로 (전체 원문은 raw_gz)
        return sid

    def latest_snapshot(self) -> dict | None:
        row = self.conn.execute("SELECT snapshot_id, snapshot_date, observed_at, observed_end_at, market_phase,"
                                " policy_version, source, row_count, summary_json FROM universe_snapshot"
                                " ORDER BY snapshot_id DESC LIMIT 1").fetchone()
        return None if row is None else {**dict(row), "summary": json.loads(row["summary_json"])}

    def snapshot_rows(self, snapshot_id: int, *, collect_only: bool = False) -> list[dict]:
        q = "SELECT * FROM universe_row WHERE snapshot_id=?" + (" AND collect=1" if collect_only else "")
        out = []
        for r in self.conn.execute(q + " ORDER BY code", (snapshot_id,)):
            d = dict(r)
            d["risk_flags"] = json.loads(d["risk_flags"])
            d["exclusions"] = json.loads(d["exclusions"])
            out.append(d)
        return out

    def snapshot_raw_pages(self, snapshot_id: int) -> list:
        row = self.conn.execute("SELECT raw_gz FROM universe_snapshot WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        return json.loads(gzip.decompress(row[0]).decode("utf-8"))

    # ── 시계열 읽기 ──────────────────────────────────────────
    def get_series(self, series_id: str) -> SeriesMeta | None:
        r = self.conn.execute("SELECT * FROM series WHERE series_id=?", (series_id,)).fetchone()
        if r is None:
            return None
        return SeriesMeta(r["series_id"], r["kind"], r["code"], r["api_id"], r["price_scale"], r["trade_value_unit"],
                          r["volume_unit"], r["revision"], r["adj_upd_stkpc_tp"], r["adj_base_dt"],
                          _dt(r["fetched_at"]), r["job_id"], r["verified_base_dt"], _dt(r["verified_at"]),
                          _d(r["first_date"]), _d(r["last_date"]), _d(r["required_from"]), r["coverage"],
                          r["coverage_detail"], r["no_trades_count"], r["invalid_count"], r["integrity"],
                          r["integrity_detail"])

    def series_ids(self, kind: str | None = None) -> list[str]:
        q, a = "SELECT series_id FROM series", ()
        if kind:
            q, a = q + " WHERE kind=?", (kind,)
        return [r[0] for r in self.conn.execute(q + " ORDER BY series_id", a)]

    @staticmethod
    def _stored(r) -> StoredBar:
        raw = RawBar(date.fromisoformat(r["date"]), r["open_raw"], r["high_raw"], r["low_raw"], r["close_raw"],
                     r["volume"], r["trade_value_raw"], r["quality"])
        return StoredBar(raw, r["run_type"], _dt(r["received_at"]), _dt(r["available_at"]),
                         _dt(r["first_ready_at"]), r["revision"])

    def load_bars(self, series_id: str) -> list[StoredBar]:
        return [self._stored(r) for r in self.conn.execute(
            f"SELECT date, {_COLS}, revision FROM bar WHERE series_id=? ORDER BY date", (series_id,))]

    def load_history(self, series_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM bar_history WHERE series_id=? ORDER BY revision, date", (series_id,))]

    def revisions(self, series_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM series_revision WHERE series_id=? ORDER BY revision", (series_id,))]

    def events(self, series_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM series_event", ()
        if series_id:
            q, a = q + " WHERE series_id=?", (series_id,)
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in self.conn.execute(q + " ORDER BY event_id", a)]

    def research_series(self, series_id: str, as_of: datetime | None = None) -> ResearchSeries:
        """연구 계산 입력. as_of를 주면 그 시각에 활성이던 revision에서 그 시각까지 쓸 수 있던 봉만(A2-R1)."""
        meta = self.get_series(series_id)
        if meta is None:
            raise KeyError(series_id)
        if as_of is None:
            stored, rev, integrity = self.load_bars(series_id), meta.revision, meta.integrity
            basis = AdjustmentBasis(meta.adj_upd_stkpc_tp, meta.adj_base_dt)
        else:
            x = _ts(as_of)
            r = self.conn.execute(
                "SELECT * FROM series_revision WHERE series_id=? AND activated_at<=? AND"
                " (superseded_at IS NULL OR superseded_at>?) ORDER BY revision DESC LIMIT 1",
                (series_id, x, x)).fetchone()
            integrity = "AS_OF"
            if r is None:
                return ResearchSeries([], {}, {}, {}, None, None, integrity, as_of, meta)
            rev, basis = r["revision"], AdjustmentBasis(r["upd_stkpc_tp"], r["base_dt"])
            table = "bar" if rev == meta.revision else "bar_history"
            extra = "" if table == "bar" else " AND revision=?"
            args = (series_id, x) + (() if table == "bar" else (rev,))
            stored = [self._stored(q) for q in self.conn.execute(
                f"SELECT date, {_COLS}, revision FROM {table} WHERE series_id=? AND available_at<=?{extra}"
                " ORDER BY date", args)]
        spec = meta.spec
        bars, avail, first, rtype = [], {}, {}, {}
        for sb in stored:
            rb = to_research_bar(sb.raw, spec)
            if rb is None:
                continue
            bars.append(rb)
            avail[rb.date], first[rb.date], rtype[rb.date] = sb.available_at, sb.first_ready_at, sb.run_type
        return ResearchSeries(bars, avail, first, rtype, rev, basis, integrity, as_of, meta)

    # ── 시계열 쓰기 ──────────────────────────────────────────
    def _insert_bars(self, series_id: str, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            f"INSERT INTO bar(series_id, date, {_COLS}, revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(series_id, *r) for r in rows])

    @staticmethod
    def _row(fb: FetchedBar, run_type: str, available: datetime, first_ready: datetime, revision: int) -> tuple:
        b = fb.raw
        return (b.date.isoformat(), b.open_raw, b.high_raw, b.low_raw, b.close_raw, b.volume, b.trade_value_raw,
                b.quality, run_type, _ts_up(fb.received_at), _ts_up(available), _ts_up(first_ready), revision)

    def _event(self, series_id: str, at: datetime, event: str, detail: dict) -> None:
        self.conn.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
                          (series_id, _ts(at), event, json.dumps(detail, ensure_ascii=False, default=str)))

    def _counts(self, series_id: str) -> tuple:
        r = self.conn.execute(
            "SELECT MIN(date), MAX(date), SUM(quality=?), SUM(quality LIKE 'INVALID%') FROM bar WHERE series_id=?",
            (NO_TRADES, series_id)).fetchone()
        return r[0], r[1], int(r[2] or 0), int(r[3] or 0)

    def _upsert_series(self, *, spec, series_id, code, revision, basis, fetched_at, job_id, verified_base_dt,
                       verified_at, required_from, coverage, coverage_detail, now) -> None:
        first, last, n_nt, n_inv = self._counts(series_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (series_id, spec.kind, code, spec.api_id, spec.price_scale, spec.trade_value_unit, spec.volume_unit,
             revision, basis.upd_stkpc_tp, basis.base_dt, _ts_up(fetched_at), job_id, verified_base_dt,
             _ts_up(verified_at), first, last, required_from.isoformat() if required_from else None, coverage,
             coverage_detail, n_nt, n_inv, _ts(now), INTEGRITY_OK, ""))

    def _invalid(self, series_id, revision, invalid_raw) -> None:
        for d, raw in (invalid_raw or {}).items():
            self.conn.execute("INSERT OR REPLACE INTO bar_invalid_raw VALUES(?,?,?,?)",
                              (series_id, revision, d.isoformat(), json.dumps(raw, ensure_ascii=False)))

    def _run(self, body, in_tx: bool):
        if in_tx:
            return body()
        with self.tx():
            return body()

    def init_series(self, *, spec: SourceSpec, series_id: str, code: str, bars: list[FetchedBar],
                    basis: AdjustmentBasis, activated_at: datetime, job_id: str | None, required_from: date | None,
                    coverage: str, coverage_detail: str, invalid_raw: dict | None = None, reason: str = "",
                    now: datetime | None = None, in_tx: bool = False) -> dict:
        """봉이 없는 시계열의 첫 저장(부족해도 받은 만큼 저장 — coverage로 사유 기록)."""
        now = now or activated_at
        if len({fb.date for fb in bars}) != len(bars):
            raise ValueError(f"{series_id}: 같은 날짜가 두 번 들어옴")

        def body():
            if self.conn.execute("SELECT 1 FROM bar WHERE series_id=? LIMIT 1", (series_id,)).fetchone():
                raise ValueError(f"{series_id}: 이미 봉이 있음 — replace_series로 검증 후 교체")
            meta = self.get_series(series_id)
            revision = meta.revision + 1 if meta else 1
            if meta:
                self.conn.execute("UPDATE series_revision SET superseded_at=? WHERE series_id=? AND revision=?"
                                  " AND superseded_at IS NULL", (_ts_up(activated_at), series_id, meta.revision))
            self._insert_bars(series_id, [self._row(fb, BACKFILL, max(fb.received_at, activated_at), fb.received_at,
                                                    revision) for fb in sorted(bars, key=lambda x: x.date)])
            self._invalid(series_id, revision, invalid_raw)
            self.conn.execute("INSERT INTO series_revision VALUES(?,?,?,?,?,?,?,?)",
                              (series_id, revision, basis.upd_stkpc_tp, basis.base_dt, job_id, reason or "INIT",
                               _ts_up(activated_at), None))
            self._upsert_series(spec=spec, series_id=series_id, code=code, revision=revision, basis=basis,
                                fetched_at=min((fb.received_at for fb in bars), default=activated_at), job_id=job_id,
                                verified_base_dt=basis.base_dt, verified_at=activated_at, required_from=required_from,
                                coverage=coverage, coverage_detail=coverage_detail, now=now)
            self._event(series_id, now, "INIT", {"revision": revision, "bars": len(bars), "job_id": job_id,
                                                 "basis": basis.__dict__, "reason": reason})
            return {"action": "INIT", "revision": revision, "inserted": len(bars)}

        return self._run(body, in_tx)

    def validate_candidate(self, meta: SeriesMeta, candidate: list[FetchedBar], required_from: date | None,
                           expected: dict[date, RawBar] | None) -> list[str]:
        """기존 봉이 있는 시계열을 바꾸기 전 후보 검증 (A2-R2). 빈 목록이면 통과."""
        if not candidate:
            return ["EMPTY_CANDIDATE"]
        cand = {fb.date: fb.raw for fb in candidate}
        c_first, c_last = min(cand), max(cand)
        problems = []
        if meta.last_date is not None and c_last < meta.last_date:
            problems.append(f"SHORT_RECENT:cand_last={c_last}<stored_last={meta.last_date}")
        need = meta.first_date
        if need is not None and required_from is not None:
            need = max(need, required_from)
        if need is not None and c_first > need:
            problems.append(f"SHORT_HISTORY:cand_first={c_first}>need={need}")
        if expected:
            bad = sorted(d for d, rb in expected.items()
                         if d not in cand or cand[d].values() != rb.values() or cand[d].quality != rb.quality)
            if bad:
                problems.append(f"EXPECTED_MISMATCH:{len(bad)}(first={bad[0]})")
        return problems

    def replace_series(self, *, spec: SourceSpec, series_id: str, code: str, candidate: list[FetchedBar],
                       basis: AdjustmentBasis, activated_at: datetime, job_id: str | None,
                       required_from: date | None, coverage: str, coverage_detail: str,
                       expected: dict[date, RawBar] | None = None, forward_after: date | None = None,
                       invalid_raw: dict | None = None, reason: str = "", now: datetime | None = None,
                       in_tx: bool = False) -> dict:
        """기존 봉이 있는 시계열을 후보로 검증 후 EXTEND 또는 REBASE. 검증 실패면 아무것도 바꾸지 않고
        integrity=REBASE_REQUIRED + VERIFY_FAILED 기록 (action = REBASE_FAILED)."""
        now = now or activated_at
        if len({fb.date for fb in candidate}) != len(candidate):
            raise ValueError(f"{series_id}: 같은 날짜가 두 번 들어옴")

        def body():
            meta = self.get_series(series_id)
            old = {sb.raw.date: sb for sb in self.load_bars(series_id)}
            if meta is None or not old:
                raise ValueError(f"{series_id}: 기존 봉 없음 — init_series 사용")
            problems = self.validate_candidate(meta, candidate, required_from, expected)
            if problems:
                detail = ";".join(problems)
                self.conn.execute("UPDATE series SET integrity=?, integrity_detail=?, updated_at=? WHERE series_id=?",
                                  (REBASE_REQUIRED, f"{reason}|{detail}"[:500], _ts(now), series_id))
                self._event(series_id, now, "VERIFY_FAILED", {"problems": problems, "reason": reason,
                                                              "candidate_basis": basis.__dict__,
                                                              "candidate_bars": len(candidate)})
                return {"action": "REBASE_FAILED", "revision": meta.revision, "problems": problems}
            cand = {fb.date: fb for fb in candidate}
            lo, hi = min(cand), max(cand)
            changed = sorted(d for d in cand if d in old and (old[d].raw.values() != cand[d].raw.values()
                                                             or old[d].raw.quality != cand[d].raw.quality))
            lost = sorted(d for d in old if lo <= d <= hi and d not in cand)
            fwd = [d for d, sb in old.items() if sb.run_type == FORWARD]
            forward_start = min(fwd) if fwd else None

            def new_run_type(d: date) -> str:
                # 매일 갱신 중 재수집: 마지막 저장일 뒤, 또는 이미 FORWARD로 쌓던 구간 안의 새 날짜 → FORWARD
                if forward_after is not None and (d > forward_after or (forward_start is not None and d >= forward_start)):
                    return FORWARD
                return BACKFILL

            if changed or lost:                                   # REBASE: 통째 교체
                new_rev = meta.revision + 1
                self.conn.execute(
                    f"INSERT INTO bar_history(series_id, revision, date, {_COLS}, superseded_at, reason)"
                    f" SELECT series_id, revision, date, {_COLS}, ?, ? FROM bar WHERE series_id=?",
                    (_ts_up(activated_at), reason or "REBASE", series_id))
                self.conn.execute("UPDATE series_revision SET superseded_at=? WHERE series_id=? AND revision=?",
                                  (_ts_up(activated_at), series_id, meta.revision))
                self.conn.execute("DELETE FROM bar WHERE series_id=?", (series_id,))
                rows = []
                for d in sorted(cand):
                    fb, prev = cand[d], old.get(d)
                    run_type = prev.run_type if prev else new_run_type(d)
                    first_ready = min(prev.first_ready_at, fb.received_at) if prev else fb.received_at
                    rows.append(self._row(fb, run_type, max(fb.received_at, activated_at), first_ready, new_rev))
                self._insert_bars(series_id, rows)
                self._invalid(series_id, new_rev, invalid_raw)
                self.conn.execute("INSERT INTO series_revision VALUES(?,?,?,?,?,?,?,?)",
                                  (series_id, new_rev, basis.upd_stkpc_tp, basis.base_dt, job_id, reason or "REBASE",
                                   _ts_up(activated_at), None))
                self._upsert_series(spec=spec, series_id=series_id, code=code, revision=new_rev, basis=basis,
                                    fetched_at=min(fb.received_at for fb in candidate), job_id=job_id,
                                    verified_base_dt=basis.base_dt, verified_at=activated_at,
                                    required_from=required_from, coverage=coverage, coverage_detail=coverage_detail,
                                    now=now)
                self._event(series_id, now, "REBASE", {
                    "old_revision": meta.revision, "revision": new_rev, "basis": basis.__dict__, "job_id": job_id,
                    "changed": len(changed), "lost": len(lost), "new_dates": len(set(cand) - set(old)),
                    "reason": reason, "sample": [{"date": d.isoformat(), "old": old[d].raw.values(),
                                                  "new": cand[d].raw.values()} for d in changed[:3]]})
                return {"action": "REBASE", "revision": new_rev, "changed": len(changed), "lost": len(lost)}
            # EXTEND: 같은 조정 기준이 확인됨 → 없는 날짜만 추가 (revision 유지, 기존 값·시각 그대로)
            new_dates = sorted(d for d in cand if d not in old)
            self._insert_bars(series_id, [self._row(cand[d], new_run_type(d), cand[d].received_at,
                                                    cand[d].received_at, meta.revision) for d in new_dates])
            self._invalid(series_id, meta.revision, {d: v for d, v in (invalid_raw or {}).items() if d in new_dates})
            first, last, n_nt, n_inv = self._counts(series_id)
            self.conn.execute(
                "UPDATE series SET verified_base_dt=?, verified_at=?, first_date=?, last_date=?, required_from=?,"
                " coverage=?, coverage_detail=?, no_trades_count=?, invalid_count=?, updated_at=?, integrity=?,"
                " integrity_detail='' WHERE series_id=?",
                (basis.base_dt, _ts_up(activated_at), first, last,
                 required_from.isoformat() if required_from else meta.required_from and meta.required_from.isoformat(),
                 coverage, coverage_detail, n_nt, n_inv, _ts(now), INTEGRITY_OK, series_id))
            event = "EXTEND" if new_dates else "VERIFIED"
            self._event(series_id, now, event, {"revision": meta.revision, "new_dates": len(new_dates),
                                                "verified_base_dt": basis.base_dt, "reason": reason})
            return {"action": event, "revision": meta.revision, "inserted": len(new_dates)}

        return self._run(body, in_tx)

    def append_forward(self, *, series_id: str, bars: list[FetchedBar], verified_base_dt: str,
                       verified_at: datetime, now: datetime | None = None) -> int:
        """첫 페이지 겹침 확인을 마친 뒤 새 날짜만 FORWARD로 추가 (available_at = 그 행의 수신 시각)."""
        now = now or verified_at
        with self.tx():
            meta = self.get_series(series_id)
            if meta is None:
                raise KeyError(series_id)
            if meta.integrity != INTEGRITY_OK:
                raise IntegrityError(f"{series_id}: {meta.integrity} — 재검증 전에는 새 날짜를 붙이지 않음")
            if bars and meta.last_date is not None and min(fb.date for fb in bars) <= meta.last_date:
                raise ValueError(f"{series_id}: 마지막 저장일 이전 날짜는 append 불가")
            self._insert_bars(series_id, [self._row(fb, FORWARD, fb.received_at, fb.received_at, meta.revision)
                                          for fb in bars])
            first, last, n_nt, n_inv = self._counts(series_id)
            self.conn.execute(
                "UPDATE series SET verified_base_dt=?, verified_at=?, first_date=?, last_date=?, no_trades_count=?,"
                " invalid_count=?, updated_at=? WHERE series_id=?",
                (verified_base_dt, _ts_up(verified_at), first, last, n_nt, n_inv, _ts(now), series_id))
            self._event(series_id, now, "APPEND" if bars else "VERIFIED",
                        {"added": len(bars), "verified_base_dt": verified_base_dt, "scope": "FIRST_PAGE"})
        return len(bars)

    def mark_integrity(self, series_id: str, detail: str, now: datetime, status: str = REBASE_REQUIRED) -> None:
        """값을 바꾸지 않고 재검증 필요 상태만 기록 (재수집 조회 자체가 실패한 경우 등)."""
        with self.tx():
            self.conn.execute("UPDATE series SET integrity=?, integrity_detail=?, updated_at=? WHERE series_id=?",
                              (status, detail[:500], _ts(now), series_id))
            self._event(series_id, now, "VERIFY_FAILED", {"detail": detail[:500], "status": status})

    # ── 백필 작업 ────────────────────────────────────────────
    def create_job(self, *, job_id: str, kind: str, created_at: datetime, base_dt: str, upd_stkpc_tp: str,
                   required_from: date, max_pages: int, snapshot_id: int | None, targets: list[dict],
                   params: dict) -> None:
        with self.tx():
            self.conn.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (job_id, kind, _ts(created_at), base_dt, upd_stkpc_tp, required_from.isoformat(),
                               max_pages, snapshot_id, "OPEN", json.dumps(params, ensure_ascii=False)))
            self.conn.executemany(
                "INSERT INTO job_item VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(job_id, t["series_id"], i, t["kind"], t["code"], t.get("reg_day"), PENDING, 0, None, None, "", 0,
                  _ts(created_at)) for i, t in enumerate(targets)])

    def get_job(self, job_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
        return None if r is None else {**dict(r), "params": json.loads(r["params_json"])}

    def open_jobs(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM job WHERE status='OPEN' ORDER BY created_at")]

    def job_items(self, job_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        q, a = "SELECT * FROM job_item WHERE job_id=?", [job_id]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            a += list(statuses)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY seq", a)]

    def job_counts(self, job_id: str) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM job_item WHERE job_id=? GROUP BY status", (job_id,))}

    def set_item(self, job_id: str, series_id: str, *, status: str, pages: int, first_date: date | None,
                 last_date: date | None, reason: str, now: datetime, in_tx: bool = False) -> None:
        def body():
            self.conn.execute(
                "UPDATE job_item SET status=?, pages=?, first_date=?, last_date=?, reason=?, attempts=attempts+1,"
                " updated_at=? WHERE job_id=? AND series_id=?",
                (status, pages, first_date.isoformat() if first_date else None,
                 last_date.isoformat() if last_date else None, reason, _ts(now), job_id, series_id))
        self._run(body, in_tx)

    def close_job_if_finished(self, job_id: str) -> bool:
        with self.tx():
            left = self.conn.execute("SELECT COUNT(*) FROM job_item WHERE job_id=? AND status IN (?,?)",
                                     (job_id, PENDING, ERROR)).fetchone()[0]
            if left == 0:
                self.conn.execute("UPDATE job SET status='DONE' WHERE job_id=?", (job_id,))
        return left == 0
