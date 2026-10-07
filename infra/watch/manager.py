from __future__ import annotations

"""지정 종목 설정 적용·데이터 준비 (조회 전용, 주문 없음).

설정 적용 (`sync_config`) — 파일을 읽어 검증하고 결과를 이력(config_version)에 남김
- 정상 → APPLIED(새 버전이 사용 중 설정). 오류 → REJECTED(적용 안 함)·오류 보존.
- 정상 설정이 한 번도 없으면 감시를 시작하지 않음(can_monitor=False).
- 정상 설정이 있는데 새 내용이 틀리면 마지막 정상 설정으로 감시 유지 + 오류·사용 중 버전 표시, **신규 매수 차단**
  (entry_blocked — 이후 주문 단계가 반드시 확인).
- 파일이 없어져도 같은 규칙(REJECTED "파일 없음").

데이터 준비 (`prepare_data`) — 등록 종목(관심 켜짐 또는 보유 있음)과 국내 지수 2개만 갱신
- 연구 수집기(ResearchCollector.update_series)를 그대로 씀: 없는 시계열은 처음부터 전체 수집(등록 즉시 이력 확보),
  있는 시계열은 새 완성 봉만 추가. 전체 시장 갱신은 하지 않음(그건 tools/research_collect.py update).
- 종목 목록에 없는 종목·열린 백필 작업에 걸린 시계열은 조회하지 않고 UNKNOWN.
- 준비 상태(readiness): 최근 완성 거래일까지 확보·검증된 완성 일봉이 history_sessions개 이상이면 READY,
  아니면 UNKNOWN(사유). 그 시각에 확보 시각이 입증된 봉만 셈(연구 저장소 as_of 조회). READY 전에는 매매 가능 상태가 아님.
- 모든 기록에 적용 중인 설정 버전을 남김.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from domain.watchlist.config import (
    Issue, Listing, WatchConfig, WatchSymbol, config_from_dict, parse_text, validate,
)
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE, INDEX_TARGETS, CollectError, stock_series_id
from infra.research.kiwoom_readonly import ResearchApiError, ResearchConfigError
from infra.research.kiwoom_rows import RowError
from infra.research.s1_scanner import OK, ScanError, data_status, expected_session
from infra.research.store import PENDING, ERROR, IntegrityError, ResearchStore
from infra.watch.store import APPLIED, REJECTED, WatchStore
from utils.trading_calendar import TradingCalendar

READY, UNKNOWN = "READY", "UNKNOWN"
INDEX_NAMES = {"INDEX:KOSPI:001": "KOSPI", "INDEX:KOSDAQ:101": "KOSDAQ"}


class WatchNotReady(RuntimeError):
    """정상 설정이 없어 감시·준비를 시작할 수 없음."""


def listing_from_store(rstore: ResearchStore) -> tuple[dict[str, Listing] | None, int | None]:
    """연구 저장소의 최신 종목 목록 스냅숏 → 코드별 Listing (없으면 None)."""
    snap = rstore.latest_snapshot()
    if snap is None:
        return None, None
    out = {}
    for r in rstore.snapshot_rows(snap["snapshot_id"]):
        out[r["code"]] = Listing(r["code"], r["name"], r["market"], r["security_type"], tuple(r["risk_flags"]),
                                 r["reg_day"])
    return out, snap["snapshot_id"]


@dataclass
class WatchState:
    active: dict | None                 # 사용 중(마지막 APPLIED) 버전 기록
    latest: dict | None                 # 가장 최근 시도
    config: WatchConfig | None = field(default=None)

    @property
    def active_version(self) -> int | None:
        return None if self.active is None else self.active["version"]

    @property
    def can_monitor(self) -> bool:
        return self.config is not None

    @property
    def config_error(self) -> list[dict]:
        if self.latest is not None and self.latest["status"] == REJECTED:
            return self.latest["errors"]
        return []

    @property
    def entry_blocked(self) -> bool:
        return self.config is None or bool(self.config_error)

    @property
    def block_reason(self) -> str:
        if self.config is None:
            return "NO_VALID_CONFIG(정상 설정 없음 — 감시 시작 안 함)"
        if self.config_error:
            return (f"CONFIG_ERROR(v{self.latest['version']} 거부 — 마지막 정상 v{self.active_version}로 감시 유지, "
                    "신규 매수 차단)")
        return ""

    def summary(self) -> dict:
        return {"can_monitor": self.can_monitor, "active_version": self.active_version,
                "active_applied_at": None if self.active is None else self.active["attempted_at"],
                "latest_version": None if self.latest is None else self.latest["version"],
                "latest_status": None if self.latest is None else self.latest["status"],
                "config_error": self.config_error, "entry_blocked": self.entry_blocked,
                "block_reason": self.block_reason,
                "warnings": [] if self.active is None else self.active["warnings"]}


def load_state(wstore: WatchStore) -> WatchState:
    active, latest = wstore.active_version(), wstore.latest_attempt()
    cfg = None if active is None else config_from_dict(active["config"])
    return WatchState(active, latest, cfg)


def _issues(xs: list[Issue]) -> list[dict]:
    return [{"code": i.code, "field": i.field, "message": i.message} for i in xs]


def check_text(raw_text: str | None, listing: dict[str, Listing] | None, *, check_list: bool = True):
    """원문 → ValidationResult (파일 없음·YAML 오류도 오류 결과로)."""
    from domain.watchlist.config import ValidationResult
    if raw_text is None:
        return ValidationResult(None, [Issue("-", "file", "설정 파일 없음")])
    raw, perr = parse_text(raw_text)
    if perr:
        return ValidationResult(None, [Issue("-", "file", perr)])
    return validate(raw, listing, check_list=check_list)


def sync_config(wstore: WatchStore, path: str | Path, listing: dict[str, Listing] | None,
                snapshot_id: int | None, *, now: datetime, origin: str = "FILE") -> tuple[WatchState, dict]:
    """설정 파일을 읽어 검증·기록. 반환 (상태, 이번 시도 {version, status, new, errors, warnings})."""
    p = Path(path)
    raw_text = p.read_text(encoding="utf-8") if p.exists() else None
    raw_sha = "MISSING" if raw_text is None else hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]
    res = check_text(raw_text, listing)
    status = APPLIED if res.ok else REJECTED
    version, new = wstore.record_attempt(
        at=now, origin=origin, source_path=str(p), raw_sha=raw_sha, status=status,
        config_hash=res.config.norm_hash() if res.ok else None, errors=_issues(res.errors),
        warnings=_issues(res.warnings), raw_text=raw_text, config=res.config.to_dict() if res.ok else None,
        list_snapshot_id=snapshot_id)
    return load_state(wstore), {"version": version, "status": status, "new": new, "errors": _issues(res.errors),
                                "warnings": _issues(res.warnings)}


# ── 데이터 준비 상태 ─────────────────────────────────────────
def _targets(cfg: WatchConfig, codes: list[str] | None = None) -> list[tuple[str, str, str, WatchSymbol | None]]:
    syms = cfg.active_symbols
    if codes is not None:
        syms = tuple(s for s in syms if s.code in codes)
    out = [(sid, "INDEX", code, None) for sid, code in INDEX_TARGETS]
    out += [(stock_series_id(s.code), "STOCK", s.code, s) for s in syms]
    return out


def evaluate_readiness(rstore: ResearchStore, calendar: TradingCalendar, cfg: WatchConfig,
                       listing: dict[str, Listing] | None, *, now: datetime,
                       after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE, fetch: dict | None = None) -> list[dict]:
    """감시 대상(지수 2 + 등록 종목) 각각 READY / UNKNOWN(사유). 저장하지 않음."""
    need = cfg.monitor.history_sessions
    fetch = fetch or {}
    try:
        t = expected_session(calendar, now, after_close)
        sessions = calendar.trading_days_in_range(date(min(calendar.covered_years), 1, 1), t)
        cal_err = "" if len(sessions) >= need else f"CALENDAR_SHORT:{len(sessions)}/{need}"
    except ScanError as exc:
        t, sessions, cal_err = None, [], f"CALENDAR:{exc}"[:200]
    window = sessions[-need:]
    rows = []
    for sid, kind, code, sym in _targets(cfg):
        lrow = None if listing is None or kind != "STOCK" else listing.get(code)
        detail = {"need": need, "expected_last": None if t is None else t.isoformat(),
                  "modes": list(sym.modes) if sym else ["MARKET"], "fetch": fetch.get(sid)}
        if lrow is not None:
            detail["risk_flags"] = list(lrow.risk_flags)
            detail["security_type"] = lrow.security_type
        reason = ""
        if cal_err:
            reason = cal_err
        elif kind == "STOCK" and lrow is None:
            reason = "NOT_IN_LIST"
        else:
            try:
                rs = rstore.research_series(sid, as_of=now, start=window[0])
            except KeyError:
                rs = None
            st = data_status(rs, t)
            have = 0 if rs is None else len({b.date for b in rs.bars if b.date >= window[0]})
            detail.update(have=have, last=None if rs is None or not rs.bars else rs.bars[-1].date.isoformat())
            detail["data_status"] = st
            if st != OK:
                # 사유는 범주만(날짜·개수는 detail) — 매일 숫자가 바뀌어도 상태 이력이 늘지 않게
                reason = st if st.startswith("INTEGRITY") else st.split(":")[0]
            elif have < need:
                first = rs.bars[0].date if rs.bars else None
                reason = "INSUFFICIENT_HISTORY" if first is None or first > window[0] else "MISSING_SESSIONS"
        rows.append({"target": sid, "kind": kind, "code": code, "status": UNKNOWN if reason else READY,
                     "reason": reason, "detail": detail})
    return rows


def entry_gate(state: WatchState, sym: WatchSymbol, ready: dict | None) -> tuple[bool, list[str]]:
    """신규 진입 관찰(이후 매수) 가능 여부와 막는 이유 — 데이터 UNKNOWN·위험 표시·설정 오류·관심 꺼짐이면 불가."""
    why = []
    if state.entry_blocked:
        why.append(state.block_reason.split("(")[0])
    if not sym.interest_active:
        why.append("INTEREST_OFF")
    if ready is None or ready["status"] != READY:
        why.append(f"DATA_{UNKNOWN}" + ("" if ready is None else f":{ready['reason']}"))
    elif ready["detail"].get("risk_flags"):
        why.append("RISK_FLAGS")
    return not why, why


# ── 데이터 준비 실행 ─────────────────────────────────────────
def prepare_data(wstore: WatchStore, rstore: ResearchStore, collector, calendar: TradingCalendar,
                 state: WatchState, listing: dict[str, Listing] | None, *, now: Callable[[], datetime],
                 codes: list[str] | None = None, after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE,
                 log: Callable[[str], None] = lambda m: None) -> dict:
    """등록 종목(+지수)만 갱신하고 준비 상태를 저장. codes가 있으면 그 종목(+지수)만 조회 — 상태 판정은 전체 등록 종목."""
    if not state.can_monitor:
        raise WatchNotReady(state.block_reason)
    cfg, version = state.config, state.active_version
    if codes is not None:
        unknown = sorted(set(codes) - {s.code for s in cfg.active_symbols})
        if unknown:
            raise WatchNotReady(f"감시 중인 등록 종목이 아님: {unknown}")
    targets = _targets(cfg, codes)
    blocked = set()
    for j in rstore.open_jobs():
        blocked |= {it["series_id"] for it in rstore.job_items(j["job_id"], (PENDING, ERROR))}
    run_id = wstore.begin_prepare(version, now(), [t[0] for t in targets])
    fetch: dict[str, dict] = {}
    tally: dict[str, int] = {}
    status = "COMPLETE"
    try:
        for sid, kind, code, sym in targets:
            lrow = None if listing is None else listing.get(code)
            if kind == "STOCK" and lrow is None:
                res = {"action": "SKIPPED", "reason": "NOT_IN_LIST"}
            elif sid in blocked:
                res = {"action": "SKIPPED", "reason": "OPEN_BACKFILL_JOB(연구 백필 작업이 맡고 있음)"}
            else:
                reg = date.fromisoformat(lrow.reg_day) if lrow is not None and lrow.reg_day else None
                try:
                    res = collector.update_series(sid, kind, code, now=now, reg_day=reg)
                except ResearchConfigError:
                    raise
                except (ResearchApiError, RowError, CollectError, ValueError, IntegrityError) as exc:
                    res = {"action": "ERROR", "reason": f"{type(exc).__name__}: {exc}"[:300]}
                    log(f"[준비] {sid} 조회 실패 {exc}")
            fetch[sid] = {"action": res["action"], "reason": res.get("reason", ""), "at": now().isoformat()}
            tally[res["action"]] = tally.get(res["action"], 0) + 1
    except BaseException as exc:
        status = "FAILED"
        wstore.finish_prepare(run_id, now(), status, {"fetch": tally}, f"{type(exc).__name__}: {exc}"[:300])
        raise
    rows = evaluate_readiness(rstore, calendar, cfg, listing, now=now(), after_close=after_close, fetch=fetch)
    changed = wstore.set_readiness(rows, version=version, run_id=run_id, at=now())
    ready = {r["status"]: 0 for r in rows}
    for r in rows:
        ready[r["status"]] += 1
    if tally.get("ERROR"):
        status = "PARTIAL"
    counts = {"fetch": tally, "readiness": ready, "changed": changed}
    wstore.finish_prepare(run_id, now(), status, counts)
    return {"run_id": run_id, "config_version": version, "status": status, "counts": counts,
            "calls": getattr(collector.client, "calls", None), "rows": rows}
