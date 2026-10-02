from __future__ import annotations

"""연구 저장소 읽기 전용 점검 — 스키마를 올리지 않고(백업·이전 없이) UNPROVEN 봉을 저장 기록과 대조합니다.

`python tools/research_collect.py inspect-unproven` 이 씁니다. DB를 읽기 전용(mode=ro)으로 열기 때문에
ResearchStore처럼 자동 이전이 일어나지 않습니다 — 보정 전에 무엇이 바뀔지 먼저 확인하는 용도.

판정(봉의 revision·수신 시각 묶음마다)
- PROVABLE_SAME_SECOND : 같은 revision 저장 기록이 수신 시각 1초 전 안에 있음 → r4 보정에서 MIGRATED로
- PROVABLE             : 수신 시각 이후 저장 기록이 있음 → r4 보정에서 MIGRATED로 (r3에서도 입증됐어야 하는 경우)
- REVISION_UNPROVEN    : 판(revision) 자체가 입증 안 됨(활성 기록 없음·구간 겹침) → 보정 안 함
- NO_EVIDENCE          : 저장 기록 없음 → 보정 안 함(계속 보류)
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from infra.research.store import find_write_evidence, write_events_by_revision


def inspect_unproven(db_path: str | Path, *, limit: int = 30) -> dict:
    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bar)")}
        if "time_basis" not in cols:
            return {"schema": version, "note": "time_basis 열 없음(r3 이전) — UNPROVEN 표시 자체가 없음"}
        groups = [dict(r) for r in conn.execute(
            "SELECT 'bar' AS tbl, series_id, revision, received_at, COUNT(*) AS n, MIN(date) AS first, MAX(date) AS last"
            " FROM bar WHERE time_basis='UNPROVEN' GROUP BY series_id, revision, received_at"
            " UNION ALL SELECT 'bar_history', series_id, revision, received_at, COUNT(*), MIN(date), MAX(date)"
            " FROM bar_history WHERE time_basis='UNPROVEN' GROUP BY series_id, revision, received_at"
            " ORDER BY series_id, revision, received_at")]
        verdicts: dict[str, int] = {}
        bars_by_verdict: dict[str, int] = {}
        rows = []
        cache: dict[str, tuple] = {}
        for g in groups:
            sid = g["series_id"]
            if sid not in cache:
                evs = [(r["at"], r["event"], json.loads(r["detail_json"])) for r in conn.execute(
                    "SELECT at, event, detail_json FROM series_event WHERE series_id=? ORDER BY event_id", (sid,))]
                revs = {r["revision"]: r["reason"] for r in conn.execute(
                    "SELECT revision, reason FROM series_revision WHERE series_id=?", (sid,))}
                cache[sid] = (write_events_by_revision(evs), revs, evs)
            writes, revs, evs = cache[sid]
            reason = revs.get(g["revision"], "")
            w = find_write_evidence(writes, g["revision"], g["received_at"])
            if reason.endswith(":UNPROVEN"):
                v = "REVISION_UNPROVEN"
            elif w is None:
                v = "NO_EVIDENCE"
            elif w < g["received_at"]:
                v = "PROVABLE_SAME_SECOND"
            else:
                v = "PROVABLE"
            verdicts[v] = verdicts.get(v, 0) + 1
            bars_by_verdict[v] = bars_by_verdict.get(v, 0) + g["n"]
            near = [(at, ev) for at, ev, _ in evs
                    if abs((datetime.fromisoformat(at) - datetime.fromisoformat(g["received_at"])).total_seconds()) <= 60]
            rows.append({**g, "revision_reason": reason, "write_evidence": w, "verdict": v,
                         "events_within_60s": near[:5]})
        series = sorted({r["series_id"] for r in rows})
        return {"schema": version, "unproven_bars": sum(g["n"] for g in groups), "series": len(series),
                "groups": len(groups), "verdict_groups": verdicts, "verdict_bars": bars_by_verdict,
                "by_date": _count(rows, "last"), "rows": rows[:limit],
                "note": "PROVABLE*만 r4 보정 대상. REVISION_UNPROVEN·NO_EVIDENCE는 계속 보류."}
    finally:
        conn.close()


def _count(rows, key):
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + r["n"]
    return dict(sorted(out.items()))
