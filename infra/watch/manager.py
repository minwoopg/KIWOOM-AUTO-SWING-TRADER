from __future__ import annotations

"""지정 종목 설정 적용·데이터 준비 (조회 전용, 주문 없음).

설정 적용 (`sync_config`) — 파일을 읽어 검증하고 결과를 이력(config_version)에 남김
- 정상 → APPLIED(새 버전이 사용 중 설정). 오류 → REJECTED(적용 안 함)·오류 보존.
- 정상 설정이 한 번도 없으면 감시를 시작하지 않음(can_monitor=False).
- 정상 설정이 있는데 새 내용이 틀리면 마지막 정상 설정으로 감시 유지 + 오류·사용 중 버전 표시, **신규 매수 차단**
  (entry_blocked — 이후 주문 단계가 반드시 확인).
- 파일이 없어지거나 읽을 수 없어도(인코딩·권한 오류 — W1-R4) 같은 규칙(REJECTED).
- 보유 보호(W1-R1): 마지막 정상 설정에 있던 수동 보유가 새 설정에서 사라지면, `holding-close` 청산 기록(같은 보유 값)이
  있을 때만 적용. 없으면 REJECTED — 파일을 직접 고쳐도 보유 감시가 조용히 사라지지 않음.
  청산 기록은 **그 기록을 만든 명령의 적용에서만** 씀(close_id를 넘겨받음 — W1c-R1). 적용이 끝나면 쓰이지 않은 OPEN 기록은
  모두 VOID — 명령이 실패·강제 종료돼 남은 기록이 나중의 apply에서 보유 보호를 통과시키지 않음.
- 위험 자격(W1-R2): 설정을 읽을 때마다 **최신 종목 목록**으로 종목별 위험 자격(symbol_risk)을 갱신·기록 — 준비(prepare)를
  기다리지 않음. 진입 관찰은 이 기록으로 판단(준비 기록의 옛 위험 값을 쓰지 않음).

데이터 준비 (`prepare_data`) — 등록 종목(관심 켜짐 또는 보유 있음)과 국내 지수 2개만 갱신
- 연구 수집기(ResearchCollector.update_series)를 그대로 씀: 없는 시계열은 처음부터 전체 수집(등록 즉시 이력 확보),
  있는 시계열은 새 완성 봉만 추가. 전체 시장 갱신은 하지 않음(그건 tools/research_collect.py update).
- 종목 목록에 없는 종목·열린 백필 작업에 걸린 시계열은 조회하지 않고 UNKNOWN.
- 준비 상태는 둘로 나눔 (W1-R3):
  * 가격 데이터(status): 최근 완성 거래일까지 확보 시각이 입증된 일봉이 있고 정합성 정상이면 READY, 아니면 UNKNOWN.
    보유 가격 감시는 이것만 봄 — 분석이 보류돼도 보유 감시는 유지.
  * S1 분석(analysis_status): S1 계산과 같은 `SeriesView.window(max(history_sessions, S1 min_history))` 계약 — 기준일 봉 없음·거래 없음
    (NO_TRADES_AT_T), 창 안 거래 없는 봉(NO_TRADES)·누락(DATA_GAP)·이력 부족(INSUFFICIENT_HISTORY)·달력 부족이면 HOLD.
    READY 전에는 진입 관찰(이후 매수) 대상이 아님. 판정 기준(전략·S1 설정 해시·필요 봉 수)을 detail.analysis_basis에
    남기고, 지금 설정의 기준과 다르면 다시 판정하기 전까지 HOLD(BASIS_CHANGED) — W1b-R1.
- 모든 기록에 적용 중인 설정 버전을 남김.
"""

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from domain.watchlist.config import (
    Issue, Listing, WatchConfig, WatchSymbol, config_from_dict, parse_text, validate,
)
from domain.research.s1 import STRATEGY_ID, STRATEGY_VERSION, S1Config
from domain.research.series import SeriesView
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE, INDEX_TARGETS, CollectError, stock_series_id
from infra.research.kiwoom_readonly import RequestStopped, ResearchApiError, ResearchConfigError
from infra.research.kiwoom_rows import RowError
from infra.research.s1_scanner import OK, ScanError, data_status, expected_session
from infra.research.store import PENDING, ERROR, IntegrityError, ResearchStore
from infra.watch.store import APPLIED, REJECTED, WatchStore
from utils.trading_calendar import TradingCalendar

READY, UNKNOWN, HOLD = "READY", "UNKNOWN", "HOLD"
RISK_OK, RISK_FLAGGED, RISK_NOT_IN_LIST, RISK_NO_LIST = "OK", "RISK", "NOT_IN_LIST", "NO_LIST"
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
    journal_block: str | None = None    # 미해결 적용 저널(R5) — REJECTED 기록이 실패해도 신규 진입 차단

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
        return self.config is None or bool(self.config_error) or self.journal_block is not None

    @property
    def block_reason(self) -> str:
        if self.config is None:
            return "NO_VALID_CONFIG(정상 설정 없음 — 감시 시작 안 함)"
        if self.journal_block is not None:
            return (f"JOURNAL_UNRESOLVED({self.journal_block} — 마지막 정상 v{self.active_version}로 감시 유지, 신규 매수 "
                    "차단; restore 또는 resolve-journal --keep-file)")
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
                "block_reason": self.block_reason, "journal_block": self.journal_block,
                "warnings": [] if self.active is None else self.active["warnings"]}


def load_state(wstore: WatchStore) -> WatchState:
    active, latest = wstore.active_version(), wstore.latest_attempt()
    cfg = None if active is None else config_from_dict(active["config"])
    return WatchState(active, latest, cfg)


def _issues(xs: list[Issue]) -> list[dict]:
    return [{"code": i.code, "field": i.field, "message": i.message} for i in xs]


def read_config_text(path: str | Path) -> tuple[str | None, str | None]:
    """(원문, 읽기 오류). 없으면 (None, None). 인코딩·권한 등 읽기 실패는 예외 대신 오류 문자열 (W1-R4)."""
    p = Path(path)
    try:
        if not p.exists():
            return None, None
        return p.read_text(encoding="utf-8"), None
    except UnicodeError as exc:
        return None, f"UTF-8로 읽을 수 없음({type(exc).__name__}) — 파일을 UTF-8로 저장하세요"
    except OSError as exc:
        return None, f"파일을 읽을 수 없음({type(exc).__name__}: {exc.strerror or exc})"


def check_text(raw_text: str | None, listing: dict[str, Listing] | None, *, check_list: bool = True,
               read_error: str | None = None):
    """원문 → ValidationResult (파일 없음·읽기 실패·YAML 오류도 오류 결과로)."""
    from domain.watchlist.config import ValidationResult
    if read_error:
        return ValidationResult(None, [Issue("-", "file", read_error)])
    if raw_text is None:
        return ValidationResult(None, [Issue("-", "file", "설정 파일 없음")])
    raw, perr = parse_text(raw_text)
    if perr:
        return ValidationResult(None, [Issue("-", "file", perr)])
    return validate(raw, listing, check_list=check_list)


class ConfigConflict(RuntimeError):
    """적용하려던 기준 버전과 지금 사용 중 버전이 다름 — 다른 명령·프로세스가 먼저 적용함 (C1)."""


def holding_guard(active: WatchConfig | None, active_version: int | None, new: WatchConfig, *,
                  close_holdings: dict[str, dict] | None = None) -> tuple[list[Issue], list[str]]:
    """마지막 정상 설정의 수동 보유가 새 설정에서 사라졌는지 (W1-R1).
    close_holdings = 이번 적용이 함께 기록할 청산 {코드: 보유 값}. 그 코드가 **사용 중 설정의 보유 종목이고 값이 지금
    보유와 같을 때만** 근거가 됨(C2 — 종목·값 일치. 기준 버전 일치는 호출하는 쪽이 expect_version으로 같은 트랜잭션에서 확인).
    저장소에 남은 OPEN 청산 기록은 근거가 아님(W1c-R1/W1d-R1).
    반환 (오류, 이번에 청산으로 쓴 코드)."""
    close_holdings = close_holdings or {}
    errors, used = [], []
    held = {} if active is None else {s.code: s for s in active.symbols if s.holding is not None}
    for code in close_holdings:
        if code not in held:
            errors.append(Issue(code, "holding", f"청산 근거 불일치 — 사용 중 v{active_version}에 이 종목의 수동 보유가 없음"))
    for code, old in held.items():
        cur = new.symbol(code)
        if cur is not None and cur.holding is not None:
            continue
        if code in close_holdings:
            if close_holdings[code] == asdict(old.holding):
                used.append(code)
                continue
            errors.append(Issue(code, "holding", f"청산 근거 불일치 — 청산하려는 보유 값이 사용 중 v{active_version}의 보유와 다름"))
            continue
        h = old.holding
        errors.append(Issue(code, "holding",
                            f"마지막 정상 v{active_version}의 수동 보유({h.quantity:,}주 @ {h.avg_price:,})가 새 설정에 없음 — "
                            f"청산이면 `python tools/watchlist.py holding-close {code}`(YAML에서 이미 지웠어도 됨), "
                            "실수면 `python tools/watchlist.py restore`로 마지막 정상 설정 복원 — 그 전까지 보유 감시 유지"))
    return errors, used


def refresh_risk(wstore: WatchStore, state: WatchState, listing: dict[str, Listing] | None, snapshot_id: int | None,
                 *, now: datetime) -> int:
    """사용 중 설정의 종목별 위험 자격을 최신 목록으로 갱신 (W1-R2). 반환: 바뀐 수."""
    if state.config is None:
        return 0
    rows = []
    for sym in state.config.symbols:
        lr = None if listing is None else listing.get(sym.code)
        if listing is None:
            st, flags = RISK_NO_LIST, []
        elif lr is None:
            st, flags = RISK_NOT_IN_LIST, []
        else:
            st, flags = (RISK_FLAGGED if lr.risk_flags else RISK_OK), list(lr.risk_flags)
        rows.append({"code": sym.code, "status": st, "flags": flags})
    return wstore.set_risk(rows, snapshot_id=snapshot_id, version=state.active_version, at=now)


def file_sha(raw_text: str | None, read_error: str | None, path: Path) -> str:
    if read_error:
        try:
            return "UNREADABLE:" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            return "UNREADABLE"
    return "MISSING" if raw_text is None else hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]


def sync_config(wstore: WatchStore, path: str | Path, listing: dict[str, Listing] | None,
                snapshot_id: int | None, *, now: datetime, origin: str = "FILE",
                close_holdings: dict[str, dict] | None = None, expect_version: int | None = None,
                hook: Callable[[str], None] | None = None) -> tuple[WatchState, dict]:
    """설정 파일을 읽어 검증·기록하고 위험 자격을 갱신. 반환 (상태, 이번 시도 {version, status, new, errors, warnings}).

    **확정 지점 = 아래 트랜잭션의 COMMIT 한 번** (W1d-R1). 시도 기록(APPLIED/REJECTED)·청산 기록(USED)·남은 OPEN 정리·
    위험 자격 갱신이 모두 같은 트랜잭션 — 중간 어디서 실패·중단돼도 전부 되돌아가고 이전 사용 중 설정·보유 감시가 그대로.
    COMMIT 뒤의 출력·보고 실패는 적용 취소가 아님.
    close_holdings = 이번 적용이 함께 기록할 청산 {코드: 보유 값}(holding-close). 이때 expect_version 필수 —
    트랜잭션 안에서 사용 중 버전이 그대로인지 다시 확인(아니면 ConfigConflict, 아무것도 기록 안 함)."""
    from domain.watchlist.config import ValidationResult
    if close_holdings and expect_version is None:
        raise ValueError("close_holdings에는 expect_version이 필요")
    hook = hook or (lambda point: None)
    p = Path(path)
    raw_text, read_error = read_config_text(p)
    raw_sha = file_sha(raw_text, read_error, p)
    res0 = check_text(raw_text, listing, read_error=read_error)
    with wstore.tx():
        before = load_state(wstore)
        if expect_version is not None and before.active_version != expect_version:
            raise ConfigConflict(f"사용 중 설정이 v{expect_version}에서 v{before.active_version}로 바뀜 — 다시 실행하세요")
        res, used = res0, []
        if res.ok:
            guard, used = holding_guard(before.config, before.active_version, res.config, close_holdings=close_holdings)
            if guard:
                res, used = ValidationResult(None, guard, res.warnings), []
        status = APPLIED if res.ok else REJECTED
        version, new = wstore.record_attempt(
            at=now, origin=origin, source_path=str(p), raw_sha=raw_sha, status=status,
            config_hash=res.config.norm_hash() if res.ok else None, errors=_issues(res.errors),
            warnings=_issues(res.warnings), raw_text=raw_text, config=res.config.to_dict() if res.ok else None,
            list_snapshot_id=snapshot_id)
        hook("after_attempt")
        for code in used:
            wstore.record_close(code, close_holdings[code], at=now, from_version=before.active_version, origin=origin,
                                state="USED", used_version=version)
        hook("after_close")
        wstore.void_open_closes(keep=set())          # 예전 버전이 남긴 OPEN 청산 기록 정리
        hook("after_void")
        state = load_state(wstore)
        refresh_risk(wstore, state, listing, snapshot_id, now=now)
        hook("after_risk")
    return state, {"version": version, "status": status, "new": new, "errors": _issues(res.errors),
                   "warnings": _issues(res.warnings), "raw_sha": raw_sha}


# ── 데이터 준비 상태 ─────────────────────────────────────────
def _targets(cfg: WatchConfig, codes: list[str] | None = None) -> list[tuple[str, str, str, WatchSymbol | None]]:
    syms = cfg.active_symbols
    if codes is not None:
        syms = tuple(s for s in syms if s.code in codes)
    out = [(sid, "INDEX", code, None) for sid, code in INDEX_TARGETS]
    out += [(stock_series_id(s.code), "STOCK", s.code, s) for s in syms]
    return out


def analysis_basis(cfg: WatchConfig) -> dict:
    """S1 분석 준비를 판정한 기준. 설정·S1 계약이 바뀌면 값이 달라져 이전 READY를 쓰지 않음 (W1b-R1)."""
    s1 = S1Config()
    return {"strategy": f"{STRATEGY_ID}_{STRATEGY_VERSION}", "s1_config": s1.config_hash(),
            "need": max(cfg.monitor.history_sessions, s1.min_history)}


def evaluate_readiness(rstore: ResearchStore, calendar: TradingCalendar, cfg: WatchConfig,
                       listing: dict[str, Listing] | None, *, now: datetime,
                       after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE, fetch: dict | None = None) -> list[dict]:
    """감시 대상(지수 2 + 등록 종목)마다 가격 데이터 준비(READY/UNKNOWN)와 S1 분석 준비(READY/HOLD). 저장하지 않음.
    사유는 범주만(날짜·봉 수는 detail) — 매일 숫자가 바뀌어도 상태 이력이 늘지 않게."""
    basis = analysis_basis(cfg)
    need = basis["need"]
    fetch = fetch or {}
    try:
        t = expected_session(calendar, now, after_close)
        sessions = calendar.trading_days_in_range(date(min(calendar.covered_years), 1, 1), t)
        cal_err = ""
    except ScanError as exc:
        t, sessions, cal_err = None, [], f"CALENDAR:{exc}"[:200]
    start = sessions[-need:][0] if sessions else None
    rows = []
    for sid, kind, code, sym in _targets(cfg):
        lrow = None if listing is None or kind != "STOCK" else listing.get(code)
        detail = {"need": need, "analysis_basis": basis, "expected_last": None if t is None else t.isoformat(),
                  "modes": list(sym.modes) if sym else ["MARKET"], "fetch": fetch.get(sid)}
        if lrow is not None:
            detail["security_type"] = lrow.security_type
        reason, a_status, a_reason = "", HOLD, ""
        if cal_err:
            reason = a_reason = cal_err.split(":")[0]
            detail["calendar"] = cal_err
        elif kind == "STOCK" and lrow is None:
            reason = a_reason = "NOT_IN_LIST"
        else:
            try:
                rs = rstore.research_series(sid, as_of=now, start=start)
            except KeyError:
                rs = None
            st = data_status(rs, t)
            detail.update(data_status=st, have=0 if rs is None else len(rs.bars),
                          last=None if rs is None or not rs.bars else rs.bars[-1].date.isoformat())
            if st != OK:
                reason = st if st.startswith("INTEGRITY") else st.split(":")[0]
                a_reason = "DATA_" + UNKNOWN
            else:
                # S1 계산과 같은 계약: 기준일 봉·거래, 창 안 거래 없는 봉·누락·이력·달력 (W1-R3)
                bars, why = SeriesView(rs.bars, sessions, t, source_id=sid).window(need)
                detail["window"] = why or "OK"
                if bars is not None:
                    a_status = READY
                else:
                    a_reason = why.split(":")[0]
        rows.append({"target": sid, "kind": kind, "code": code, "status": UNKNOWN if reason else READY,
                     "reason": reason, "analysis_status": a_status, "analysis_reason": a_reason, "detail": detail})
    return rows


def entry_gate(state: WatchState, sym: WatchSymbol, ready: dict | None, risk: dict | None, *,
               snapshot_id: int | None = None) -> tuple[bool, list[str]]:
    """신규 진입 관찰(이후 매수) 가능 여부와 막는 이유. 설정 오류·관심 꺼짐·위험 자격(최신 목록 기준 symbol_risk,
    snapshot_id를 주면 그 목록으로 다시 확인된 기록만)·가격 데이터·S1 분석 준비 중 하나라도 아니면 불가."""
    why = []
    if state.entry_blocked:
        why.append(state.block_reason.split("(")[0])
    if not sym.interest_active:
        why.append("INTEREST_OFF")
    if risk is None:
        why.append("RISK_UNVERIFIED")
    elif snapshot_id is not None and risk["list_snapshot_id"] != snapshot_id:
        why.append("RISK_STALE")
    elif risk["status"] == RISK_FLAGGED:
        why.append("RISK_FLAGS")
    elif risk["status"] != RISK_OK:
        why.append(f"RISK:{risk['status']}")
    if ready is None or ready["status"] != READY:
        why.append(f"DATA_{UNKNOWN}" + ("" if ready is None else f":{ready['reason']}"))
    elif ready.get("analysis_status") != READY:
        why.append(f"ANALYSIS_{HOLD}:{ready.get('analysis_reason') or 'NOT_EVALUATED'}")
    elif state.config is not None and (ready.get("detail") or {}).get("analysis_basis") != analysis_basis(state.config):
        why.append(f"ANALYSIS_{HOLD}:BASIS_CHANGED")            # 준비 기준이 바뀜 — 다시 prepare 전까지 (W1b-R1)
    return not why, why


# ── 데이터 준비 실행 ─────────────────────────────────────────
def prepare_data(wstore: WatchStore, rstore: ResearchStore, collector, calendar: TradingCalendar,
                 state: WatchState, listing: dict[str, Listing] | None, *, now: Callable[[], datetime],
                 codes: list[str] | None = None, after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE,
                 log: Callable[[str], None] = lambda m: None,
                 should_stop: Callable[[], str | None] | None = None) -> dict:
    """등록 종목(+지수)만 갱신하고 준비 상태를 저장. codes가 있으면 그 종목(+지수)만 조회 — 상태 판정은 전체 등록 종목.
    should_stop(): 대상마다 조회 전에 부름 — 사유를 돌려주면 남은 대상은 조회하지 않고 YIELDED로 끝냄(W2 — 더 급한 작업·
    중지 요청·호출 예산에 양보). 받은 만큼은 저장돼 있고 다음 실행이 이어서 받음(update_series는 같은 날 다시 불러도 안전)."""
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
    status, yielded = "COMPLETE", ""
    try:
        for sid, kind, code, sym in targets:
            if should_stop is not None:
                yielded = should_stop() or ""
                if yielded:
                    log(f"[준비] 양보 — {yielded}: 남은 대상 {len(targets) - len(fetch)}개는 다음 실행에서")
                    break
            lrow = None if listing is None else listing.get(code)
            if kind == "STOCK" and lrow is None:
                res = {"action": "SKIPPED", "reason": "NOT_IN_LIST"}
            elif sid in blocked:
                res = {"action": "SKIPPED", "reason": "OPEN_BACKFILL_JOB(연구 백필 작업이 맡고 있음)"}
            else:
                reg = date.fromisoformat(lrow.reg_day) if lrow is not None and lrow.reg_day else None
                try:
                    res = collector.update_series(sid, kind, code, now=now, reg_day=reg)
                except RequestStopped as exc:
                    # 요청 경계에서 막힘(예산·중지·우선 작업) — 그 종목은 저장 안 됨(종목 단위 트랜잭션), 오류로 세지 않고 양보
                    yielded = exc.reason
                    log(f"[준비] {sid} 요청 전에 멈춤 — {exc.reason}")
                    break
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
    ready: dict[str, int] = {}
    for r in rows:
        k = f"{r['status']}/{r['analysis_status']}"
        ready[k] = ready.get(k, 0) + 1
    if yielded:
        status = "YIELDED"
    elif tally.get("ERROR"):
        status = "PARTIAL"
    counts = {"fetch": tally, "readiness": ready, "changed": changed,
              "not_fetched": len(targets) - len(fetch)}
    wstore.finish_prepare(run_id, now(), status, counts, yielded)
    return {"run_id": run_id, "config_version": version, "status": status, "counts": counts,
            "calls": getattr(collector.client, "calls", None), "rows": rows, "yield_reason": yielded}
