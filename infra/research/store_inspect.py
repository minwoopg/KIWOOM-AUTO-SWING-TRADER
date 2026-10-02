from __future__ import annotations

"""연구 저장소 읽기 전용 점검 — 스키마를 올리지 않고(백업·이전 없이) 시각 보정이 무엇을 바꿀지 미리 봅니다.

`python tools/research_collect.py inspect-unproven` 이 씁니다. DB를 읽기 전용(mode=ro)으로 열기 때문에
ResearchStore처럼 자동 이전이 일어나지 않습니다 — 보정 전에 무엇이 바뀔지 먼저 확인하는 용도.

판정은 실제 보정(`ResearchStore._repair_migrated_series`)과 **같은 함수** `plan_migrated_bar`로 냅니다
(같은 revision으로 귀속된 저장 기록만 근거 — GPT B1).
- PROVABLE_SAME_SECOND : 같은 판의 저장 기록이 수신 시각 1초 전 안에 있음 → MIGRATED
- PROVABLE             : 수신 시각 이후 같은 판의 저장 기록이 있음 → MIGRATED
- REVISION_UNPROVEN    : 판(revision) 자체가 입증 안 됨(활성 기록 없음·구간 겹침) → UNPROVEN 유지
- NO_EVIDENCE          : 같은 판으로 귀속된 저장 기록 없음 → UNPROVEN 유지(계속 보류)

출력
- UNPROVEN 봉: 위 판정별 집계(지금 보류된 봉이 풀릴지).
- recheck_migrated: 이미 MIGRATED인 봉을 같은 규칙으로 다시 계산했을 때 바뀌는 것(r4 → r5 재점검 미리 보기).
  MIGRATED→UNPROVEN(다른 판·모호한 기록으로 입증됐던 봉) / 사용 가능 시각 변경.
- unattributed_writes: 어느 판인지 정할 수 없어 근거로 쓰지 않는 예전 저장 기록(사유별).
- scan_db: 관찰 기록 DB에서 다음 열기(s2) 때 확정(final)을 풀 대표 기록 수 — GPT B2.
"""

import sqlite3
from pathlib import Path

from infra.research.store import MIGRATED_TIME, UNPROVEN_TIME, load_series_evidence, plan_migrated_bar


def _ro(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def inspect_unproven(db_path: str | Path, *, limit: int = 30, scan_db: str | Path | None = None) -> dict:
    conn = _ro(db_path)
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bar)")}
        if "time_basis" not in cols:
            return {"schema": version, "note": "time_basis 열 없음(r3 이전) — UNPROVEN 표시 자체가 없음"}
        groups = [dict(r) for r in conn.execute(
            "SELECT 'bar' AS tbl, series_id, revision, received_at, time_basis, available_at, COUNT(*) AS n,"
            " MIN(date) AS first, MAX(date) AS last FROM bar WHERE time_basis IN (?, ?)"
            " GROUP BY series_id, revision, received_at, time_basis, available_at"
            " UNION ALL SELECT 'bar_history', series_id, revision, received_at, time_basis, available_at, COUNT(*),"
            " MIN(date), MAX(date) FROM bar_history WHERE time_basis IN (?, ?)"
            " GROUP BY series_id, revision, received_at, time_basis, available_at"
            " ORDER BY series_id, revision, received_at", (UNPROVEN_TIME, MIGRATED_TIME) * 2)]
        cache: dict[str, tuple] = {}
        unattributed: dict[str, int] = {}
        verdicts: dict[str, int] = {}
        bars_by_verdict: dict[str, int] = {}
        rows, changes = [], []
        recheck = {"bars": 0, "unchanged": 0, "MIGRATED->UNPROVEN": 0, "available_at_changed": 0}
        for g in groups:
            sid = g["series_id"]
            if sid not in cache:
                cache[sid] = load_series_evidence(conn, sid)
                for w in cache[sid][1]:
                    if w.revision is None:
                        unattributed[w.source] = unattributed.get(w.source, 0) + 1
            tl, writes, reasons = cache[sid]
            p = plan_migrated_bar(tl, writes, g["revision"], g["received_at"])
            ev = None if p.evidence is None else {"at": p.evidence.at, "event": p.evidence.event,
                                                  "source": p.evidence.source}
            if g["time_basis"] == UNPROVEN_TIME:
                verdicts[p.verdict] = verdicts.get(p.verdict, 0) + 1
                bars_by_verdict[p.verdict] = bars_by_verdict.get(p.verdict, 0) + g["n"]
                rows.append({k: g[k] for k in ("tbl", "series_id", "revision", "received_at", "n", "first", "last")}
                            | {"revision_reason": reasons.get(g["revision"], ""), "verdict": p.verdict,
                               "write_evidence": ev})
                continue
            recheck["bars"] += g["n"]
            if p.time_basis == UNPROVEN_TIME:
                kind = "MIGRATED->UNPROVEN"
            elif p.time_basis == MIGRATED_TIME and p.available_at != g["available_at"]:
                kind = "available_at_changed"
            else:
                recheck["unchanged"] += g["n"]
                continue
            recheck[kind] += g["n"]
            changes.append({k: g[k] for k in ("tbl", "series_id", "revision", "received_at", "n", "first", "last")}
                           | {"change": kind, "verdict": p.verdict, "available_at": g["available_at"],
                              "new_available_at": p.available_at, "write_evidence": ev})
        out = {"schema": version, "unproven_bars": sum(r["n"] for r in rows),
               "series": len({r["series_id"] for r in rows}), "groups": len(rows), "verdict_groups": verdicts,
               "verdict_bars": bars_by_verdict, "by_date": _count(rows, "last"), "rows": rows[:limit],
               "recheck_migrated": {**recheck, "series": len({c["series_id"] for c in changes}),
                                    "rows": changes[:limit]},
               "unattributed_writes": dict(sorted(unattributed.items())),
               "note": "다음 스키마 변경(r5) 때 이 판정대로 보정. PROVABLE*만 MIGRATED, REVISION_UNPROVEN·NO_EVIDENCE는 "
                       "UNPROVEN(보류). recheck_migrated는 이미 MIGRATED인 봉 중 바뀔 것."}
    finally:
        conn.close()
    if scan_db is not None and Path(scan_db).exists():
        from infra.research.scan_store import inspect_final_reset
        out["scan_db"] = inspect_final_reset(scan_db)
    return out


def _count(rows, key):
    out: dict[str, int] = {}
    for r in rows:
        out[r[key]] = out.get(r[key], 0) + r["n"]
    return dict(sorted(out.items()))
