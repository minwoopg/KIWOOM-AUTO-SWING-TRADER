from __future__ import annotations

"""A4-A: S1_BASE 앞으로의 신호 관찰 스캔 (주문 없음, 조회만).

규칙 (GPT A2 종합 검토 → A4-A 지시)
- S1_BASE(s1_pullback_v0.1) 조건·기준값을 그대로 씁니다. 주문 경로와 연결하지 않습니다.
- **스캔 시각(scan_at)의 입력만** 씁니다.
  * 신호일 t = scan_at에 완성된 가장 최근 거래일(정규장 종료 + 160분 — 잠정 완성 기준, 수집기와 같은 값).
  * 종목·지수는 `research_series(sid, as_of=scan_at)` — 그 시각에 쓸 수 있던 값·revision만.
  * integrity ≠ OK, time_proof ≠ OK, revision 없음, t의 봉 없음(오래된 봉) → 그 종목은 UNKNOWN 보류(데이터 보류).
  * 지수가 위 조건에 걸리면 그 지수를 쓰는 종목은 지수 없이 평가 → RS·시장 판정 UNKNOWN(지수 보류).
  * 종목 목록은 scan_at까지 **수집이 끝난** 스냅숏(`snapshot_as_of`) — latest_snapshot 아님. 그 스냅숏이 t 장 마감
    전에 관측된 것이면 현재 위험 상태를 모르는 것으로 보고 RISK_STATUS UNKNOWN(스냅숏 보류).
    분류는 스냅숏 원래 필드를 **현재 정책(u2)으로 다시 분류**해 씁니다(정책 버전 기록).
  * 세션 목록은 거래소 달력(config/krx_calendar.yaml)에서만 — 다루지 않는 해는 INSUFFICIENT_SESSIONS(보수적).
- 후보(PASS)와 탈락(FAIL)·보류(UNKNOWN)를 모두 조건별 사유·참고 손절가·진입 상한과 함께 저장합니다.
  입력 해시·revision·조정 기준·스냅숏 ID·계산 버전은 증거 필드(신호 ID에는 넣지 않음).
- 같은 run_key(신호일·스캔 시각·전략·설정·분류 정책)는 한 번만 완료로 저장 — 다시 돌리면 건너뜀(verify면 다시
  계산해 저장값과 같은지 비교만).
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from domain.research import features as F
from domain.research import market as M
from domain.research.s1 import STRATEGY_ID, STRATEGY_VERSION, Eligibility, S1Config, evaluate_s1
from domain.research.series import SeriesView
from domain.research.types import Tri
from domain.research.universe import COMMON, UniversePolicy, classify_row
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE
from infra.research.scan_store import ABORTED, ScanStore
from infra.research.store import NO_REVISION, ResearchStore
from utils.trading_calendar import CalendarCoverageError, TradingCalendar

SCAN_LOOKBACK_SESSIONS = 300          # S1 최대 창 160 + 관찰값 여유(252일 지표가 생겨도 충분)
INDEX_IDS = ("INDEX:KOSPI:001", "INDEX:KOSDAQ:101")
OK = "OK"


class ScanError(RuntimeError):
    """스캔 자체를 할 수 없음(달력·스냅숏 없음 등)."""


def expected_session(calendar: TradingCalendar, scan_at: datetime,
                     after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE) -> date:
    """scan_at에 완성된(정규장 종료 + after_close 경과) 가장 최근 거래일."""
    d = scan_at.date()
    try:
        st = calendar.session_times(d)
        if st is not None and scan_at >= datetime.combine(d, st.close) + after_close:
            return d
        return calendar.previous_trading_day(d)
    except CalendarCoverageError as exc:
        raise ScanError(f"달력이 {d.year}년을 다루지 않음 — 신호일을 정할 수 없음: {exc}") from exc


def data_status(rs, t: date) -> str:
    if rs is None:
        return "NO_SERIES"
    if rs.revision is None or rs.integrity == NO_REVISION:
        return "NO_REVISION"
    if rs.integrity != OK:
        return f"INTEGRITY:{rs.integrity}"
    if rs.time_proof != OK:
        return f"UNPROVEN:{rs.unproven_bars}"
    if not rs.bars or rs.bars[-1].date < t:
        return f"STALE:last={rs.bars[-1].date if rs.bars else None}"
    if t not in rs.available_at:
        return "STALE:no_bar_at_t"
    return OK


def _bars_digest(bars) -> str:
    raw = json.dumps([(b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume, b.trade_value) for b in bars])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_raw(row: dict) -> dict:
    """universe_row 열 → ka10099 원래 필드 이름 (현재 정책으로 다시 분류하기 위해)."""
    return {"code": row["code"], "name": row["name"], "marketCode": row["market_code"],
            "auditInfo": row["audit_info"], "state": row["state"], "orderWarning": row["order_warning"],
            "companyClassName": row["company_class"], "regDay": (row["reg_day"] or "").replace("-", ""),
            "listCount": row["list_count"], "lastPrice": row["last_price"]}


@dataclass
class IndexInput:
    series_id: str
    status: str
    view: SeriesView
    digest: str
    evidence: dict
    regime: M.MarketRegime


class S1Scanner:
    def __init__(self, rstore: ResearchStore, sstore: ScanStore, calendar: TradingCalendar, *,
                 cfg: S1Config | None = None, policy: UniversePolicy | None = None,
                 after_close: timedelta = BAR_COMPLETE_AFTER_CLOSE, lookback: int = SCAN_LOOKBACK_SESSIONS,
                 log: Callable[[str], None] | None = None) -> None:
        self.rstore, self.sstore, self.cal = rstore, sstore, calendar
        self.cfg = cfg or S1Config()
        self.policy = policy or UniversePolicy()
        self.after_close = after_close
        self.lookback = lookback
        self.log = log or (lambda m: None)
        self.strategy = f"{STRATEGY_ID}_{STRATEGY_VERSION}"
        self.config_hash = self.cfg.config_hash()

    def run_key(self, scan_at: datetime, signal_date: date) -> str:
        return (f"{signal_date.isoformat()}|{scan_at.replace(microsecond=0).isoformat(timespec='seconds')}|"
                f"{self.strategy}|{self.config_hash}|{self.policy.policy_version}")

    # ── 계산(읽기만) ─────────────────────────────────────────
    def _evidence(self, rs) -> dict:
        if rs is None:
            return {"series": None}
        used = rs.bars
        return {"revision": rs.revision, "base_dt": rs.basis.base_dt if rs.basis else None,
                "upd_stkpc_tp": rs.basis.upd_stkpc_tp if rs.basis else None, "integrity": rs.integrity,
                "integrity_detail": rs.integrity_detail[:200], "time_proof": rs.time_proof,
                "first_bar": used[0].date.isoformat() if used else None,
                "last_bar": used[-1].date.isoformat() if used else None,
                "last_available_at": max(rs.available_at.values()).isoformat() if rs.available_at else None}

    def _load(self, sid: str, scan_at: datetime, start: date):
        try:
            return self.rstore.research_series(sid, as_of=scan_at, start=start)
        except KeyError:
            return None

    def compute(self, scan_at: datetime) -> dict:
        """스캔 계산(저장 안 함). 같은 scan_at·같은 DB 상태면 결과가 항상 같습니다."""
        t = expected_session(self.cal, scan_at, self.after_close)
        cov_start = date(min(self.cal.covered_years), 1, 1)
        sessions = self.cal.trading_days_in_range(cov_start, t)[-self.lookback:]
        start = sessions[0]
        close_t = datetime.combine(t, self.cal.session_times(t).close)
        snap = self.rstore.snapshot_as_of(scan_at)
        if snap is None:
            raise ScanError(f"{scan_at}까지 수집이 끝난 종목 목록 스냅숏이 없음 — universe(또는 update) 먼저")
        snap_status = OK if snap["observed_at"] >= close_t.isoformat(timespec="seconds") else \
            f"BEFORE_SESSION_CLOSE:observed_at={snap['observed_at']}"
        try:
            nxt = self.cal.next_trading_day(t)
            next_open = datetime.combine(nxt, self.cal.session_times(nxt).open)
            actionable = int(scan_at < next_open)
        except CalendarCoverageError:
            next_open, actionable = None, None

        indexes: dict[str, IndexInput] = {}
        for sid in INDEX_IDS:
            rs = self._load(sid, scan_at, start)
            st = data_status(rs, t)
            bars = rs.bars if st == OK else []
            view = SeriesView(bars, sessions, t, source_id=sid)
            indexes[sid] = IndexInput(sid, st, view, _bars_digest(bars), self._evidence(rs),
                                      M.classify_market(view))

        rows = self.rstore.snapshot_rows(snap["snapshot_id"])
        evals = []
        for row in rows:
            rec = classify_row(_row_to_raw(row), self.policy)
            if not rec.collect:
                continue
            sid = f"STOCK:{rec.code}"
            idx = indexes.get(rec.index_id)
            idx_status = idx.status if idx else "NO_INDEX_FOR_MARKET"
            rs = self._load(sid, scan_at, start)
            ds = data_status(rs, t)
            risk = None if snap_status != OK else ("OK" if not rec.risk_flags else "FLAGGED")
            risk_detail = (snap_status if snap_status != OK else ",".join(rec.risk_flags))
            elig = Eligibility(rec.security_type, risk, risk_detail, market_index_id=rec.index_id)
            stock_bars = rs.bars if (rs is not None and ds == OK) else []
            input_hash = hashlib.sha256(json.dumps({
                "stock": _bars_digest(stock_bars), "index": idx.digest if idx else None,
                "eligibility": [elig.security_type, elig.risk_status, elig.risk_detail, elig.market_index_id],
                "sessions": [sessions[0].isoformat(), sessions[-1].isoformat(), len(sessions)],
                "config": self.config_hash, "strategy": self.strategy, "feature": F.FEATURE_VERSION,
                "market": M.MARKET_VERSION, "policy": self.policy.policy_version}, sort_keys=True).encode()).hexdigest()
            # 종목별 증거만 여기에 — 지수 revision·스냅숏·세션·버전·스캔 시각은 실행 context에 한 번
            evidence = {"stock": self._evidence(rs), "index_id": rec.index_id,
                        "index_revision": idx.evidence.get("revision") if idx else None,
                        "snapshot_id": snap["snapshot_id"], "security_type": rec.security_type,
                        "risk_flags": list(rec.risk_flags)}
            if ds != OK:
                result, tri = None, {k: Tri.UNKNOWN.value for k in
                                     ("eligible_signal", "pattern_pass", "eligibility_pass", "market_pass",
                                      "stop_valid")}
                nt_hold = 0
            else:
                view = SeriesView(stock_bars, sessions, t, source_id=sid)
                res = evaluate_s1(rec.code, view, idx.view if idx else SeriesView([], sessions, t), None, elig,
                                  self.cfg)
                result = res.to_dict()
                result.pop("market", None)        # 시장 판정은 지수별로 같음 → 실행 context(indexes.regime)에 한 번
                tri = {k: result[k] for k in ("eligible_signal", "pattern_pass", "eligibility_pass", "market_pass",
                                              "stop_valid")}
                nt_hold = int(any(c["result"] == Tri.UNKNOWN.value and "NO_TRADES" in (c["detail"] or "")
                                  for c in result["checks"]))
            evals.append({"symbol": rec.code, "name": rec.name, "market": rec.market, "series_id": sid,
                          "signal_date": t.isoformat(), **tri, "data_status": ds, "index_status": idx_status,
                          "snapshot_status": snap_status, "no_trades_hold": nt_hold,
                          "final": int(ds == OK and idx_status == OK and snap_status == OK),
                          "actionable": actionable, "input_hash": input_hash, "result": result,
                          "evidence": evidence})
        context = {"signal_date": t.isoformat(), "scan_at": scan_at.isoformat(timespec="seconds"),
                   "close_t": close_t.isoformat(timespec="seconds"),
                   "next_open": next_open.isoformat(timespec="seconds") if next_open else None,
                   "sessions": {"first": sessions[0].isoformat(), "last": sessions[-1].isoformat(),
                                "count": len(sessions)},
                   "after_close_min": int(self.after_close.total_seconds() // 60),
                   "completion_rule": "PROVISIONAL(정규장 종료 + after_close — 실측 확인 전 잠정 기준)",
                   "indexes": {sid: {"status": ix.status, "regime": ix.regime.to_dict(), "evidence": ix.evidence}
                               for sid, ix in indexes.items()},
                   "snapshot": {"snapshot_id": snap["snapshot_id"], "observed_at": snap["observed_at"],
                                "stored_policy": snap["policy_version"], "status": snap_status},
                   "applied_policy": self.policy.policy_version, "strategy": self.strategy,
                   "config_hash": self.config_hash, "config": asdict(self.cfg),
                   "feature_version": F.FEATURE_VERSION, "market_version": M.MARKET_VERSION}
        return {"signal_date": t, "snapshot": snap, "snapshot_status": snap_status, "evals": evals,
                "context": context}

    # ── 실행(저장) ───────────────────────────────────────────
    def run(self, scan_at: datetime, *, now: Callable[[], datetime], verify: bool = False,
            before_commit=None) -> dict:
        t = expected_session(self.cal, scan_at, self.after_close)
        key = self.run_key(scan_at, t)
        done = self.sstore.complete_run(key)
        if done is not None:
            out = {"status": "SKIPPED_ALREADY_COMPLETE", "run_id": done["run_id"], "run_key": key,
                   "counts": done["counts"], "report_path": done["report_path"]}
            if verify:
                out["verify"] = self.verify(done["run_id"], scan_at)
            return out
        run_id, aborted = self.sstore.begin_run(
            run_key=key, scan_at=scan_at, signal_date=t.isoformat(), strategy=self.strategy,
            config_hash=self.config_hash, universe_policy=self.policy.policy_version,
            feature_version=F.FEATURE_VERSION, market_version=M.MARKET_VERSION,
            after_close_min=int(self.after_close.total_seconds() // 60), started_at=now())
        for rid in aborted:
            self.log(f"[SCAN] 이전에 끝나지 않은 실행 {rid} → ABORTED")
        try:
            comp = self.compute(scan_at)
            counts = summarize_evals(comp["evals"], comp["context"])
            obs = self.sstore.finish_run(run_id=run_id, evals=comp["evals"], context=comp["context"], counts=counts,
                                         snapshot=comp["snapshot"], snapshot_status=comp["snapshot_status"],
                                         scan_at=scan_at, now=now(), strategy=self.strategy,
                                         config_hash=self.config_hash, before_commit=before_commit)
        except Exception as exc:
            self.sstore.fail_run(run_id, f"{type(exc).__name__}: {exc}", now())
            raise
        except BaseException:
            try:
                self.sstore.fail_run(run_id, "중단됨(KeyboardInterrupt 등)", now(), status=ABORTED)
            finally:
                raise
        counts["observations"] = obs
        return {"status": "COMPLETE", "run_id": run_id, "run_key": key, "counts": counts,
                "context": comp["context"], "evals": comp["evals"], "aborted_previous": aborted}

    def verify(self, run_id: str, scan_at: datetime) -> dict:
        """저장된 실행을 같은 scan_at으로 다시 계산해 종목별 판정·입력 해시·결과가 같은지 비교(저장 안 함)."""
        stored = {e["symbol"]: e for e in self.sstore.evals(run_id)}
        comp = {e["symbol"]: e for e in self.compute(scan_at)["evals"]}
        keys = ("eligible_signal", "data_status", "index_status", "snapshot_status", "input_hash", "final")
        diff = sorted(s for s in set(stored) | set(comp)
                      if s not in stored or s not in comp
                      or any(stored[s][k] != comp[s][k] for k in keys)
                      or stored[s]["result"] != json.loads(json.dumps(comp[s]["result"], sort_keys=True)))
        return {"identical": not diff, "symbols": len(comp), "diff": diff[:20], "diff_count": len(diff)}


def summarize_evals(evals: list[dict], context: dict) -> dict:
    """보고서용 집계 (매매 수익 아님 — 신호·보류 개수)."""
    def inc(d, k, n=1):
        d[k] = d.get(k, 0) + n

    by_signal, by_market, data_holds, fail_checks, fail_groups, unknown_reasons = {}, {}, {}, {}, {}, {}
    index_holds, nt_hold, snapshot_hold = {}, 0, 0
    passes = []
    for e in evals:
        inc(by_signal, e["eligible_signal"])
        inc(by_market.setdefault(e["market"] or "?", {}), e["eligible_signal"])
        if e["data_status"] != OK:
            inc(data_holds, e["data_status"].split(":")[0])
            continue
        if e["index_status"] != OK:
            inc(index_holds, e["evidence"]["index_id"] or "?")
        if e["snapshot_status"] != OK:
            snapshot_hold += 1
        nt_hold += e["no_trades_hold"]
        r = e["result"]
        for g in ("pattern_pass", "eligibility_pass", "market_pass", "stop_valid"):
            if r[g] == Tri.FAIL.value:
                inc(fail_groups, g)
        for c in r["checks"]:
            if c["result"] == Tri.FAIL.value:
                inc(fail_checks, c["name"])
            elif c["result"] == Tri.UNKNOWN.value and e["eligible_signal"] == Tri.UNKNOWN.value:
                reason = (c["detail"] or "UNKNOWN").split(":")[0].split("(")[0]
                inc(unknown_reasons, f"{c['name']}:{reason}")
        if e["eligible_signal"] == Tri.PASS.value:
            passes.append(e["symbol"])
    return {"universe": len(evals), "by_signal": by_signal, "signals": len(passes),
            "distinct_symbols": len(set(passes)), "by_market": by_market, "data_holds": data_holds,
            "data_hold_total": sum(data_holds.values()), "index_holds": index_holds,
            "snapshot_hold": snapshot_hold, "no_trades_hold": nt_hold, "fail_groups": fail_groups,
            "fail_checks": dict(sorted(fail_checks.items(), key=lambda kv: (-kv[1], kv[0]))),
            "unknown_reasons": dict(sorted(unknown_reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            "final": sum(e["final"] for e in evals),
            "index_status": {k: v["status"] for k, v in context["indexes"].items()},
            "market_regime": {k: v["regime"]["state"] for k, v in context["indexes"].items()}}
