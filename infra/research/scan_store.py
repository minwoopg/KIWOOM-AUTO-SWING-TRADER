from __future__ import annotations

"""S1 관찰 기록 저장소 (A4-A, SQLite — 기본 data/research/s1_scans.sqlite3, git 제외). 스키마 s2.

수집 저장소(research.sqlite3)와 파일을 나눕니다 — 수집 DB를 다시 만들거나 이전해도 관찰 기록은 그대로 남습니다.

테이블
- scan_run       : 스캔 한 번. run_key = (신호일 | 스캔 시각 | 전략 | 설정 해시 | 분류 정책). 같은 run_key의
                   COMPLETE는 하나뿐(부분 유일 인덱스) → 같은 입력·스캔 시각으로 다시 돌려도 중복 저장 없음.
                   상태 RUNNING → COMPLETE / FAILED / ABORTED. 시작할 때 남아 있던 RUNNING은 ABORTED로 정리.
- s1_eval        : 그 실행의 종목별 판정 전부(후보·탈락·보류). 실행마다 쌓이고 고치지 않음(append-only).
                   결과 본문(S1Result, 시장 판정은 실행 context로 분리)·종목별 증거(revision·조정 기준·위험 표시)는
                   사전(zdict) 압축 JSON — 사전은 codec 표에 함께 저장. 입력 해시는 열로.
                   실행 공통 증거(지수 revision·스냅숏·세션·버전·스캔 시각)는 scan_run.context에 한 번만.
- s1_observation : 신호 ID = 전략·설정 해시·종목·신호일(**입력 해시는 넣지 않음**)마다 대표 판정 하나.
                   final(`final_rule`: 데이터·지수·스냅숏 모두 정상이고 판정이 PASS/FAIL로 정해진 실행의 판정)은
                   **절대 바꾸지 않음** — 이후 정정 데이터나 새 스냅숏이 들어와도 그대로. final이 아닌 기록(보류·UNKNOWN)만
                   더 늦은 스캔 시각의 실행이 대체(이력 보존).
- obs_audit      : 대표 기록을 실행이 아닌 이유로 고친 이력(s2). 바꾸기 전·후 값과 사유.

기존 DB 이전 (열 때 자동, 바꾸기 전에 같은 폴더에 백업 `<파일>.bak-s1-<시각>`)
- s1 → s2 (GPT B2): 이전 버전(f837185)은 입력만 정상이면 UNKNOWN 판정도 final=1로 고정했습니다. 대표 기록 중
  현재 규칙(`final_rule`)을 만족하지 않는 final=1을 final=0으로 풀고 obs_audit에 남깁니다 → 이후 정해진 판정으로
  대체될 수 있음. PASS·FAIL로 확정된 대표 기록은 그대로. 실행·종목별 판정(scan_run·s1_eval)은 당시 그대로 보존.

원자성: 종목별 판정·대표 기록·COMPLETE 표시를 한 트랜잭션에 씁니다. 도중에 끊기면 아무것도 남지 않고 실행은
RUNNING으로 남아 다음 실행이 ABORTED로 정리합니다. COMPLETE 표시는 `status='RUNNING'`인 행에만 하므로,
다른 실행이 이미 ABORTED로 정리한 실행은 완료로 표시되지 않고 되돌려집니다.
"""

import hashlib
import json
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from infra.research.store import sqlite_backup

SCAN_SCHEMA = "s2"
RUNNING, COMPLETE, FAILED, ABORTED = "RUNNING", "COMPLETE", "FAILED", "ABORTED"
FINAL_RULE = "inputs_ok+decided"       # 실행 context에 기록 — 이 규칙 전(f837185) 실행과 구분


def final_rule(data_status: str, index_status: str, snapshot_status: str, eligible_signal: str) -> int:
    """확정(final): 종목·지수·스냅숏 입력이 모두 정상이고 판정이 PASS/FAIL로 정해진 경우만 (GPT R3·B2).
    UNKNOWN(거래 없는 봉·이력 부족 등)은 이후 실행의 정해진 판정으로 대체될 수 있어야 함."""
    return int(data_status == "OK" and index_status == "OK" and snapshot_status == "OK"
               and eligible_signal in ("PASS", "FAIL"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scan_run(
  run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL, attempt INTEGER NOT NULL, scan_at TEXT NOT NULL,
  signal_date TEXT NOT NULL, strategy TEXT NOT NULL, config_hash TEXT NOT NULL, universe_policy TEXT NOT NULL,
  feature_version TEXT NOT NULL, market_version TEXT NOT NULL, after_close_min INTEGER NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, snapshot_id INTEGER, snapshot_observed_at TEXT,
  snapshot_status TEXT, context_json TEXT NOT NULL DEFAULT '{}', counts_json TEXT NOT NULL DEFAULT '{}',
  report_path TEXT, error TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS ux_run_complete ON scan_run(run_key) WHERE status='COMPLETE';
CREATE INDEX IF NOT EXISTS ix_run_signal_date ON scan_run(signal_date, scan_at);
CREATE TABLE IF NOT EXISTS s1_eval(
  run_id TEXT NOT NULL, symbol TEXT NOT NULL, name TEXT NOT NULL, market TEXT, series_id TEXT NOT NULL,
  signal_date TEXT NOT NULL, eligible_signal TEXT NOT NULL, pattern_pass TEXT NOT NULL, eligibility_pass TEXT NOT NULL,
  market_pass TEXT NOT NULL, stop_valid TEXT NOT NULL, data_status TEXT NOT NULL, index_status TEXT NOT NULL,
  snapshot_status TEXT NOT NULL, no_trades_hold INTEGER NOT NULL, final INTEGER NOT NULL, actionable INTEGER,
  input_hash TEXT NOT NULL, codec TEXT NOT NULL, result_z BLOB, evidence_z BLOB NOT NULL,
  UNIQUE(run_id, symbol));
CREATE TABLE IF NOT EXISTS s1_observation(
  signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, signal_date TEXT NOT NULL, strategy TEXT NOT NULL,
  config_hash TEXT NOT NULL, run_id TEXT NOT NULL, scan_at TEXT NOT NULL, eligible_signal TEXT NOT NULL,
  data_status TEXT NOT NULL, input_hash TEXT NOT NULL, final INTEGER NOT NULL, actionable INTEGER,
  first_run_id TEXT NOT NULL, replaced_count INTEGER NOT NULL, history_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_obs_date ON s1_observation(signal_date, eligible_signal);
CREATE TABLE IF NOT EXISTS codec(name TEXT PRIMARY KEY, zdict BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS obs_audit(
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL,
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, reason TEXT NOT NULL);
"""

# 판정 본문 압축용 사전(zdict) — 결과 JSON의 키·조건 이름을 미리 알려 행마다 압축률을 높입니다(약 1/4).
# 사전은 DB의 codec 표에 함께 저장하므로, 코드의 이 템플릿이 나중에 바뀌어도 기존 행은 그대로 풀립니다.
CODEC = "zd1"
_CHECKS = ("HISTORY", "C_GT_MA120", "MA60_RISING", "RS60_POS", "PULLBACK", "CLOSE_GT_PREV_HIGH", "CLOSE_GE_MA20",
           "ATR_POS", "NOT_EXTENDED", "SECURITY_TYPE", "RISK_STATUS", "LIQUIDITY_TV20", "MARKET_REGIME", "STOP_VALID")
_TEMPLATE = {
    "checks": [{"detail": d, "name": n, "result": r, "value": 0.0} for n in _CHECKS for r, d in
               (("PASS", ""), ("FAIL", "MA60_BREAK:"), ("UNKNOWN", "NO_TRADES:"), ("UNKNOWN", "INSUFFICIENT_HISTORY"))],
    "config_hash": "", "eligibility_pass": "PASS", "eligible_signal": "UNKNOWN", "feature_version": "f2",
    "levels": {"atr14": 0.0, "entry_cap": 0.0, "risk_ratio_at_cap": 0.0, "stop_ref": 0.0, "stop_status": "OK",
               "reason": ""},
    "market_pass": "FAIL", "observations": {"close_location": 0.0, "extension20": 0.0, "ma120_distance_atr": 0.0,
                                            "ma60_slope5": 0.0, "pivot_is_60d_high": 0.0, "ret20": 0.0, "ret60": 0.0,
                                            "rs20": 0.0, "rs60": 0.0, "tv20": 0.0, "tv20_reason": "",
                                            "volume_ratio": 0.0},
    "pattern_pass": "FAIL", "pullback": {"avg_range_pullback": 0.0, "avg_volume_pullback": 0.0, "depth": 0.0,
                                         "depth_atr": 0.0, "pivot_close": 0.0, "pivot_date": "2026-01-01",
                                         "pivot_high": 0.0, "pullback_end": "2026-01-01", "pullback_len": 0,
                                         "pullback_low": 0.0, "pullback_start": "2026-01-01"},
    "signal_date": "2026-01-01", "stop_valid": "PASS", "strategy": "s1_pullback_v0.1", "symbol": "000000",
    "stock": {"base_dt": "", "first_bar": "", "integrity": "OK", "integrity_detail": "", "last_available_at": "",
              "last_bar": "", "revision": 1, "time_proof": "OK", "upd_stkpc_tp": "1"},
    "index_id": "INDEX:KOSPI:001", "index_revision": 1, "risk_flags": [], "security_type": "COMMON", "snapshot_id": 1,
}
ZDICT_V1 = json.dumps(_TEMPLATE, ensure_ascii=False, sort_keys=True).encode("utf-8")

EVAL_FIELDS = ("symbol", "name", "market", "series_id", "signal_date", "eligible_signal", "pattern_pass",
               "eligibility_pass", "market_pass", "stop_valid", "data_status", "index_status", "snapshot_status",
               "no_trades_hold", "final", "actionable", "input_hash")


class ScanAbortedError(RuntimeError):
    """이 실행이 이미 ABORTED로 정리됨 — 완료로 표시하지 않음."""


def _ts(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(microsecond=0).isoformat(timespec="seconds")


def signal_id(strategy: str, config_hash: str, symbol: str, signal_date: str) -> str:
    return f"S1|{strategy}|{config_hash}|{symbol}|{signal_date}"


_RESET_SELECT = (
    "SELECT o.signal_id, o.run_id, o.scan_at, o.eligible_signal, o.data_status, e.index_status, e.snapshot_status"
    " FROM s1_observation o LEFT JOIN s1_eval e ON e.run_id = o.run_id AND e.symbol = o.symbol WHERE o.final = 1"
    " ORDER BY o.signal_id")


def _needs_reset(r) -> bool:
    """대표 기록의 final=1이 현재 규칙을 만족하지 않음(실행 판정 행이 없으면 입력 상태를 알 수 없으므로 풂)."""
    return r["index_status"] is None or not final_rule(r["data_status"], r["index_status"], r["snapshot_status"],
                                                       r["eligible_signal"])


def inspect_final_reset(path: str | Path) -> dict:
    """읽기 전용: 다음 열기(s1 → s2) 때 final을 풀 대표 기록 수 — DB를 바꾸지 않음."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT value FROM meta WHERE key='scan_schema'").fetchone()
        rows = conn.execute(_RESET_SELECT).fetchall()
        reset = [r for r in rows if _needs_reset(r)]
        by_signal: dict[str, int] = {}
        for r in reset:
            by_signal[r["eligible_signal"]] = by_signal.get(r["eligible_signal"], 0) + 1
        return {"scan_schema": cur[0] if cur else None, "final_observations": len(rows),
                "final_to_reset": len(reset), "by_signal": by_signal,
                "note": "s1이면 다음 열기 때 이 기록들의 final을 풂(감사 이력 남김). s2면 이미 끝남 — 0이어야 정상."}
    finally:
        conn.close()


class ScanStore:
    def __init__(self, path: str | Path, *, backup_before_upgrade: bool = True) -> None:
        """기존 DB의 스키마를 올려야 하면, 바꾸기 전에 같은 폴더에 백업(<파일>.bak-<옛 버전>-<시각>)을 만듭니다."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(p)
        self.backup_path: str | None = None
        self.upgrade_summary: dict | None = None
        self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='scan_schema'").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES('scan_schema', ?)", (SCAN_SCHEMA,))
        elif cur[0] == "s1":
            if backup_before_upgrade:
                self.backup_path = sqlite_backup(self.conn, self.path, "s1")
            self._upgrade_s1_to_s2()
        elif cur[0] != SCAN_SCHEMA:
            raise RuntimeError(f"관찰 저장소 스키마 {cur[0]} ≠ {SCAN_SCHEMA}: {self.path}")
        self.conn.execute("INSERT OR IGNORE INTO codec(name, zdict) VALUES(?, ?)", (CODEC, ZDICT_V1))
        self._zdict = {r[0]: bytes(r[1]) for r in self.conn.execute("SELECT name, zdict FROM codec")}

    def _upgrade_s1_to_s2(self) -> None:
        """GPT B2: 이전 버전이 고정한 'UNKNOWN + final=1' 대표 기록을 final=0으로 (감사 이력과 함께, 한 트랜잭션).

        현재 규칙(`final_rule`)을 대표 기록의 실행 판정 행(s1_eval)에 다시 적용해 만족하지 않는 것만 풉니다.
        PASS·FAIL로 확정된 기록은 그대로. scan_run·s1_eval(당시 실행 기록)은 바꾸지 않습니다."""
        now = _ts(datetime.now())
        reason = "S2_FINAL_RULE: 입력 정상 + 판정 PASS/FAIL만 확정 — 이전 버전(f837185)이 고정한 보류 판정을 풂 (GPT B2)"
        with self.tx():
            rows = self.conn.execute(_RESET_SELECT).fetchall()
            reset = [r for r in rows if _needs_reset(r)]
            for r in reset:
                before = {k: r[k] for k in ("run_id", "scan_at", "eligible_signal", "data_status", "index_status",
                                             "snapshot_status")} | {"final": 1}
                self.conn.execute("INSERT INTO obs_audit(signal_id, at, action, before_json, after_json, reason)"
                                  " VALUES(?,?,?,?,?,?)",
                                  (r["signal_id"], now, "FINAL_RESET", json.dumps(before, ensure_ascii=False),
                                   json.dumps({"final": 0}), reason))
                self.conn.execute("UPDATE s1_observation SET final=0, updated_at=? WHERE signal_id=?",
                                  (now, r["signal_id"]))
            summary = {"from": "s1", "at": now, "final_observations": len(rows), "final_reset": len(reset)}
            self.conn.execute("UPDATE meta SET value=? WHERE key='scan_schema'", (SCAN_SCHEMA,))
            self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('upgraded_to_s2', ?)",
                              (json.dumps(summary, ensure_ascii=False),))
        self.upgrade_summary = summary

    def audit(self, signal_id: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM obs_audit", ()
        if signal_id:
            q, a = q + " WHERE signal_id=?", (signal_id,)
        return [{**dict(r), "before": json.loads(r["before_json"]), "after": json.loads(r["after_json"])}
                for r in self.conn.execute(q + " ORDER BY audit_id", a)]

    def _pack(self, obj) -> bytes:
        co = zlib.compressobj(9, zdict=self._zdict[CODEC])
        return co.compress(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")) + co.flush()

    def _unpack(self, blob: bytes, codec: str):
        do = zlib.decompressobj(zdict=self._zdict[codec])
        return json.loads((do.decompress(blob) + do.flush()).decode("utf-8"))

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

    # ── 실행 ────────────────────────────────────────────────
    def complete_run(self, run_key: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM scan_run WHERE run_key=? AND status=?", (run_key, COMPLETE)).fetchone()
        return None if r is None else self._run_dict(r)

    def begin_run(self, *, run_key: str, scan_at: datetime, signal_date: str, strategy: str, config_hash: str,
                  universe_policy: str, feature_version: str, market_version: str, after_close_min: int,
                  started_at: datetime) -> tuple[str, list[str]]:
        """(run_id, 정리한 이전 RUNNING 실행 목록). 이미 COMPLETE면 ValueError — 호출 전에 complete_run으로 확인."""
        with self.tx():
            if self.conn.execute("SELECT 1 FROM scan_run WHERE run_key=? AND status=?", (run_key, COMPLETE)).fetchone():
                raise ValueError(f"이미 완료된 실행: {run_key}")
            stale = [r[0] for r in self.conn.execute("SELECT run_id FROM scan_run WHERE status=?", (RUNNING,))]
            for rid in stale:
                self.conn.execute("UPDATE scan_run SET status=?, finished_at=?, error=? WHERE run_id=?",
                                  (ABORTED, _ts(started_at), f"다음 실행 시작 시 RUNNING으로 남아 있어 정리", rid))
            attempt = self.conn.execute("SELECT COUNT(*) FROM scan_run WHERE run_key=?", (run_key,)).fetchone()[0] + 1
            # 실행 ID는 run_key 전체의 해시를 포함 — 분류 정책 등 run_key만 다른 실행이 같은 ID가 되지 않게 (GPT R5)
            key_hash = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:10]
            run_id = f"s1_{signal_date.replace('-', '')}_{scan_at:%Y%m%d%H%M%S}_{key_hash}_a{attempt}"
            self.conn.execute(
                "INSERT INTO scan_run(run_id, run_key, attempt, scan_at, signal_date, strategy, config_hash,"
                " universe_policy, feature_version, market_version, after_close_min, started_at, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, run_key, attempt, _ts(scan_at), signal_date, strategy, config_hash, universe_policy,
                 feature_version, market_version, after_close_min, _ts(started_at), RUNNING))
        return run_id, stale

    def fail_run(self, run_id: str, error: str, now: datetime, status: str = FAILED) -> None:
        with self.tx():
            self.conn.execute("UPDATE scan_run SET status=?, finished_at=?, error=? WHERE run_id=? AND status=?",
                              (status, _ts(now), error[:1000], run_id, RUNNING))

    def finish_run(self, *, run_id: str, evals: list[dict], context: dict, counts: dict, snapshot: dict | None,
                   snapshot_status: str, scan_at: datetime, now: datetime, strategy: str, config_hash: str,
                   before_commit=None) -> dict:
        """판정·대표 기록·COMPLETE를 한 트랜잭션에. 반환: 대표 기록 변화 집계."""
        obs_tally = {"new": 0, "kept_final": 0, "replaced": 0, "kept_newer": 0}
        with self.tx():
            cur = self.conn.execute(
                "UPDATE scan_run SET status=?, finished_at=?, snapshot_id=?, snapshot_observed_at=?, snapshot_status=?,"
                " context_json=?, counts_json=? WHERE run_id=? AND status=?",
                (COMPLETE, _ts(now), snapshot["snapshot_id"] if snapshot else None,
                 snapshot["observed_at"] if snapshot else None, snapshot_status,
                 json.dumps(context, ensure_ascii=False, default=str), json.dumps(counts, ensure_ascii=False),
                 run_id, RUNNING))
            if cur.rowcount != 1:
                raise ScanAbortedError(f"{run_id}: RUNNING이 아님(다른 실행이 정리함) — 완료로 표시하지 않음")
            self.conn.executemany(
                "INSERT INTO s1_eval(run_id, symbol, name, market, series_id, signal_date, eligible_signal, pattern_pass,"
                " eligibility_pass, market_pass, stop_valid, data_status, index_status, snapshot_status, no_trades_hold,"
                " final, actionable, input_hash, codec, result_z, evidence_z)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, *(e[f] for f in EVAL_FIELDS), CODEC,
                  None if e["result"] is None else self._pack(e["result"]), self._pack(e["evidence"])) for e in evals])
            for e in evals:
                key = self._upsert_observation(e, run_id=run_id, scan_at=scan_at, now=now, strategy=strategy,
                                               config_hash=config_hash)
                obs_tally[key] += 1
            counts = {**counts, "observations": obs_tally}
            self.conn.execute("UPDATE scan_run SET counts_json=? WHERE run_id=?",
                              (json.dumps(counts, ensure_ascii=False), run_id))
            if before_commit is not None:          # 시험용: 커밋 직전 중단을 흉내
                before_commit()
        return obs_tally

    def _upsert_observation(self, e: dict, *, run_id, scan_at, now, strategy, config_hash) -> str:
        sid = signal_id(strategy, config_hash, e["symbol"], e["signal_date"])
        old = self.conn.execute("SELECT * FROM s1_observation WHERE signal_id=?", (sid,)).fetchone()
        vals = (run_id, _ts(scan_at), e["eligible_signal"], e["data_status"], e["input_hash"], int(e["final"]),
                e["actionable"])
        if old is None:
            self.conn.execute("INSERT INTO s1_observation VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (sid, e["symbol"], e["signal_date"], strategy, config_hash, *vals, run_id, 0, "[]",
                               _ts(now), _ts(now)))
            return "new"
        if old["final"]:
            return "kept_final"                    # 확정된 관찰은 덮어쓰지 않음
        if old["scan_at"] >= _ts(scan_at):
            return "kept_newer"                    # 과거 시각 재현 실행이 더 늦은 기록을 대체하지 않음
        hist = json.loads(old["history_json"]) + [{"run_id": old["run_id"], "scan_at": old["scan_at"],
                                                   "eligible_signal": old["eligible_signal"],
                                                   "data_status": old["data_status"]}]
        self.conn.execute(
            "UPDATE s1_observation SET run_id=?, scan_at=?, eligible_signal=?, data_status=?, input_hash=?, final=?,"
            " actionable=?, replaced_count=replaced_count+1, history_json=?, updated_at=? WHERE signal_id=?",
            (*vals, json.dumps(hist, ensure_ascii=False), _ts(now), sid))
        return "replaced"

    def set_report_path(self, run_id: str, path: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE scan_run SET report_path=? WHERE run_id=?", (path, run_id))

    # ── 읽기 ────────────────────────────────────────────────
    @staticmethod
    def _run_dict(r) -> dict:
        d = dict(r)
        d["context"] = json.loads(d.pop("context_json") or "{}")
        d["counts"] = json.loads(d.pop("counts_json") or "{}")
        return d

    def load_run(self, run_id: str) -> dict | None:
        """저장된 실행 그대로(context·counts·종목 판정) — 다시 계산하지 않고 보고서를 만들 때 씀 (GPT R4)."""
        r = self.conn.execute("SELECT * FROM scan_run WHERE run_id=?", (run_id,)).fetchone()
        if r is None:
            return None
        return {**self._run_dict(r), "evals": self.evals(run_id)}

    def runs(self, signal_date: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM scan_run", ()
        if signal_date:
            q, a = q + " WHERE signal_date=?", (signal_date,)
        return [self._run_dict(r) for r in self.conn.execute(q + " ORDER BY started_at, run_id", a)]

    def evals(self, run_id: str) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT * FROM s1_eval WHERE run_id=? ORDER BY symbol", (run_id,)):
            d = dict(r)
            codec, rz = d.pop("codec"), d.pop("result_z")
            d["result"] = None if rz is None else self._unpack(rz, codec)
            d["evidence"] = self._unpack(d.pop("evidence_z"), codec)
            out.append(d)
        return out

    def observations(self, signal_date: str | None = None) -> list[dict]:
        q, a = "SELECT * FROM s1_observation", ()
        if signal_date:
            q, a = q + " WHERE signal_date=?", (signal_date,)
        return [{**dict(r), "history": json.loads(r["history_json"])}
                for r in self.conn.execute(q + " ORDER BY signal_date, symbol", a)]
