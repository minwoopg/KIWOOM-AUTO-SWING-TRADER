from __future__ import annotations

"""연구 데이터 저장소 (A2, SQLite 한 파일 — 기본 data/research/research.sqlite3, git 제외).

담는 것
- universe_snapshot / universe_row : 날짜별 종목 목록. `snapshot_date`와 **`observed_at`(실제 조회 시각)**,
  장 단계, 분류 정책 버전을 함께 저장. 원래 응답 전체는 gzip으로 raw_gz에 보존.
- series / bar : 종목·지수 일봉. 원천 정수(부호만 뗀 값)·품질(NO_TRADES / INVALID:…)을 그대로 두고
  배율(지수 ÷100)·단위(백만원 → 원)는 읽을 때 적용.
  * run_type: BACKFILL(과거 일괄 수집 — 확보 시각 모름) / FORWARD(매일 수집 — ready_at 있음)
  * ready_at: 그 날짜 봉이 **완성 봉으로 처음 들어온 응답의 수신 시각**. 한 번 기록하면 바꾸지 않음.
- 수정주가 기준 (A2 보완 4)
  * series마다 조정 기준(upd_stkpc_tp·base_dt)과 조회 시각, revision을 저장.
  * 한 종목 시계열은 **한 번의 연속 조회(같은 base_dt, 모든 페이지)** 로만 채웁니다.
  * 매일 갱신은 겹치는 구간 값이 모두 같을 때만 새 날짜를 붙입니다(verified_base_dt 갱신).
    값이 하나라도 다르거나 사라진 날짜가 있으면 **전체를 새 기준으로 다시 받아 통째로 교체**하고,
    이전 값은 bar_history(이전 revision)에 남깁니다 → 한 시계열 안에 서로 다른 조정 기준이 섞이지 않음.
  * 수정주가는 과거 실제 체결가격이 아닙니다(분할·증자 등으로 과거 가격이 다시 계산된 값).
- job / job_item : 재개 가능한 백필. 작업마다 base_dt를 고정하고, 종목 단위로 한 트랜잭션에 저장.
- bar_invalid_raw : INVALID 행의 원래 JSON. series_event : INIT·REBASE·APPEND 등 변경 기록.
"""

import gzip
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from domain.research.series import ResearchBar
from infra.research.kiwoom_rows import NO_TRADES, RawBar, SourceSpec, to_research_bar

SCHEMA_VERSION = "r1"
BACKFILL, FORWARD = "BACKFILL", "FORWARD"
COVERAGE_OK, LISTED_AFTER_START, HISTORY_END, PAGE_CAP = "OK", "LISTED_AFTER_START", "HISTORY_END", "PAGE_CAP"
PENDING, DONE, SHORTFALL, ERROR = "PENDING", "DONE", "SHORTFALL", "ERROR"

_SCHEMA = """
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
  no_trades_count INTEGER NOT NULL, invalid_count INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bar(
  series_id TEXT NOT NULL, date TEXT NOT NULL,
  open_raw INTEGER, high_raw INTEGER, low_raw INTEGER, close_raw INTEGER, volume INTEGER, trade_value_raw INTEGER,
  quality TEXT NOT NULL, run_type TEXT NOT NULL, ready_at TEXT, fetched_at TEXT NOT NULL, revision INTEGER NOT NULL,
  PRIMARY KEY(series_id, date)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bar_history(
  series_id TEXT NOT NULL, revision INTEGER NOT NULL, date TEXT NOT NULL,
  open_raw INTEGER, high_raw INTEGER, low_raw INTEGER, close_raw INTEGER, volume INTEGER, trade_value_raw INTEGER,
  quality TEXT NOT NULL, run_type TEXT NOT NULL, ready_at TEXT, fetched_at TEXT NOT NULL,
  superseded_at TEXT NOT NULL, reason TEXT NOT NULL,
  PRIMARY KEY(series_id, revision, date)) WITHOUT ROWID;
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

_BAR_COLS = "open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw, quality, run_type, ready_at, fetched_at, revision"


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _d(s: str | None) -> date | None:
    return None if s is None else date.fromisoformat(s)


@dataclass(frozen=True)
class AdjustmentBasis:
    upd_stkpc_tp: str | None        # 종목 "1"(수정주가). 지수는 None
    base_dt: str                    # 조회 기준일 YYYYMMDD


@dataclass(frozen=True)
class StoredBar:
    raw: RawBar
    run_type: str
    ready_at: datetime | None
    fetched_at: datetime
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

    @property
    def spec(self) -> SourceSpec:
        from infra.research.kiwoom_rows import INDEX_DAILY, STOCK_DAILY
        base = STOCK_DAILY if self.kind == "STOCK" else INDEX_DAILY
        return SourceSpec(self.kind, self.api_id, base.list_key, self.price_scale, self.trade_value_unit,
                          self.volume_unit)


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
        elif cur[0] != SCHEMA_VERSION:
            raise RuntimeError(f"연구 저장소 스키마 {cur[0]} ≠ {SCHEMA_VERSION}: {self.path}")

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
                (snapshot_date.isoformat(), _ts(observed_at), _ts(observed_end_at), market_phase, policy_version,
                 source, len(records), json.dumps(summary, ensure_ascii=False), raw_gz))
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

    # ── 시계열 ───────────────────────────────────────────────
    def get_series(self, series_id: str) -> SeriesMeta | None:
        r = self.conn.execute("SELECT * FROM series WHERE series_id=?", (series_id,)).fetchone()
        if r is None:
            return None
        return SeriesMeta(r["series_id"], r["kind"], r["code"], r["api_id"], r["price_scale"], r["trade_value_unit"],
                          r["volume_unit"], r["revision"], r["adj_upd_stkpc_tp"], r["adj_base_dt"],
                          _dt(r["fetched_at"]), r["job_id"], r["verified_base_dt"], _dt(r["verified_at"]),
                          _d(r["first_date"]), _d(r["last_date"]), _d(r["required_from"]), r["coverage"],
                          r["coverage_detail"], r["no_trades_count"], r["invalid_count"])

    def series_ids(self, kind: str | None = None) -> list[str]:
        q, a = "SELECT series_id FROM series", ()
        if kind:
            q, a = q + " WHERE kind=?", (kind,)
        return [r[0] for r in self.conn.execute(q + " ORDER BY series_id", a)]

    def load_bars(self, series_id: str) -> list[StoredBar]:
        out = []
        for r in self.conn.execute(f"SELECT date, {_BAR_COLS} FROM bar WHERE series_id=? ORDER BY date", (series_id,)):
            raw = RawBar(date.fromisoformat(r["date"]), r["open_raw"], r["high_raw"], r["low_raw"], r["close_raw"],
                         r["volume"], r["trade_value_raw"], r["quality"])
            out.append(StoredBar(raw, r["run_type"], _dt(r["ready_at"]), _dt(r["fetched_at"]), r["revision"]))
        return out

    def load_history(self, series_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM bar_history WHERE series_id=? ORDER BY revision, date", (series_id,))]

    def events(self, series_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM series_event", ()
        if series_id:
            q, a = q + " WHERE series_id=?", (series_id,)
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in self.conn.execute(q + " ORDER BY event_id", a)]

    def research_series(self, series_id: str) -> tuple[list[ResearchBar], dict[date, datetime], SeriesMeta]:
        """연구 계산용 (봉 목록, FORWARD 봉의 ready_at, 메타). INVALID 봉은 빠짐(→ DATA_GAP)."""
        meta = self.get_series(series_id)
        if meta is None:
            raise KeyError(series_id)
        spec = meta.spec
        bars, ready = [], {}
        for sb in self.load_bars(series_id):
            rb = to_research_bar(sb.raw, spec)
            if rb is not None:
                bars.append(rb)
            if sb.run_type == FORWARD and sb.ready_at is not None:
                ready[sb.raw.date] = sb.ready_at
        return bars, ready, meta

    def _insert_bars(self, series_id: str, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            f"INSERT INTO bar(series_id, date, {_BAR_COLS}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(series_id, *r) for r in rows])

    def _event(self, series_id: str, at: datetime, event: str, detail: dict) -> None:
        self.conn.execute("INSERT INTO series_event(series_id, at, event, detail_json) VALUES(?,?,?,?)",
                          (series_id, _ts(at), event, json.dumps(detail, ensure_ascii=False, default=str)))

    def _refresh_meta_counts(self, series_id: str) -> tuple:
        r = self.conn.execute(
            "SELECT MIN(date), MAX(date), SUM(quality=?), SUM(quality LIKE 'INVALID%') FROM bar WHERE series_id=?",
            (NO_TRADES, series_id)).fetchone()
        return r[0], r[1], int(r[2] or 0), int(r[3] or 0)

    def write_full_series(self, *, spec: SourceSpec, series_id: str, code: str, bars: list[RawBar],
                          basis: AdjustmentBasis, fetched_at: datetime, job_id: str | None,
                          required_from: date | None, coverage: str, coverage_detail: str,
                          forward_after: date | None = None, forward_ready_at: datetime | None = None,
                          invalid_raw: dict[date, dict] | None = None, reason: str = "",
                          now: datetime | None = None, in_tx: bool = False) -> dict:
        """한 번의 연속 조회로 받은 전체 시계열을 저장합니다.

        - 저장된 값과 겹치는 구간이 모두 같으면 없는 날짜만 추가(revision 유지).
        - 하나라도 다르거나 새 범위 안에서 사라진 날짜가 있으면 REBASE: 기존 봉 전부를 bar_history로 옮기고
          새 값으로 통째로 교체(revision+1). 그 날짜의 run_type·ready_at(처음 완성 확보 시각)은 유지.
        - forward_after(매일 갱신 중 재수집)가 있으면 그 날짜 뒤의 새 날짜와, 이미 FORWARD로 쌓던 구간 안에서
          새로 생긴 날짜(누락 복구)는 FORWARD(ready_at = forward_ready_at). 나머지 새 날짜는 BACKFILL.
        """
        now = now or fetched_at
        new = {b.date: b for b in bars}
        if len(new) != len(bars):
            raise ValueError(f"{series_id}: 같은 날짜가 두 번 들어옴")

        def body() -> dict:
            meta = self.get_series(series_id)
            old = {sb.raw.date: sb for sb in self.load_bars(series_id)}
            lo, hi = (min(new), max(new)) if new else (None, None)
            changed = [d for d in new if d in old and (old[d].raw.values() != new[d].values()
                                                       or old[d].raw.quality != new[d].quality)]
            lost = [d for d in old if lo is not None and lo <= d <= hi and d not in new]
            rebase = bool(old) and bool(changed or lost)
            if meta is None:
                revision = 1
            elif rebase or not old:
                revision = meta.revision + 1
            else:
                revision = meta.revision
            if rebase:
                self.conn.execute(
                    f"INSERT INTO bar_history(series_id, revision, date, {_BAR_COLS.replace(', revision', '')},"
                    " superseded_at, reason) SELECT series_id, revision, date,"
                    f" {_BAR_COLS.replace(', revision', '')}, ?, ? FROM bar WHERE series_id=?",
                    (_ts(now), reason or "REBASE", series_id))
                self.conn.execute("DELETE FROM bar WHERE series_id=?", (series_id,))
                keep_meta = old
                to_insert = sorted(new)
            else:
                keep_meta = {}
                to_insert = sorted(d for d in new if d not in old)
            # 매일 갱신 중의 재수집이면, 이미 FORWARD로 쌓던 구간 안에서 새로 생긴 날짜(누락 복구)도
            # FORWARD — ready_at은 이번 복구 조회 시각(그 전에는 확보하지 못했으므로).
            fwd_dates = [d for d, sb in old.items() if sb.run_type == FORWARD]
            forward_start = min(fwd_dates) if fwd_dates else None
            rows = []
            for d in to_insert:
                b = new[d]
                prev = keep_meta.get(d) if rebase else None
                if prev is not None:
                    run_type, ready = prev.run_type, prev.ready_at
                elif forward_after is not None and (d > forward_after
                                                    or (forward_start is not None and d >= forward_start)):
                    run_type, ready = FORWARD, forward_ready_at
                else:
                    run_type, ready = BACKFILL, None
                rows.append((d.isoformat(), b.open_raw, b.high_raw, b.low_raw, b.close_raw, b.volume,
                             b.trade_value_raw, b.quality, run_type, _ts(ready), _ts(fetched_at), revision))
            self._insert_bars(series_id, rows)
            for d, raw in (invalid_raw or {}).items():
                self.conn.execute("INSERT OR REPLACE INTO bar_invalid_raw VALUES(?,?,?,?)",
                                  (series_id, revision, d.isoformat(), json.dumps(raw, ensure_ascii=False)))
            first, last, n_nt, n_inv = self._refresh_meta_counts(series_id)
            self.conn.execute(
                "INSERT OR REPLACE INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (series_id, spec.kind, code, spec.api_id, spec.price_scale, spec.trade_value_unit, spec.volume_unit,
                 revision, basis.upd_stkpc_tp, basis.base_dt, _ts(fetched_at), job_id, basis.base_dt, _ts(fetched_at),
                 first, last, required_from.isoformat() if required_from else None, coverage, coverage_detail,
                 n_nt, n_inv, _ts(now)))
            event = "REBASE" if rebase else ("INIT" if not old else "EXTEND")
            detail = {"basis": {"upd_stkpc_tp": basis.upd_stkpc_tp, "base_dt": basis.base_dt}, "job_id": job_id,
                      "revision": revision, "inserted": len(rows), "new_dates": len(set(new) - set(old)),
                      "changed": len(changed), "lost": len(lost), "reason": reason,
                      "sample": [{"date": d.isoformat(), "old": old[d].raw.values(), "new": new[d].values()}
                                 for d in sorted(changed)[:3]]}
            self._event(series_id, now, event, detail)
            return {"action": event, "revision": revision, "inserted": len(rows), "changed": len(changed),
                    "lost": len(lost)}

        if in_tx:
            return body()
        with self.tx():
            return body()

    def append_forward(self, *, series_id: str, bars: list[RawBar], received_at: datetime,
                       verified_base_dt: str, now: datetime | None = None) -> int:
        """겹치는 구간 확인을 마친 뒤 새 날짜만 FORWARD로 추가 (ready_at = 이번 응답 수신 시각)."""
        now = now or received_at
        with self.tx():
            meta = self.get_series(series_id)
            if meta is None:
                raise KeyError(series_id)
            if bars and meta.last_date is not None and min(b.date for b in bars) <= meta.last_date:
                raise ValueError(f"{series_id}: 마지막 저장일 이전 날짜는 append 불가")
            self._insert_bars(series_id, [
                (b.date.isoformat(), b.open_raw, b.high_raw, b.low_raw, b.close_raw, b.volume, b.trade_value_raw,
                 b.quality, FORWARD, _ts(received_at), _ts(received_at), meta.revision) for b in bars])
            first, last, n_nt, n_inv = self._refresh_meta_counts(series_id)
            self.conn.execute(
                "UPDATE series SET verified_base_dt=?, verified_at=?, first_date=?, last_date=?, no_trades_count=?,"
                " invalid_count=?, updated_at=? WHERE series_id=?",
                (verified_base_dt, _ts(received_at), first, last, n_nt, n_inv, _ts(now), series_id))
            self._event(series_id, now, "APPEND" if bars else "VERIFIED",
                        {"added": len(bars), "verified_base_dt": verified_base_dt})
        return len(bars)

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
        if in_tx:
            body()
        else:
            with self.tx():
                body()

    def close_job_if_finished(self, job_id: str) -> bool:
        with self.tx():
            left = self.conn.execute("SELECT COUNT(*) FROM job_item WHERE job_id=? AND status IN (?,?)",
                                     (job_id, PENDING, ERROR)).fetchone()[0]
            if left == 0:
                self.conn.execute("UPDATE job SET status='DONE' WHERE job_id=?", (job_id,))
        return left == 0
