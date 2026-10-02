from __future__ import annotations

"""A5-1: 다음 거래일 개장 + N분 가격 기록 (주문 없음, 조회만). GPT 재검토 `71b78e3` 지시.

흐름 (대상 거래일 D, 신호일 t = D의 직전 거래일)
1. 후보 확정(`candidate_set`·`candidate`, D마다 한 번): 대상 계약 하나(config/research.yaml s1.active_contract)의
   t 대표 기록 중 **PASS·final=1·actionable=1·스캔 시각 < D 개장**인 것만. signal_id·run_id·contract_hash·
   진입 상한·참고 손절가·신호일 종가를 저장하고, 이후 실행은 다시 고르지 않고 이 목록을 씁니다.
   - 같은 D에는 후보 목록이 하나뿐(대상 계약이 바뀌어도 이미 확정된 D의 목록·계약은 그대로, 새 계약은 다음 D부터)
     → 계약이 여러 개여도 같은 종목이 두 후보가 되지 않음.
   - 대상 계약의 완료된 스캔이 개장 전에 없으면 NO_SCAN, 스캔은 있었는데 후보가 없으면 NO_CANDIDATES로 확정.
2. 가격 확인(`price_check`, 후보·확인 종류마다 한 행 — 재시작해도 중복 없음, 이미 기록된 후보는 다시 조회하지 않음):
   - 목표 시각 = 달력의 D 개장 시각 + offset_min(일반 09:05, 특수 개장일은 그 개장 시각 기준). 목표 전에는 실행 거부.
   - 조회 시각이 목표 + on_time_tolerance_sec 이내면 ON_TIME, 넘으면 LATE — **실제 요청·수신 시각을 그대로 기록**하고
     09:05 가격으로 간주하지 않음. D 정규장 종료 뒤(또는 다음 날)면 조회하지 않고 MISSED. 일봉으로 채우지 않음.
   - 조회 실패(재시도 후에도)는 FETCH_FAILED, 응답에 필수 필드가 없으면 PARSE_FAILED — 그 확인의 결과로 남김.
3. 판정(outcome, 관찰 가격 기준 — **실제 체결 아님**):
   NOT_TRADABLE(현재가 없음·거래량 0·상한가) > BASIS_CHANGED(D 기준가 ≠ 신호일 종가 — 액면분할·권리락 등으로 가격
   기준이 달라져 진입 상한을 그대로 비교하지 않음) > ABOVE_CAP(관찰가 > 진입 상한) > BELOW_STOP(관찰가 ≤ 참고 손절가)
   > WITHIN_CAP. 가정 체결가격(assumed_fill_price)은 WITHIN_CAP일 때만 관찰가로 두고 규칙 이름을 함께 기록.

가격 원천 (2026-10-02 11:40 실측 `tools/probe_price_sources.py`로 확인)
- ka10001(주식기본정보, 판정 기준): cur_prc(현재가)·base_pric(기준가 = 전일 종가, pred_pre = 현재가 − 기준가)는 필수,
  open_pric·high_pric·low_pric·upl_pric·lst_pric·trde_qty는 있으면 기록. 응답에 가격 시각 필드는 없음.
- ka10003(체결정보, 보조): 최근 체결 목록 cntr_infr의 첫 행(가장 최근, stex_tp=KRX) tm(HHMMSS)·cur_prc →
  **원천 가격 시각**(source_time)·그 체결가. 실측: 요청보다 약 1초 앞선 체결. ka10001 바로 뒤에 조회.
- ka10004(주식호가, 보조): bid_req_base_tm(호가 기준 시각)·sel_fpr_bid(최우선 매도호가)·buy_fpr_bid(최우선 매수호가).
- 보조 조회는 판정·가정 체결가격에 쓰지 않는 기록용 — 실패해도 ka10001 판정은 그대로, 상태는 extra_json에.
저장: data/research/a5_checks.sqlite3(수집·관찰 DB와 별도 파일). 스키마 a2(a1 DB는 열 때 백업 후 열 추가).
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from infra.research.kiwoom_readonly import ResearchApiError
from infra.research.scan_store import ScanStore
from infra.research.store import ResearchStore, sqlite_backup
from utils.trading_calendar import TradingCalendar

A5_SCHEMA = "a2"
PRICE_API_ID = "ka10001"
TRADE_API_ID = "ka10003"            # 체결정보 — 원천 가격 시각
BOOK_API_ID = "ka10004"             # 주식호가 — 최우선 호가
EXTRA_COLUMNS = (("source_price", "INTEGER"), ("source_exchange", "TEXT"), ("source_lag_sec", "INTEGER"),
                 ("best_ask", "INTEGER"), ("best_bid", "INTEGER"), ("quote_time", "TEXT"), ("extra_json", "TEXT"))
SELECTION_RULE = "signal_date=D 직전 거래일 · contract=대상 계약 · PASS · final=1 · actionable=1 · scan_at < D 개장"
ASSUMED_FILL_RULE = "OBSERVED_PRICE_AT_CHECK(가정 — 실제 체결 아님)"
ON_TIME, LATE, MISSED = "ON_TIME", "LATE", "MISSED"
FETCHED, FETCH_FAILED, PARSE_FAILED, NOT_RUN = "OK", "FETCH_FAILED", "PARSE_FAILED", "NOT_RUN"
WITHIN_CAP, ABOVE_CAP, BELOW_STOP, BASIS_CHANGED, NOT_TRADABLE, NO_PRICE_OUTCOME = (
    "WITHIN_CAP", "ABOVE_CAP", "BELOW_STOP", "BASIS_CHANGED", "NOT_TRADABLE", "NO_PRICE")
OPTIONAL_FIELDS = {"open_price": "open_pric", "high_price": "high_pric", "low_price": "low_pric",
                   "upper_limit": "upl_pric", "lower_limit": "lst_pric", "volume": "trde_qty"}
SENSITIVE_MARKERS = ("token", "appkey", "app_key", "secret", "acnt", "account", "authorization")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_set(
  set_id TEXT PRIMARY KEY, target_day TEXT NOT NULL UNIQUE, signal_date TEXT NOT NULL, contract_hash TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, open_at TEXT NOT NULL, selected_at TEXT NOT NULL, rule TEXT NOT NULL,
  source_json TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candidate(
  set_id TEXT NOT NULL, symbol TEXT NOT NULL, signal_id TEXT NOT NULL, name TEXT, market TEXT, run_id TEXT NOT NULL,
  scan_at TEXT NOT NULL, contract_hash TEXT NOT NULL, input_hash TEXT NOT NULL, signal_close INTEGER,
  entry_cap REAL, stop_ref REAL, stock_revision INTEGER, base_dt TEXT, levels_json TEXT NOT NULL,
  PRIMARY KEY(set_id, symbol)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS price_check(
  set_id TEXT NOT NULL, symbol TEXT NOT NULL, check_kind TEXT NOT NULL, signal_id TEXT NOT NULL,
  target_at TEXT NOT NULL, requested_at TEXT, received_at TEXT, lateness_sec INTEGER, timing TEXT NOT NULL,
  fetch_status TEXT NOT NULL, attempts INTEGER NOT NULL, api_id TEXT, error TEXT NOT NULL,
  observed_price INTEGER, base_price INTEGER, open_price INTEGER, high_price INTEGER, low_price INTEGER,
  upper_limit INTEGER, lower_limit INTEGER, volume INTEGER, source_time TEXT, source_price INTEGER,
  source_exchange TEXT, source_lag_sec INTEGER, best_ask INTEGER, best_bid INTEGER, quote_time TEXT, extra_json TEXT,
  outcome TEXT NOT NULL, outcome_detail TEXT NOT NULL, gap_vs_close REAL, gap_vs_cap REAL,
  assumed_fill_price INTEGER, assumed_fill_rule TEXT, body_json TEXT, run_id TEXT NOT NULL, recorded_at TEXT NOT NULL,
  PRIMARY KEY(set_id, symbol, check_kind)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS check_run(
  run_id TEXT PRIMARY KEY, target_day TEXT NOT NULL, check_kind TEXT NOT NULL, set_id TEXT, active_contract TEXT,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
  counts_json TEXT NOT NULL DEFAULT '{}');
"""


class OpenCheckError(RuntimeError):
    """확인을 할 수 없음(목표 시각 전·달력 밖 등) — 아무것도 기록하지 않음."""


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(microsecond=0).isoformat(timespec="seconds")


def _abs_int(v) -> int | None:
    """키움 시세 문자열('+72500', '-218750', '0') → 부호를 뗀 정수. 비었거나 숫자가 아니면 None."""
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip().replace(",", "")
    if s[:1] in "+-":
        s = s[1:]
    return int(s) if s.isdigit() else None


def _redact(body):
    if isinstance(body, dict):
        return {k: ("***" if any(m in k.lower() for m in SENSITIVE_MARKERS) else _redact(v)) for k, v in body.items()}
    if isinstance(body, list):
        return [_redact(x) for x in body]
    return body


@dataclass(frozen=True)
class Quote:
    price: int
    base: int
    optional: dict            # open_price·…·volume (없으면 None)


def parse_quote(body: dict) -> Quote:
    """ka10001 응답 → Quote. 필수(cur_prc·base_pric)가 없거나 숫자가 아니면 ValueError(PARSE_FAILED)."""
    price, base = _abs_int(body.get("cur_prc")), _abs_int(body.get("base_pric"))
    missing = [k for k, v in (("cur_prc", price), ("base_pric", base)) if v is None]
    if missing:
        raise ValueError(f"필수 필드 없음·숫자 아님: {missing}")
    return Quote(price, base, {k: _abs_int(body.get(src)) for k, src in OPTIONAL_FIELDS.items()})


def _hms(day: date, v) -> datetime | None:
    s = str(v or "").strip()
    if len(s) != 6 or not s.isdigit():
        return None
    try:
        return datetime(day.year, day.month, day.day, int(s[:2]), int(s[2:4]), int(s[4:]))
    except ValueError:
        return None


def parse_trade(body: dict, day: date) -> dict:
    """ka10003 → 가장 최근 KRX 체결의 시각·가격. 없거나 형식이 틀리면 ValueError."""
    rows = body.get("cntr_infr")
    if not isinstance(rows, list) or not rows:
        raise ValueError("cntr_infr 체결 목록 없음")
    row = next((r for r in rows if isinstance(r, dict) and (r.get("stex_tp") or "KRX") == "KRX"), None)
    if row is None:
        raise ValueError("KRX 체결 행 없음")
    t, price = _hms(day, row.get("tm")), _abs_int(row.get("cur_prc"))
    if t is None or price is None:
        raise ValueError(f"체결 시각·가격 형식 오류: tm={row.get('tm')!r} cur_prc={row.get('cur_prc')!r}")
    return {"source_time": t, "source_price": price, "source_exchange": row.get("stex_tp") or "KRX",
            "first_row": row}


def parse_book(body: dict, day: date) -> dict:
    """ka10004 → 호가 기준 시각·최우선 매도/매수호가. 없으면 ValueError."""
    ask, bid, t = _abs_int(body.get("sel_fpr_bid")), _abs_int(body.get("buy_fpr_bid")), _hms(day, body.get("bid_req_base_tm"))
    if ask is None or bid is None or t is None:
        raise ValueError("최우선 호가·호가 기준 시각 없음")
    keys = ("bid_req_base_tm", "sel_fpr_bid", "sel_fpr_req", "buy_fpr_bid", "buy_fpr_req", "tot_sel_req", "tot_buy_req")
    return {"best_ask": ask, "best_bid": bid, "quote_time": t, "fields": {k: body.get(k) for k in keys}}


def evaluate(cand: dict, q: Quote) -> tuple[str, str]:
    """관찰 가격 판정 (outcome, 상세). 순서: 거래 불가 > 가격 기준 변경 > 상한 초과 > 손절가 이하 > 상한 이내."""
    if q.price <= 0:
        return NOT_TRADABLE, "NO_PRICE: 현재가 0"
    if q.optional.get("volume") == 0:
        return NOT_TRADABLE, "ZERO_VOLUME: 확인 시각 거래량 0(거래정지 가능)"
    if q.optional.get("upper_limit") and q.price >= q.optional["upper_limit"]:
        return NOT_TRADABLE, f"AT_UPPER_LIMIT: 현재가 {q.price} ≥ 상한가 {q.optional['upper_limit']}"
    if cand["signal_close"] is None:
        return BASIS_CHANGED, "SIGNAL_CLOSE_UNKNOWN: 신호일 종가를 확인할 수 없어 기준 비교 불가"
    if q.base != cand["signal_close"]:
        return BASIS_CHANGED, f"기준가 {q.base} ≠ 신호일 종가 {cand['signal_close']} — 진입 상한 비교 보류"
    if not cand["entry_cap"] or not cand["stop_ref"]:
        return NO_PRICE_OUTCOME, "LEVELS_MISSING: 진입 상한·손절가 없음"
    if q.price > cand["entry_cap"]:
        return ABOVE_CAP, f"현재가 {q.price} > 진입 상한 {cand['entry_cap']:.0f}"
    if q.price <= cand["stop_ref"]:
        return BELOW_STOP, f"현재가 {q.price} ≤ 참고 손절가 {cand['stop_ref']:.0f}"
    return WITHIN_CAP, f"현재가 {q.price} ≤ 진입 상한 {cand['entry_cap']:.0f}"


# ── 저장소 ──────────────────────────────────────────────────────
class OpenCheckStore:
    def __init__(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.backup_path: str | None = None
        cur = self.conn.execute("SELECT value FROM meta WHERE key='a5_schema'").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES('a5_schema', ?)", (A5_SCHEMA,))
        elif cur[0] == "a1":
            self.backup_path = sqlite_backup(self.conn, self.path, "a1")
            self._upgrade_a1_to_a2()
        elif cur[0] != A5_SCHEMA:
            raise RuntimeError(f"A5 기록 저장소 스키마 {cur[0]} ≠ {A5_SCHEMA}: {self.path}")

    def _upgrade_a1_to_a2(self) -> None:
        """보조 조회(체결 시각·호가) 열 추가. 기존 행은 비워 둠(그때 조회하지 않았음)."""
        with self.tx():
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(price_check)")}
            for name, typ in EXTRA_COLUMNS:
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE price_check ADD COLUMN {name} {typ}")
            self.conn.execute("UPDATE meta SET value=? WHERE key='a5_schema'", (A5_SCHEMA,))

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

    def get_set(self, day: date) -> dict | None:
        r = self.conn.execute("SELECT * FROM candidate_set WHERE target_day=?", (day.isoformat(),)).fetchone()
        return None if r is None else {**dict(r), "source": json.loads(r["source_json"])}

    def freeze_set(self, cset: dict, cands: list[dict]) -> dict:
        """후보 목록을 한 트랜잭션으로 확정. 이미 있으면(다른 실행이 먼저 확정) 그대로 돌려줌 — 다시 고르지 않음."""
        with self.tx():
            if self.conn.execute("SELECT 1 FROM candidate_set WHERE target_day=?", (cset["target_day"],)).fetchone():
                pass
            else:
                self.conn.execute(
                    "INSERT INTO candidate_set(set_id, target_day, signal_date, contract_hash, status, reason, open_at,"
                    " selected_at, rule, source_json, count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (cset["set_id"], cset["target_day"], cset["signal_date"], cset["contract_hash"], cset["status"],
                     cset["reason"], cset["open_at"], cset["selected_at"], SELECTION_RULE,
                     json.dumps(cset["source"], ensure_ascii=False), len(cands)))
                self.conn.executemany(
                    "INSERT INTO candidate(set_id, symbol, signal_id, name, market, run_id, scan_at, contract_hash,"
                    " input_hash, signal_close, entry_cap, stop_ref, stock_revision, base_dt, levels_json)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(cset["set_id"], c["symbol"], c["signal_id"], c["name"], c["market"], c["run_id"], c["scan_at"],
                      c["contract_hash"], c["input_hash"], c["signal_close"], c["entry_cap"], c["stop_ref"],
                      c["stock_revision"], c["base_dt"], json.dumps(c["levels"], ensure_ascii=False))
                     for c in cands])
        return self.get_set(date.fromisoformat(cset["target_day"]))

    def candidates(self, set_id: str) -> list[dict]:
        return [{**dict(r), "levels": json.loads(r["levels_json"])} for r in self.conn.execute(
            "SELECT * FROM candidate WHERE set_id=? ORDER BY symbol", (set_id,))]

    def checks(self, set_id: str, kind: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM price_check WHERE set_id=?", [set_id]
        if kind:
            q, a = q + " AND check_kind=?", a + [kind]
        return [dict(r) for r in self.conn.execute(q + " ORDER BY symbol", a)]

    def has_check(self, set_id: str, symbol: str, kind: str) -> bool:
        return self.conn.execute("SELECT 1 FROM price_check WHERE set_id=? AND symbol=? AND check_kind=?",
                                 (set_id, symbol, kind)).fetchone() is not None

    def record_check(self, row: dict) -> bool:
        """한 후보의 확인 결과를 한 트랜잭션으로. 이미 있으면 쓰지 않고 False(중복 없음)."""
        cols = list(row)
        with self.tx():
            cur = self.conn.execute(f"INSERT OR IGNORE INTO price_check({', '.join(cols)}) VALUES"
                                    f"({', '.join('?' * len(cols))})", [row[c] for c in cols])
        return cur.rowcount == 1

    def begin_run(self, run_id: str, day: date, kind: str, active_contract: str, started_at: datetime) -> list[str]:
        """끝나지 않은 이전 실행은 ABORTED로 정리하고 새 실행을 RUNNING으로."""
        with self.tx():
            stale = [r[0] for r in self.conn.execute("SELECT run_id FROM check_run WHERE status='RUNNING'")]
            self.conn.execute("UPDATE check_run SET status='ABORTED', finished_at=?, note='다음 실행 시작 시 정리'"
                              " WHERE status='RUNNING'", (_ts(started_at),))
            self.conn.execute("INSERT INTO check_run(run_id, target_day, check_kind, active_contract, started_at,"
                              " status) VALUES(?,?,?,?,?,'RUNNING')",
                              (run_id, day.isoformat(), kind, active_contract, _ts(started_at)))
        return stale

    def finish_run(self, run_id: str, *, set_id: str | None, status: str, counts: dict, note: str,
                   now: datetime) -> None:
        with self.tx():
            self.conn.execute("UPDATE check_run SET set_id=?, status=?, counts_json=?, note=?, finished_at=?"
                              " WHERE run_id=?", (set_id, status, json.dumps(counts, ensure_ascii=False), note,
                                                 _ts(now), run_id))

    def runs(self, day: date | None = None) -> list[dict]:
        q, a = "SELECT * FROM check_run", ()
        if day:
            q, a = q + " WHERE target_day=?", (day.isoformat(),)
        return [{**dict(r), "counts": json.loads(r["counts_json"])} for r in self.conn.execute(q + " ORDER BY started_at, run_id", a)]


# ── 후보 선택 ────────────────────────────────────────────────────
def select_candidates(sstore: ScanStore, rstore: ResearchStore, cal: TradingCalendar, day: date,
                      contract_hash: str) -> tuple[dict, list[dict]]:
    """D의 후보 (확정 전 계산). 같은 입력이면 언제 고르든 같음 — 개장 전 스캔·확정 기록만 쓰기 때문."""
    t = cal.previous_trading_day(day)
    open_at = datetime.combine(day, cal.session_times(day).open)
    open_s = _ts(open_at)
    runs = [r for r in sstore.runs(t.isoformat()) if r["status"] == "COMPLETE"
            and r.get("contract_hash") == contract_hash and r["scan_at"] < open_s]
    cset = {"set_id": f"a5_{day.isoformat()}", "target_day": day.isoformat(), "signal_date": t.isoformat(),
            "contract_hash": contract_hash, "open_at": open_s,
            "source": {"scan_db": sstore.path, "runs": [r["run_id"] for r in runs]}}
    if not runs:
        return {**cset, "status": "NO_SCAN", "reason": f"대상 계약 {contract_hash}의 {t} 신호 스캔이 {open_s} 전에 없음"}, []
    obs = [o for o in sstore.observations(t.isoformat(), contract_hash=contract_hash)
           if o["eligible_signal"] == "PASS" and o["final"] == 1 and o["actionable"] == 1 and o["scan_at"] < open_s]
    evals: dict[str, dict] = {}
    out = []
    for o in obs:
        if o["run_id"] not in evals:
            evals[o["run_id"]] = {e["symbol"]: e for e in sstore.evals(o["run_id"])}
        e = evals[o["run_id"]].get(o["symbol"])
        if e is None or e["result"] is None:
            continue
        lv = e["result"]["levels"]
        sig_close = None
        try:
            rs = rstore.research_series(f"STOCK:{o['symbol']}", as_of=datetime.fromisoformat(o["scan_at"]), start=t)
            bar = next((b for b in rs.bars if b.date == t), None)
            sig_close = None if bar is None else int(round(bar.close))
        except KeyError:
            pass
        out.append({"symbol": o["symbol"], "signal_id": o["signal_id"], "name": e["name"], "market": e["market"],
                    "run_id": o["run_id"], "scan_at": o["scan_at"], "contract_hash": contract_hash,
                    "input_hash": o["input_hash"], "signal_close": sig_close, "entry_cap": lv.get("entry_cap"),
                    "stop_ref": lv.get("stop_ref"), "stock_revision": e["evidence"]["stock"].get("revision"),
                    "base_dt": e["evidence"]["stock"].get("base_dt"), "levels": lv})
    status = "OK" if out else "NO_CANDIDATES"
    return {**cset, "status": status, "reason": f"후보 {len(out)}건 (스캔 {len(runs)}회)"}, out


# ── 확인 실행 ────────────────────────────────────────────────────
class OpenChecker:
    def __init__(self, ostore: OpenCheckStore, sstore: ScanStore, rstore: ResearchStore, cal: TradingCalendar, *,
                 contract_hash: str, offset_min: int = 5, on_time_tolerance_sec: int = 120,
                 log: Callable[[str], None] | None = None) -> None:
        self.ostore, self.sstore, self.rstore, self.cal = ostore, sstore, rstore, cal
        self.contract_hash = contract_hash
        self.offset = timedelta(minutes=offset_min)
        self.tolerance = timedelta(seconds=on_time_tolerance_sec)
        self.kind = f"OPEN+{offset_min}m"
        self.log = log or (lambda m: None)

    def target_at(self, day: date) -> datetime:
        st = self.cal.session_times(day)
        if st is None:
            raise OpenCheckError(f"{day}는 거래일이 아님")
        return datetime.combine(day, st.open) + self.offset

    def close_at(self, day: date) -> datetime:
        return datetime.combine(day, self.cal.session_times(day).close)

    def run(self, day: date, *, client, now: Callable[[], datetime]) -> dict:
        """D의 가격 확인. 목표 시각 전이면 OpenCheckError(아무것도 기록 안 함). client는 필요할 때만 씀
        (모두 MISSED이거나 후보가 없으면 조회 0회)."""
        target = self.target_at(day)
        started = now()
        if started < target:
            raise OpenCheckError(f"목표 시각 {target} 전({started}) — 일찍 조회한 가격을 개장 + N분 가격으로 기록하지 않음")
        run_id = f"a5_{day:%Y%m%d}_{started:%Y%m%d%H%M%S}"
        for rid in self.ostore.begin_run(run_id, day, self.kind, self.contract_hash, started):
            self.log(f"[A5] 끝나지 않은 이전 실행 {rid} → ABORTED")
        try:
            cset = self.ostore.get_set(day)
            if cset is None:
                draft, cands = select_candidates(self.sstore, self.rstore, self.cal, day, self.contract_hash)
                cset = self.ostore.freeze_set({**draft, "selected_at": _ts(started)}, cands)
                self.log(f"[A5] {day} 후보 확정: {cset['status']} {cset['count']}건 (계약 {cset['contract_hash']})")
            note = ""
            if cset["contract_hash"] != self.contract_hash:
                note = (f"CONTRACT_CHANGED: 대상 계약 {self.contract_hash}이지만 {day} 후보는 이미 계약 "
                        f"{cset['contract_hash']}로 확정 — 확정된 목록을 그대로 씀(섞지 않음)")
                self.log(f"[A5] {note}")
            close = self.close_at(day)
            for c in self.ostore.candidates(cset["set_id"]):
                if self.ostore.has_check(cset["set_id"], c["symbol"], self.kind):
                    continue
                self.ostore.record_check(self._check_one(cset, c, target, close, client, now, run_id))
            checks = self.ostore.checks(cset["set_id"], self.kind)
            counts = {"candidates": cset["count"], "checked": len(checks),
                      "timing": _tally(checks, "timing"), "fetch": _tally(checks, "fetch_status"),
                      "outcome": _tally(checks, "outcome")}
            self.ostore.finish_run(run_id, set_id=cset["set_id"], status="COMPLETE", counts=counts, note=note,
                                   now=now())
        except BaseException as exc:
            self.ostore.finish_run(run_id, set_id=None, status="FAILED", counts={},
                                   note=f"{type(exc).__name__}: {str(exc)[:300]}", now=now())
            raise
        return {"run_id": run_id, "set": cset, "kind": self.kind, "target_at": _ts(target), "counts": counts,
                "note": note, "checks": checks, "candidates": self.ostore.candidates(cset["set_id"])}

    def _check_one(self, cset: dict, c: dict, target: datetime, close: datetime, client, now, run_id) -> dict:
        base = {"set_id": cset["set_id"], "symbol": c["symbol"], "check_kind": self.kind, "signal_id": c["signal_id"],
                "target_at": _ts(target), "requested_at": None, "received_at": None, "lateness_sec": None,
                "attempts": 0, "api_id": None, "error": "", "observed_price": None, "base_price": None,
                **{k: None for k in OPTIONAL_FIELDS}, "source_time": None, **{k: None for k, _ in EXTRA_COLUMNS},
                "gap_vs_close": None, "gap_vs_cap": None,
                "assumed_fill_price": None, "assumed_fill_rule": None, "body_json": None, "run_id": run_id}
        ts = now()
        if ts >= close or ts.date() > target.date():
            # 정규장이 끝난 뒤 — 이 시각 가격은 개장 + N분 관찰이 아님. 조회하지 않고 누락으로(일봉으로 채우지 않음)
            return {**base, "timing": MISSED, "fetch_status": NOT_RUN, "outcome": MISSED,
                    "outcome_detail": f"확인 시각 {_ts(ts)} — D 정규장 종료({_ts(close)}) 뒤라 조회하지 않음",
                    "recorded_at": _ts(ts)}
        try:
            b = client.fetch_body(PRICE_API_ID, {"stk_cd": c["symbol"]})
        except ResearchApiError as exc:
            t2 = now()
            late = int((ts - target).total_seconds())
            return {**base, "requested_at": _ts(ts), "lateness_sec": late, "attempts": 1, "api_id": PRICE_API_ID,
                    "timing": ON_TIME if ts - target <= self.tolerance else LATE, "fetch_status": FETCH_FAILED,
                    "error": str(exc)[:300], "outcome": FETCH_FAILED, "outcome_detail": "조회 실패(재시도 후)",
                    "recorded_at": _ts(t2)}
        late = int((b.requested_at - target).total_seconds())
        row = {**base, "requested_at": _ts(b.requested_at), "received_at": _ts(b.received_at), "lateness_sec": late,
               "attempts": b.attempts, "api_id": PRICE_API_ID,
               "timing": ON_TIME if b.requested_at - target <= self.tolerance else LATE,
               "body_json": json.dumps(_redact(b.body), ensure_ascii=False), "recorded_at": _ts(now())}
        try:
            q = parse_quote(b.body)
        except ValueError as exc:
            return {**row, "fetch_status": PARSE_FAILED, "error": str(exc)[:300], "outcome": PARSE_FAILED,
                    "outcome_detail": "응답에 필수 가격 필드 없음"}
        outcome, detail = evaluate(c, q)
        row.update(self._extras(c["symbol"], target.date(), client, now))
        return {**row, "fetch_status": FETCHED, "observed_price": q.price, "base_price": q.base, **q.optional,
                "outcome": outcome, "outcome_detail": detail,
                "gap_vs_close": None if not c["signal_close"] else round(q.price / c["signal_close"] - 1, 6),
                "gap_vs_cap": None if not c["entry_cap"] else round(q.price / c["entry_cap"] - 1, 6),
                "assumed_fill_price": q.price if outcome == WITHIN_CAP else None,
                "assumed_fill_rule": ASSUMED_FILL_RULE if outcome == WITHIN_CAP else None}


    def _extras(self, symbol: str, day: date, client, now) -> dict:
        """보조 조회(기록용): ka10003 최근 체결 시각·가격, ka10004 최우선 호가. 실패해도 판정에 영향 없음."""
        out: dict = {"recorded_at": _ts(now())}
        info: dict = {}
        for api_id, parse in ((TRADE_API_ID, parse_trade), (BOOK_API_ID, parse_book)):
            try:
                b = client.fetch_body(api_id, {"stk_cd": symbol})
            except ResearchApiError as exc:
                info[api_id] = {"status": FETCH_FAILED, "error": str(exc)[:200]}
                continue
            entry = {"status": FETCHED, "requested_at": _ts(b.requested_at), "received_at": _ts(b.received_at),
                     "attempts": b.attempts}
            try:
                p = parse(b.body, day)
            except ValueError as exc:
                info[api_id] = {**entry, "status": PARSE_FAILED, "error": str(exc)[:200]}
                continue
            if api_id == TRADE_API_ID:
                out.update(source_time=_ts(p["source_time"]), source_price=p["source_price"],
                           source_exchange=p["source_exchange"],
                           source_lag_sec=int((b.requested_at - p["source_time"]).total_seconds()))
                entry["first_row"] = _redact(p["first_row"])
            else:
                out.update(best_ask=p["best_ask"], best_bid=p["best_bid"], quote_time=_ts(p["quote_time"]))
                entry["fields"] = p["fields"]
            info[api_id] = entry
        out["extra_json"] = json.dumps(info, ensure_ascii=False)
        out["recorded_at"] = _ts(now())
        return out


def _tally(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + 1
    return dict(sorted(out.items()))


# ── 보고서 ──────────────────────────────────────────────────────
NOTE = ("관찰 가격 기록입니다 — 실제 체결이 아니며 수익으로 해석하지 않습니다. 가정 체결가격은 상한 이내일 때 관찰가를 "
        "그대로 둔 분석용 값입니다.")


def build_markdown(res: dict) -> str:
    s, c = res["set"], res["counts"]
    lines = [f"# A5-1 개장 가격 기록 — 대상일 {s['target_day']} (신호일 {s['signal_date']})", "",
             f"- 확인 `{res['kind']}` 목표 시각 {res['target_at']} · 후보 확정 {s['selected_at']} · 계약 `c:{s['contract_hash']}`",
             f"- 후보: {s['status']} — {s['reason']}",
             f"- 시각: {c['timing']} · 조회: {c['fetch']} · 판정: {c['outcome']}"]
    if res["note"]:
        lines.append(f"- 주의: {res['note']}")
    lines += ["", f"> {NOTE}", ""]
    cand = {x["symbol"]: x for x in res["candidates"]}
    rows = []
    for k in res["checks"]:
        x = cand.get(k["symbol"], {})
        rows.append([k["symbol"], x.get("name") or "", _n(k["observed_price"]), _n(k["base_price"]),
                     _n(x.get("signal_close")), _n(x.get("entry_cap")), _n(x.get("stop_ref")),
                     "-" if k["gap_vs_cap"] is None else f"{k['gap_vs_cap'] * 100:+.2f}%",
                     k["outcome"], k["timing"], k["requested_at"] or "-",
                     "-" if k["lateness_sec"] is None else str(k["lateness_sec"]),
                     (k.get("source_time") or "-")[11:] or "-", _n(k.get("best_ask"))])
    if rows:
        head = ["종목", "이름", "관찰가", "기준가", "신호일 종가", "진입 상한", "참고 손절가", "상한 대비", "판정", "시각",
                "요청 시각", "지연(초)", "최근 체결 시각", "최우선 매도호가"]
        lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(r) + " |" for r in rows]
    else:
        lines.append("확인한 후보 없음")
    return "\n".join(lines) + "\n"


def _n(v) -> str:
    return "-" if v is None else f"{v:,.0f}"


def write_report(res: dict, out_dir: str | Path) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = out / f"a5_open_{res['set']['target_day']}"
    md, js = base.with_suffix(".md"), base.with_suffix(".json")
    payload = {k: res[k] for k in ("run_id", "set", "kind", "target_at", "counts", "note", "candidates")}
    payload["checks"] = [{k: v for k, v in r.items() if k != "body_json"} for r in res["checks"]]
    payload["note_text"] = NOTE
    for path, text in ((md, build_markdown(res)), (js, json.dumps(payload, ensure_ascii=False, indent=2, default=str))):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return str(md)
