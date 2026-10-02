from __future__ import annotations

"""연구 데이터 저장소 (A2, SQLite 한 파일 — 기본 data/research/research.sqlite3, git 제외). 스키마 r5.

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

정합성 이력 (series_integrity, r3): 재수집 실패(REBASE_REQUIRED)·복구(OK)를 시각과 함께 남깁니다.
시점 조회는 그 시각의 상태를 돌려주므로, 나중에 복구돼도 실패 당시 평가는 보류(UNKNOWN)로 재현됩니다.

기존 DB 이전 (열 때 자동, 한 트랜잭션씩 — 바꾸기 전에 같은 폴더에 백업 `<파일>.bak-<옛 버전>-<시각>`)
- r1 → r2: 봉별 fetched_at(첫 페이지 수신 시각) → received_at, 임시 available_at, revision 행 생성.
- r2 → r3 (이미 r2로 이전된 DB 포함): r1에서 옮겨 온 판의 활성 시각을 **r1이 저장 때마다 남긴 변경 기록
  (INIT·REBASE)** 으로 복원하고, 봉 available_at = max(활성 시각, 그 봉 수신 시각 이후 첫 저장 기록 시각)으로
  다시 계산합니다(첫 페이지 수신 시각을 사용 가능 시각으로 쓰지 않음). revision 구간이 겹치거나 기록이 없어
  입증할 수 없으면 time_basis=UNPROVEN — 시점 조회에서 보류. 정합성 이력도 VERIFY_FAILED 기록에서 다시 만듭니다.
- r3·r4 → r5: 이전된 판의 MIGRATED·UNPROVEN 봉을 아래 근거 규칙으로 다시 계산(r3 뒤 새로 저장된 OBSERVED 봉은 그대로).
  · 같은 초 규칙(r4): 저장 기록 시각은 초 내림·수신 시각은 초 올림이라 같은 초 저장이 최대 1초 앞서 보일 수 있음.
  · 판 귀속(r5, GPT B1): 저장 근거는 **그 봉과 같은 revision의 기록만**. revision이 적힌 기록은 그대로,
    revision이 없는 예전 기록(r1~r4의 APPEND)은 바로 앞 INIT·REBASE 기록(기록 순서)의 판으로 보고, 그 판의
    활성 구간 [활성 시각, 다음 판 활성 시각) 안에 있을 때만 인정. 앞 기록이 없거나 구간 밖·경계와 같은 초면
    어느 판인지 모호하므로 근거로 쓰지 않음. 새 APPEND 기록에는 revision을 적습니다.
  · 판정은 `plan_migrated_bar` 하나로 — 읽기 전용 점검(`store_inspect`)과 실제 보정이 같은 함수를 씁니다.
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

SCHEMA_VERSION = "r5"
SECOND = timedelta(seconds=1)
WRITE_EVENTS = ("INIT", "REBASE", "EXTEND", "APPEND")      # 봉을 실제로 저장한 기록
ACTIVATING_EVENTS = ("INIT", "REBASE")                       # 새 판(revision)을 활성화한 기록
OBSERVED_TIME, MIGRATED_TIME, UNPROVEN_TIME = "OBSERVED", "MIGRATED", "UNPROVEN"
NO_REVISION = "NO_REVISION"
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
_COLS_R2 = ("open_raw, high_raw, low_raw, close_raw, volume, trade_value_raw, quality, run_type, received_at, "
            "available_at, first_ready_at")
_COLS = _COLS_R2 + ", time_basis"
_SCHEMA_R3 = """
CREATE TABLE IF NOT EXISTS series_integrity(
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, series_id TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_integrity_series_at ON series_integrity(series_id, at);
CREATE INDEX IF NOT EXISTS ix_event_series ON series_event(series_id, event_id);
"""


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


def _rev_of(detail) -> int | None:
    rv = detail.get("revision") if isinstance(detail, dict) else None
    return rv if isinstance(rv, int) and not isinstance(rv, bool) else None


@dataclass(frozen=True)
class RevisionTimeline:
    """한 시계열의 판(revision) 순서와 활성 시각. 이전된 판(MIGRATED_R1…)은 INIT·REBASE 기록 시각으로 복원."""
    order: tuple[int, ...]
    act: dict          # revision → 활성 시각(ISO 초)
    proven: dict       # revision → 활성 시각을 기록으로 입증했는지
    migrated: dict     # revision → r1에서 옮겨 온 판인지

    def superseded(self, revision: int) -> str | None:
        i = self.order.index(revision)
        return self.act[self.order[i + 1]] if i + 1 < len(self.order) else None


def revision_timeline(revs: list[dict], events: list[tuple[str, str, dict]]) -> RevisionTimeline:
    """revs: series_revision 행(revision 순), events: (기록 시각, 종류, detail) — series_event를 event_id 순으로."""
    made: dict[int, str] = {}
    for at, ev, detail in events:
        rv = _rev_of(detail)
        if ev in ACTIVATING_EVENTS and rv is not None and rv not in made:
            made[rv] = at
    act, proven, mig = {}, {}, {}
    for r in revs:
        rv = r["revision"]
        mig[rv] = r["reason"].startswith("MIGRATED_R1")
        if mig[rv]:
            proven[rv] = rv in made
            act[rv] = made.get(rv, r["activated_at"])
        else:
            proven[rv], act[rv] = True, r["activated_at"]
    order = tuple(r["revision"] for r in revs)
    for a, b in zip(order, order[1:]):              # 구간이 겹치면(활성 시각이 증가하지 않으면) 입증 실패
        if act[a] is None or act[b] is None or not act[b] > act[a]:
            proven[a] = proven[b] = False
    return RevisionTimeline(order, act, proven, mig)


@dataclass(frozen=True)
class WriteRecord:
    """봉을 저장한 기록 하나와 그 기록이 속한 판.

    source: EXPLICIT(기록에 revision이 적혀 있음) / ORDER(예전 기록 — 바로 앞 INIT·REBASE의 판, 활성 구간 안) /
            UNATTRIBUTED:<사유>(어느 판인지 모호 — 근거로 쓰지 않음)."""
    at: str
    event: str
    revision: int | None
    source: str


def attribute_writes(events: list[tuple[str, str, dict]], tl: RevisionTimeline) -> list[WriteRecord]:
    """저장 기록(INIT·REBASE·EXTEND·APPEND)마다 속한 판을 정합니다 (GPT B1). events는 event_id(기록 순서)대로.

    revision 없는 예전 기록은 '바로 앞 INIT·REBASE 기록의 판'으로 보되, 그 판의 활성 구간
    [활성 시각, 다음 판 활성 시각) 안에 있을 때만 인정 — 다음 판 활성과 같은 초(경계)거나 구간 밖이면 모호."""
    out: list[WriteRecord] = []
    anchor: int | None = None
    anchor_note = "NO_PRIOR_REVISION"
    for at, ev, detail in events:
        rv = _rev_of(detail)
        if ev in ACTIVATING_EVENTS:
            anchor, anchor_note = (rv, "") if rv is not None else (None, "ACTIVATION_WITHOUT_REVISION")
        if ev not in WRITE_EVENTS:
            continue
        if rv is not None:
            out.append(WriteRecord(at, ev, rv, "EXPLICIT"))
            continue
        if anchor is None:
            out.append(WriteRecord(at, ev, None, f"UNATTRIBUTED:{anchor_note}"))
            continue
        start = tl.act.get(anchor)
        end = tl.superseded(anchor) if anchor in tl.order else None
        if anchor not in tl.order or start is None:
            why = "UNKNOWN_REVISION"
        elif at < start:
            why = "BEFORE_ACTIVATION"
        elif end is not None and at >= end:
            why = "AT_OR_AFTER_NEXT_REVISION"
        else:
            out.append(WriteRecord(at, ev, anchor, "ORDER"))
            continue
        out.append(WriteRecord(at, ev, None, f"UNATTRIBUTED:{why}"))
    return out


def find_write_evidence(writes: list[WriteRecord], revision: int, received_at: str) -> WriteRecord | None:
    """그 봉(revision, 수신 시각)을 저장한 기록 — 없으면 None(입증 불가).

    **같은 revision으로 귀속된** 저장 기록 중 '수신 시각 − 1초' 이후의 첫 기록. 다른 판·귀속 불가 기록은 쓰지 않음.
    1초: 저장 기록 시각은 초 내림, 수신 시각은 초 올림으로 남아 같은 초 저장이 1초 앞서 보일 수 있음."""
    floor_recv = (datetime.fromisoformat(received_at) - SECOND).isoformat(timespec="seconds")
    cands = sorted((w for w in writes if w.revision == revision and w.at >= floor_recv), key=lambda w: w.at)
    return cands[0] if cands else None


@dataclass(frozen=True)
class BarTimePlan:
    """이전된 판의 봉 묶음(revision·수신 시각)에 대한 판정 — 점검과 보정이 같은 결과를 씁니다."""
    verdict: str                  # PROVABLE / PROVABLE_SAME_SECOND / REVISION_UNPROVEN / NO_EVIDENCE / NOT_MIGRATED
    time_basis: str | None        # MIGRATED / UNPROVEN (NOT_MIGRATED면 None — 건드리지 않음)
    available_at: str | None      # MIGRATED일 때만. UNPROVEN은 기존 값 유지
    evidence: WriteRecord | None


def plan_migrated_bar(tl: RevisionTimeline, writes: list[WriteRecord], revision: int,
                      received_at: str) -> BarTimePlan:
    if not tl.migrated.get(revision, False):
        return BarTimePlan("NOT_MIGRATED", None, None, None)
    if not tl.proven.get(revision, False):
        return BarTimePlan("REVISION_UNPROVEN", UNPROVEN_TIME, None, None)
    w = find_write_evidence(writes, revision, received_at)
    if w is None:
        return BarTimePlan("NO_EVIDENCE", UNPROVEN_TIME, None, None)
    verdict = "PROVABLE_SAME_SECOND" if w.at < received_at else "PROVABLE"
    return BarTimePlan(verdict, MIGRATED_TIME, max(tl.act[revision], received_at, w.at), w)


def load_series_evidence(conn: sqlite3.Connection, sid: str) -> tuple[RevisionTimeline, list[WriteRecord], dict]:
    """(판 순서, 귀속된 저장 기록, revision → 사유). 보정·점검이 같은 방식으로 읽습니다."""
    revs = [{"revision": r[0], "reason": r[1], "activated_at": r[2]} for r in conn.execute(
        "SELECT revision, reason, activated_at FROM series_revision WHERE series_id=? ORDER BY revision", (sid,))]
    events = [(r[0], r[1], json.loads(r[2])) for r in conn.execute(
        "SELECT at, event, detail_json FROM series_event WHERE series_id=? ORDER BY event_id", (sid,))]
    tl = revision_timeline(revs, events)
    return tl, attribute_writes(events, tl), {r["revision"]: r["reason"] for r in revs}


def sqlite_backup(conn: sqlite3.Connection, path: str, version: str) -> str:
    """SQLite 백업 API로 일관된 사본 `<파일>.bak-<옛 버전>-<시각>`(쓰는 중이어도 안전). 같은 이름이 있으면 번호를 붙임."""
    base = f"{path}.bak-{version}-{datetime.now():%Y%m%d_%H%M%S}"
    dest, n = base, 1
    while Path(dest).exists():
        dest, n = f"{base}_{n}", n + 1
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


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
    time_basis: str = OBSERVED_TIME   # OBSERVED(새 기록) / MIGRATED(이전 버전 기록, 저장 기록으로 시각 복원) / UNPROVEN


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
    """연구 계산 입력. available_at은 주봉 OBSERVED 모드의 data_ready_at으로 그대로 넘깁니다.

    query_mode : CURRENT(현재 revision 전체 — 가정 분석) / AS_OF(그 시각에 알 수 있던 값)
    integrity  : 그 시각의 정합성 상태 — OK / REBASE_REQUIRED / NO_REVISION(그 시각엔 시계열 없음).
                 A4는 OK가 아니면 그 종목·지수 판정을 UNKNOWN으로 둡니다.
    time_proof : OK / UNPROVEN — 확보 시각을 입증하지 못한 봉이 그 revision에 있으면 UNPROVEN.
                 AS_OF 조회는 그런 봉을 돌려주지 않고 표시만 하므로, A4는 UNPROVEN이면 보류합니다.
    revision_info : 조회에 쓴 revision의 기록(조정 기준·활성·대체 시각·사유) — 그 시점의 메타.
    current_meta  : **현재** 시계열 메타(지금 상태). 과거 시점 판단에 쓰지 않습니다.
    """
    bars: list[ResearchBar]
    available_at: dict[date, datetime]
    first_ready_at: dict[date, datetime]
    run_type: dict[date, str]
    revision: int | None
    basis: AdjustmentBasis | None
    query_mode: str
    integrity: str
    integrity_detail: str
    time_proof: str
    unproven_bars: int
    as_of: datetime | None
    revision_info: dict | None = None
    current_meta: SeriesMeta = field(repr=False, default=None)


class ResearchStore:
    def __init__(self, path: str | Path, *, backup_before_upgrade: bool = True) -> None:
        """기존 DB의 스키마를 올려야 하면, 바꾸기 전에 같은 폴더에 백업(<파일>.bak-<옛 버전>-<시각>)을 만듭니다."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.backup_path: str | None = None
        self.upgrade_summary: dict | None = None     # 이번에 열면서 시각을 다시 점검했으면 그 집계
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_TABLES)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if cur is None:
            self.conn.executescript(_SCHEMA_R2)
            self.conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', 'r2')")
            version = "r2"
        else:
            version = cur[0]
            if version != SCHEMA_VERSION and backup_before_upgrade:
                self.backup_path = self._backup(version)
        if version == "r1":
            self._migrate_r1_to_r2()
            version = "r2"
        if version == "r2":
            self._upgrade_r2_to_r3()
            version = "r3"
        if version in ("r3", "r4"):
            self._recheck_migrated_times(version)
            version = SCHEMA_VERSION
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"연구 저장소 스키마 {version} ≠ {SCHEMA_VERSION}: {self.path}")

    def _backup(self, version: str) -> str:
        return sqlite_backup(self.conn, self.path, version)

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
                f"INSERT INTO bar(series_id, date, {_COLS_R2}, revision) SELECT b.series_id, b.date, b.open_raw,"
                " b.high_raw, b.low_raw, b.close_raw, b.volume, b.trade_value_raw, b.quality, b.run_type,"
                " b.fetched_at, MAX(b.fetched_at, s.updated_at), COALESCE(b.ready_at, b.fetched_at), b.revision"
                " FROM bar_r1 b JOIN series s ON s.series_id = b.series_id")
            c.execute(
                f"INSERT INTO bar_history(series_id, revision, date, {_COLS_R2}, superseded_at, reason)"
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
            c.execute("UPDATE meta SET value='r2' WHERE key='schema_version'")
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_from_r1', ?)",
                      (datetime.now().isoformat(timespec="seconds"),))

    # ── r2 → r3: 이전된 판의 시각 복원 + 정합성 이력 ─────────────
    def _upgrade_r2_to_r3(self) -> None:
        """A2 2차 재검토 #1·#2. 이미 r2로 이전된 DB도 여기서 보정합니다(한 트랜잭션, 한 번만).

        1. bar·bar_history에 time_basis 열 추가(기존 행 기본값 OBSERVED).
        2. r1에서 옮겨 온 판(series_revision.reason이 MIGRATED_R1…)은 r1이 저장 때마다 남긴 변경 기록으로 복원:
           - 활성 시각 = 그 revision을 만든 INIT/REBASE 기록 시각(전체 수집을 마치고 저장한 시각).
             첫 페이지 수신 시각(r1 fetched_at)이나 MIN(fetched_at)을 쓰지 않습니다.
           - 대체 시각 = 다음 revision의 활성 시각. 활성 시각이 엄격히 증가하지 않으면(구간 겹침) 두 판 모두 UNPROVEN.
           - 봉 available_at = max(활성 시각, 그 봉의 수신 시각 이후 첫 저장 기록 시각) → 실제 저장 전에는 쓸 수 없음.
           - 기록이 없어 입증할 수 없으면 time_basis=UNPROVEN — 시점 조회에서 돌려주지 않고 표시만 합니다.
        3. series_integrity(정합성 이력)를 VERIFY_FAILED·복구 기록에서 다시 만듭니다.
        """
        c = self.conn
        with self.tx():
            for table in ("bar", "bar_history"):
                cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
                if "time_basis" not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN time_basis TEXT NOT NULL DEFAULT 'OBSERVED'")
            for stmt in _SCHEMA_R3.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            migrated = [r[0] for r in c.execute(
                "SELECT DISTINCT series_id FROM series_revision WHERE reason LIKE 'MIGRATED_R1%' ORDER BY series_id")]
            for sid in migrated:
                self._repair_migrated_series(sid)
            for (sid,) in c.execute("SELECT series_id FROM series ORDER BY series_id").fetchall():
                self._rebuild_integrity_log(sid)
            c.execute("UPDATE meta SET value='r3' WHERE key='schema_version'")
            c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('upgraded_to_r3', ?)",
                      (datetime.now().isoformat(timespec="seconds"),))

    def _recheck_migrated_times(self, from_version: str) -> None:
        """r3·r4 → r5: 이전된 판의 MIGRATED·UNPROVEN 봉을 현재 근거 규칙(같은 초 + 판 귀속)으로 다시 계산.

        - r3: 수신 시각(초 올림)과 저장 기록(초 내림)을 그대로 비교해 같은 초 저장 봉을 UNPROVEN으로 남겼음(실측 199봉).
        - r4: revision 없는 APPEND 기록을 판 확인 없이 모든 판의 근거로 써서, 끝난 판의 봉을 다음 판 기록으로
              입증할 수 있었음(GPT B1). 이미 r4로 보정된 DB도 여기서 다시 점검 — 잘못 입증된 봉은 UNPROVEN으로.
        r3 뒤 새로 저장된 OBSERVED 봉은 건드리지 않습니다. 바뀐 봉 수는 meta(recheck_r5)와 upgrade_summary에 남깁니다."""
        tally: dict[str, int] = {}
        with self.tx():
            migrated = [r[0] for r in self.conn.execute(
                "SELECT DISTINCT series_id FROM series_revision WHERE reason LIKE 'MIGRATED_R1%' ORDER BY series_id")]
            for sid in migrated:
                for k, n in self._repair_migrated_series(sid, only_basis=(MIGRATED_TIME, UNPROVEN_TIME)).items():
                    tally[k] = tally.get(k, 0) + n
            summary = {"from": from_version, "at": datetime.now().isoformat(timespec="seconds"),
                       "series": len(migrated), **dict(sorted(tally.items()))}
            self.conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,))
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('recheck_r5', ?)",
                              (json.dumps(summary, ensure_ascii=False),))
        self.upgrade_summary = summary

    def _repair_migrated_series(self, sid: str, only_basis: tuple[str, ...] | None = None) -> dict[str, int]:
        """이전된 판(MIGRATED_R1…)의 활성·대체 시각과 봉 사용 가능 시각을 저장 기록으로 복원. 반환: 바뀐 봉 집계.

        only_basis를 주면 그 time_basis인 봉만 다시 계산(r3·r4 → r5: 이후 새로 저장된 OBSERVED 봉은 건드리지 않음).
        봉마다 `plan_migrated_bar`(점검 도구와 같은 함수): 같은 판으로 귀속된 저장 기록이 있으면 MIGRATED,
        사용 가능 시각 = max(판 활성, 수신, 저장 기록)(수신 시각을 포함하므로 실제 저장보다 이르지 않음).
        근거가 없거나 판 자체가 입증 안 되면 UNPROVEN — 일괄 해제하지 않음."""
        c = self.conn
        basis_cond, basis_args = "", ()
        if only_basis:
            basis_cond = f" AND time_basis IN ({','.join('?' * len(only_basis))})"
            basis_args = tuple(only_basis)
        tl, writes, reasons = load_series_evidence(c, sid)
        tally: dict[str, int] = {}
        for i, rv in enumerate(tl.order):
            sup = tl.superseded(rv)
            if not tl.migrated[rv]:
                if sup is not None:
                    c.execute("UPDATE series_revision SET superseded_at=? WHERE series_id=? AND revision=?",
                              (sup, sid, rv))
                continue
            base_reason = reasons[rv].split(":")[0]
            c.execute("UPDATE series_revision SET activated_at=?, superseded_at=?, reason=? WHERE series_id=? AND revision=?",
                      (tl.act[rv], sup, f"{base_reason}:{'EVENT' if tl.proven[rv] else 'UNPROVEN'}", sid, rv))
            for table in ("bar", "bar_history"):
                groups = c.execute(
                    f"SELECT received_at, time_basis, available_at, COUNT(*) FROM {table} WHERE series_id=? AND revision=?"
                    f"{basis_cond} GROUP BY received_at, time_basis, available_at", (sid, rv) + basis_args).fetchall()
                plans: dict[str, BarTimePlan] = {}
                for recv, old_basis, old_av, n in groups:
                    p = plans.setdefault(recv, plan_migrated_bar(tl, writes, rv, recv))
                    if old_basis != p.time_basis:
                        key = f"{old_basis}->{p.time_basis}"
                    elif p.time_basis == MIGRATED_TIME and old_av != p.available_at:
                        key = "MIGRATED_available_at_changed"
                    else:
                        key = "unchanged"
                    tally[key] = tally.get(key, 0) + n
                for recv, p in plans.items():
                    if p.time_basis == MIGRATED_TIME:
                        c.execute(f"UPDATE {table} SET available_at=?, time_basis=? WHERE series_id=? AND revision=?"
                                  f" AND received_at=?{basis_cond}",
                                  (p.available_at, MIGRATED_TIME, sid, rv, recv) + basis_args)
                    else:
                        c.execute(f"UPDATE {table} SET time_basis=? WHERE series_id=? AND revision=? AND received_at=?"
                                  f"{basis_cond}", (UNPROVEN_TIME, sid, rv, recv) + basis_args)
        return tally

    def _rebuild_integrity_log(self, sid: str) -> None:
        c = self.conn
        if c.execute("SELECT 1 FROM series_integrity WHERE series_id=? LIMIT 1", (sid,)).fetchone():
            return
        state = INTEGRITY_OK
        for r in c.execute("SELECT at, event, detail_json FROM series_event WHERE series_id=? ORDER BY at, event_id",
                           (sid,)).fetchall():
            if r["event"] == "VERIFY_FAILED" and state != REBASE_REQUIRED:
                state = REBASE_REQUIRED
                c.execute("INSERT INTO series_integrity(series_id, at, status, detail) VALUES(?,?,?,?)",
                          (sid, r["at"], state, f"REBUILT_FROM_EVENT:{r['detail_json'][:300]}"))
            elif r["event"] in ("INIT", "REBASE", "EXTEND", "VERIFIED") and state != INTEGRITY_OK:
                state = INTEGRITY_OK
                c.execute("INSERT INTO series_integrity(series_id, at, status, detail) VALUES(?,?,?,?)",
                          (sid, r["at"], state, f"REBUILT_FROM_EVENT:{r['event']}"))
        cur = c.execute("SELECT integrity, updated_at FROM series WHERE series_id=?", (sid,)).fetchone()
        if cur is not None and cur["integrity"] != state:          # 기록과 현재 상태가 다르면 현재 상태를 기록(방어)
            c.execute("INSERT INTO series_integrity(series_id, at, status, detail) VALUES(?,?,?,?)",
                      (sid, cur["updated_at"], cur["integrity"], "REBUILT_CURRENT_STATE"))

    def _log_integrity(self, series_id: str, at: datetime, status: str, detail: str) -> None:
        self.conn.execute("INSERT INTO series_integrity(series_id, at, status, detail) VALUES(?,?,?,?)",
                          (series_id, _ts(at), status, detail[:500]))

    def integrity_at(self, series_id: str, as_of: datetime) -> tuple[str, str]:
        """그 시각의 정합성 상태 (기록이 없으면 OK)."""
        r = self.conn.execute("SELECT status, detail FROM series_integrity WHERE series_id=? AND at<=?"
                              " ORDER BY at DESC, log_id DESC LIMIT 1", (series_id, _ts(as_of))).fetchone()
        return (r["status"], r["detail"]) if r else (INTEGRITY_OK, "")

    def integrity_log(self, series_id: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM series_integrity WHERE series_id=? ORDER BY at, log_id", (series_id,))]

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
                         _dt(r["first_ready_at"]), r["revision"], r["time_basis"])

    def load_bars(self, series_id: str, start: date | None = None) -> list[StoredBar]:
        q, a = f"SELECT date, {_COLS}, revision FROM bar WHERE series_id=?", [series_id]
        if start is not None:
            q, a = q + " AND date>=?", a + [start.isoformat()]
        return [self._stored(r) for r in self.conn.execute(q + " ORDER BY date", a)]

    @contextmanager
    def read_tx(self):
        """여러 조회를 한 시점으로 읽음(사이에 다른 프로세스의 REBASE가 끼지 않게). 이미 트랜잭션 안이면 그대로."""
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN")
        try:
            yield
        finally:
            self.conn.execute("COMMIT")

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

    def research_series(self, series_id: str, as_of: datetime | None = None, *,
                        start: date | None = None) -> ResearchSeries:
        """연구 계산 입력.

        as_of=None : 현재 revision 전체(가정 분석용). integrity = 현재 상태.
        as_of=X    : X에 활성이던 revision에서 available_at ≤ X인 봉만(A2-R1). integrity = **X 시각의 상태**
                     (정합성 이력 series_integrity — 나중에 복구돼도 실패 당시 조회는 REBASE_REQUIRED 그대로).
                     확보 시각을 입증하지 못한 봉(UNPROVEN)은 돌려주지 않고 time_proof로 표시.
        start      : 이 날짜 이후 봉만(스캔 속도용). time_proof는 start와 무관하게 그 revision 전체로 판정.
        메타·revision·봉은 한 읽기 트랜잭션에서 읽고, 봉은 revision 번호로 bar·bar_history를 함께 조회합니다
        (조회 도중 다른 프로세스가 REBASE해도 섞이거나 빠지지 않음).
        """
        with self.read_tx():
            return self._research_series(series_id, as_of, start)

    def _research_series(self, series_id: str, as_of: datetime | None, start: date | None) -> ResearchSeries:
        meta = self.get_series(series_id)
        if meta is None:
            raise KeyError(series_id)
        rev_info = None
        if as_of is None:
            mode = "CURRENT"
            stored, rev = self.load_bars(series_id, start), meta.revision
            integrity, integrity_detail = meta.integrity, meta.integrity_detail
            basis = AdjustmentBasis(meta.adj_upd_stkpc_tp, meta.adj_base_dt)
            unproven = self.conn.execute("SELECT COUNT(*) FROM bar WHERE series_id=? AND time_basis=?",
                                         (series_id, UNPROVEN_TIME)).fetchone()[0]
        else:
            mode = "AS_OF"
            x = _ts(as_of)
            r = self.conn.execute(
                "SELECT * FROM series_revision WHERE series_id=? AND activated_at<=? AND"
                " (superseded_at IS NULL OR superseded_at>?) ORDER BY revision DESC LIMIT 1",
                (series_id, x, x)).fetchone()
            if r is None:
                return ResearchSeries([], {}, {}, {}, None, None, mode, NO_REVISION, "그 시각에 활성 revision 없음",
                                      OBSERVED_TIME, 0, as_of, None, meta)
            rev_info = dict(r)
            rev, basis = r["revision"], AdjustmentBasis(r["upd_stkpc_tp"], r["base_dt"])
            integrity, integrity_detail = self.integrity_at(series_id, as_of)
            cond = " AND date>=?" if start is not None else ""
            one = (series_id, rev) + ((start.isoformat(),) if start is not None else ())
            rows = [self._stored(q) for q in self.conn.execute(
                f"SELECT date, {_COLS}, revision FROM bar WHERE series_id=? AND revision=?{cond}"
                f" UNION ALL SELECT date, {_COLS}, revision FROM bar_history WHERE series_id=? AND revision=?{cond}"
                " ORDER BY date", one + one)]
            unproven = self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM bar WHERE series_id=? AND revision=? AND time_basis=?)"
                " + (SELECT COUNT(*) FROM bar_history WHERE series_id=? AND revision=? AND time_basis=?)",
                (series_id, rev, UNPROVEN_TIME) * 2).fetchone()[0]
            limit = as_of.replace(microsecond=0)
            stored = [sb for sb in rows if sb.time_basis != UNPROVEN_TIME and sb.available_at <= limit]
        spec = meta.spec
        bars, avail, first, rtype = [], {}, {}, {}
        for sb in stored:
            rb = to_research_bar(sb.raw, spec)
            if rb is None:
                continue
            bars.append(rb)
            avail[rb.date], first[rb.date], rtype[rb.date] = sb.available_at, sb.first_ready_at, sb.run_type
        return ResearchSeries(bars, avail, first, rtype, rev, basis, mode, integrity, integrity_detail,
                              UNPROVEN_TIME if unproven else "OK", unproven, as_of, rev_info, meta)

    def snapshot_as_of(self, as_of: datetime) -> dict | None:
        """as_of까지 **수집이 끝난**(observed_end_at ≤ as_of) 가장 최근 종목 목록 스냅숏 (A4 — latest_snapshot 아님)."""
        row = self.conn.execute(
            "SELECT snapshot_id, snapshot_date, observed_at, observed_end_at, market_phase, policy_version, source,"
            " row_count, summary_json FROM universe_snapshot WHERE observed_end_at<=? ORDER BY observed_end_at DESC,"
            " snapshot_id DESC LIMIT 1", (_ts(as_of),)).fetchone()
        return None if row is None else {**dict(row), "summary": json.loads(row["summary_json"])}

    # ── 시계열 쓰기 ──────────────────────────────────────────
    def _insert_bars(self, series_id: str, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            f"INSERT INTO bar(series_id, date, {_COLS}, revision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(series_id, *r) for r in rows])

    @staticmethod
    def _row(fb: FetchedBar, run_type: str, available: datetime, first_ready: datetime, revision: int) -> tuple:
        b = fb.raw
        return (b.date.isoformat(), b.open_raw, b.high_raw, b.low_raw, b.close_raw, b.volume, b.trade_value_raw,
                b.quality, run_type, _ts_up(fb.received_at), _ts_up(available), _ts_up(first_ready), OBSERVED_TIME,
                revision)

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
            if meta and meta.integrity != INTEGRITY_OK:
                self._log_integrity(series_id, now, INTEGRITY_OK, f"RECOVERED_BY_INIT:{reason}")
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
                self._log_integrity(series_id, now, REBASE_REQUIRED, f"{reason}|{detail}")
                self._event(series_id, now, "VERIFY_FAILED", {"problems": problems, "reason": reason,
                                                              "candidate_basis": basis.__dict__,
                                                              "candidate_bars": len(candidate)})
                return {"action": "REBASE_FAILED", "revision": meta.revision, "problems": problems}
            if meta.integrity != INTEGRITY_OK:               # 재검증 통과 → 정합성 회복 기록
                self._log_integrity(series_id, now, INTEGRITY_OK, f"RECOVERED:{reason}")
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
            # revision을 적음 — 저장 근거를 판별로 확인할 수 있게 (GPT B1; r1~r4 기록에는 없었음)
            self._event(series_id, now, "APPEND" if bars else "VERIFIED",
                        {"revision": meta.revision, "added": len(bars), "verified_base_dt": verified_base_dt,
                         "scope": "FIRST_PAGE"})
        return len(bars)

    def mark_integrity(self, series_id: str, detail: str, now: datetime, status: str = REBASE_REQUIRED) -> None:
        """값을 바꾸지 않고 재검증 필요 상태만 기록 (재수집 조회 자체가 실패한 경우 등)."""
        with self.tx():
            self.conn.execute("UPDATE series SET integrity=?, integrity_detail=?, updated_at=? WHERE series_id=?",
                              (status, detail[:500], _ts(now), series_id))
            self._log_integrity(series_id, now, status, detail)
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
