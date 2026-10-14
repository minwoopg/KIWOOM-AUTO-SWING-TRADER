from __future__ import annotations

"""리포트·번들 조립 (스윙 분리 7라운드, 2026-09-28).

파일에 흩어진 기록(원장·상태·저널·일봉)을 모아 `infra/reporting`의 순수 함수에
넘기는 연결부입니다. 하루 수명주기 끝(`app.main.run_session`)과 CLI
(`tools/daily_report.py`, `tools/export_bundle.py`)가 같이 씁니다.
"""

import hashlib
import json
import subprocess
import zipfile
from datetime import date, datetime
from pathlib import Path

from domain.models import AccountBalance
from domain.position.position_book import ReconcileReport, reconcile
from domain.service.lot_ledger import apply_events
from infra.market_data.daily_bar_store import DailyBarStore
from infra.reporting.daily_report import ReportInputs, build_daily_report, latest_closes, write_report
from infra.reporting.masking import mask_json, mask_text
from infra.storage.fill_ledger import FillLedgerStore
from infra.storage.swing_state_store import SwingStateStore
from infra.storage.tracked_order_journal import TrackedOrderJournalStore
from utils.trading_calendar import TradingCalendar


def generate_daily_report(
    settings,
    trade_date: date,
    *,
    balance: AccountBalance | None = None,
    reconcile_report: ReconcileReport | None = None,
    session_lines: list[str] | None = None,
    calendar: TradingCalendar | None = None,
    logger=None,
    today: date | None = None,
    final_only: bool = False,
    reconcile_missing_reason: str = "",
    close_check: str = "",
    close_issues: list[str] | None = None,
) -> Path:
    """trade_date가 오늘(today, 기본 date.today())보다 이전이면 과거 재생성:
    기준일까지의 원장만 쓰고, 현재 잔고·메타·주문 상태는 쓰지 않습니다 (8-D, F6).

    final_only=True(세션 마감 보고서, 8-E R2): 넘겨받은 최종 대조 결과만 쓰고,
    잔고로 다시 대조하지 않습니다. 최종 대조가 없으면 reconcile_missing_reason을
    표시합니다(오래된 잔고로 "일치"를 만들지 않음)."""
    calendar = calendar or TradingCalendar.load()
    historical = trade_date < (today or date.today())
    events = FillLedgerStore(settings.storage.fill_ledger_file).load()
    actual_costs: dict[str, tuple] = {}
    evidence_notes: list[str] = []
    try:   # M3 A2: 주문별 체결 근거(ka10076)로 추정가를 실제 체결가로 — 원장 파일은 그대로
        from domain.service.fill_evidence import apply_fill_evidence
        from infra.storage.fill_evidence import FillEvidenceStore, evidence_path
        overlay = apply_fill_evidence(
            events, FillEvidenceStore(evidence_path(settings.storage.fill_ledger_file), read_only=True).load())
        events = overlay.events
        actual_costs = {eid: (r.commission, r.tax) for eid, r in overlay.applied.items()}
        if overlay.mismatched:
            evidence_notes.append(f"적용 안 함·충돌 {len(overlay.mismatched)}건 — {'; '.join(overlay.mismatched[:3])}")
            if logger is not None:
                logger.warning(f"[REPORT] 체결 근거 적용 안 함 {len(overlay.mismatched)}건: {overlay.mismatched[:3]}")
    except Exception as exc:
        evidence_notes.append(f"읽기 실패 — 원장 가격(추정 포함)으로 작성: {type(exc).__name__}: {exc}")
        if logger is not None:
            logger.warning(f"[REPORT] 체결 근거 읽기 실패 — 원장 가격으로 작성: {type(exc).__name__}: {exc}")
    state, _ = SwingStateStore(settings.storage.state_file).load()
    try:
        journal = sorted(TrackedOrderJournalStore(settings.storage.tracked_order_journal_file).load_all())
    except Exception as exc:
        journal = [f"(저널 읽기 실패: {type(exc).__name__})"]
    ledger = apply_events([e for e in events if e.trade_date <= trade_date])
    symbols = sorted(set(ledger.positions()) | {e.symbol for e in events if e.trade_date == trade_date})
    closes = latest_closes(DailyBarStore(settings.market_data.daily_bars_dir), symbols, trade_date)
    if historical:
        balance, reconcile_report = None, None
    elif reconcile_report is None and balance is not None and not final_only:
        reconcile_report = reconcile(apply_events(events), balance, state.positions,
                                     in_flight_symbols=set(state.unresolved_order_intents) | {j for j in journal if not j.startswith("(")})
    cost_model = None
    try:
        from domain.cost_model import load_cost_model
        cost_model = load_cost_model(use_cache=False)
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[REPORT] 비용 모델 로드 실패 — 비용 차감 표시 생략: {exc}")
    text = build_daily_report(ReportInputs(
        trade_date=trade_date, events=events, metas=state.positions, closes=closes,
        unresolved_intents=sorted(state.unresolved_order_intents), journal_symbols=journal,
        reconcile=reconcile_report, balance_available=balance is not None or reconcile_report is not None,
        session_lines=(session_lines or []) + _daily_risk_lines(settings, trade_date), generated_at=datetime.now(), historical=historical,
        reconcile_missing_reason=reconcile_missing_reason if reconcile_report is None else "",
        close_check=close_check, close_issues=list(close_issues or []),
        actual_costs=actual_costs, evidence_notes=evidence_notes,
        cost_profile=_cost_profile(settings),
    ), calendar=calendar, cost_model=cost_model)
    path = write_report(settings.storage.reports_dir, trade_date, text)
    if logger is not None:
        logger.info(f"[REPORT] 일일 리포트 저장: {path}")
    return path


# ── 번들 ──────────────────────────────────────────────────────

def _lines_for_date(path: Path, day: date, *, header: bool) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    prefix = day.isoformat()
    body = lines[1:] if header else lines
    picked = [ln for ln in body if ln.startswith(prefix)]
    return ([lines[0]] if header and lines else []) + picked


def _git_sha(root: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def export_bundle(settings, day: date, *, root: Path, out_dir: str | Path = "exports") -> Path:
    """그날 운영 기록을 민감정보를 가려 ZIP 하나로 묶음 (GPT 검토·사후 분석 공유용).

    포함: app.log(+순환 파일)의 그날 줄, trades.csv·position_lifecycle.csv의 그날 행,
    체결 원장 전체, 체결 근거 전체(fill_evidence), 지정가 취소 감사(buy_cancels), 실행 모드 표식,
    state.json, 주문 저널, 실행 기준선, 그날 리포트, 체결조회 관측의 그날 줄.
    텍스트는 mask_text(), JSON은 키 기준 mask_json()으로 가립니다.
    """
    st = settings.storage
    files: dict[str, str] = {}

    app_log = Path(st.app_log_file)
    log_lines: list[str] = []
    for p in sorted(app_log.parent.glob(app_log.name + ".*"), reverse=True) + [app_log]:
        if p.suffix == ".tmp":
            continue
        log_lines += _lines_for_date(p, day, header=False)
    files["app.log"] = "\n".join(mask_text(l) for l in log_lines)

    for name, path in (("trades.csv", st.trade_log_file), ("position_lifecycle.csv", st.position_lifecycle_log_file)):
        files[name] = "\n".join(mask_text(l) for l in _lines_for_date(Path(path), day, header=True))

    def json_lines(path: Path, *, day_filter: str | None = None) -> str:
        if not path.exists():
            return ""
        out = []
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                obj = json.loads(ln)
            except ValueError:
                out.append(mask_text(ln))
                continue
            if day_filter and not str(obj.get(day_filter, "")).startswith(day.isoformat()):
                continue
            out.append(json.dumps(mask_json(obj), ensure_ascii=False))
        return "\n".join(out)

    files["fill_ledger.jsonl"] = json_lines(Path(st.fill_ledger_file))
    # M3 A2 보완(GPT R3): 보정 가격을 번들만으로 재현할 수 있게 체결 근거 전체, 취소 감사·실행 모드 표식도 함께
    ledger_dir = Path(st.fill_ledger_file).parent
    files["fill_evidence.jsonl"] = json_lines(ledger_dir / "fill_evidence.jsonl")
    files["buy_cancels.jsonl"] = json_lines(Path(st.state_file).with_name("buy_cancels.jsonl"))
    em = ledger_dir / "execution_mode.json"
    try:
        files["execution_mode.json"] = json.dumps(mask_json(json.loads(em.read_text(encoding="utf-8"))),
                                                  ensure_ascii=False, indent=2) if em.exists() else ""
    except ValueError:
        files["execution_mode.json"] = mask_text(em.read_text(encoding="utf-8", errors="replace"))
    # C3: 전략 판단·그림자 의도(그날), 일일 위험 기록(그날), 신호 키 전체, 시장 기준 실측 원문(logs/market_basis_*.json)
    sd = Path(st.state_file).parent
    files["strategy_decisions.jsonl"] = json_lines(sd / "strategy_decisions.jsonl", day_filter="at")
    files["shadow_intents.jsonl"] = json_lines(sd / "shadow_intents.jsonl", day_filter="at")
    files["daily_risk.jsonl"] = json_lines(sd / "daily_risk.jsonl", day_filter="trade_date")
    for k in sorted(sd.glob("strategy_signal_keys_*.jsonl")):
        files[k.name] = json_lines(k)
    for mb in sorted(Path(st.app_log_file).parent.glob("market_basis_*.json")):
        try:
            files[f"market_basis/{mb.name}"] = json.dumps(mask_json(json.loads(mb.read_text(encoding="utf-8"))),
                                                         ensure_ascii=False, indent=1)
        except ValueError:
            files[f"market_basis/{mb.name}"] = mask_text(mb.read_text(encoding="utf-8", errors="replace"))
    files["order_status_observations.jsonl"] = json_lines(Path(st.order_status_observation_log_file),
                                                          day_filter="started_at")
    for name, path in (("state.json", st.state_file), ("tracked_order_journal.json", st.tracked_order_journal_file)):
        p = Path(path)
        files[name] = json.dumps(mask_json(json.loads(p.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2) \
            if p.exists() else ""
    rb = Path(st.run_baseline_log_file)
    files["run_baseline.csv"] = "\n".join(mask_text(l) for l in rb.read_text(encoding="utf-8").splitlines()) \
        if rb.exists() else ""
    rep = Path(st.reports_dir) / f"daily_report_{day.isoformat()}.md"
    files[rep.name] = rep.read_text(encoding="utf-8") if rep.exists() else ""
    ss = Path(st.reports_dir) / f"session_status_{day.isoformat()}.json"   # 8-D 마감 검증 결과
    files[ss.name] = ss.read_text(encoding="utf-8") if ss.exists() else ""

    manifest = {
        "trade_date": day.isoformat(), "created_at": datetime.now().isoformat(timespec="seconds"),
        "git_sha": _git_sha(root), "account_scope_id": settings.broker.account_scope_id,
        "note": "파일마다 읽은 시각이 조금씩 다를 수 있음(실행 중 수집). 가격 재현은 fill_ledger + fill_evidence 겹치기",
        "files": {n: {"bytes": len(t.encode("utf-8")), "sha256": hashlib.sha256(t.encode("utf-8")).hexdigest(),
                      "empty": not t} for n, t in files.items()},
    }
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zpath = out_dir / f"swing_bundle_{day.isoformat()}.zip"
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    tmp.replace(zpath)
    return zpath


def _cost_profile(settings):
    """C3: 보고서·일일 손실 한도가 같은 비용 추정 프로파일."""
    from domain.service.trade_costs import CostProfile
    st = getattr(settings, "swing_strategy", None)
    return CostProfile() if st is None else CostProfile(st.commission_rate, st.sell_tax_rate)


def _daily_risk_lines(settings, trade_date) -> list[str]:
    """C3: 그날 일일 위험 기록(시작 기준·실패 주문·중단) 요약. 기록이 없으면 빈 목록."""
    from infra.storage.daily_risk import DailyRiskStore, daily_risk_path
    p = daily_risk_path(settings.storage.state_file)
    if not p.exists():
        return []
    try:
        start, fails, halts = DailyRiskStore(p).day(trade_date.isoformat())
    except Exception as exc:                               # noqa: BLE001
        return [f"일일 위험 기록 읽기 실패: {type(exc).__name__}: {exc}"]
    out = []
    if start is not None:
        out.append(f"일일 위험 기준: {start.get('at')} 보유 {start.get('qty') or '없음'} · 시작 평가액 "
                   f"{start.get('start_equity', 0):,}원 (손익은 원장 기준 — 입출금 무관)")
    if fails:
        out.append(f"미접수·응답 불명 주문 {len(fails)}건: " + ", ".join(f"{f['symbol']} {f['side']} {f['status']}" for f in fails[:3]))
    for h in halts:
        out.append(f"신규 매수 중단(일일 위험) {h.get('at')}: {h.get('reason')}")
    return out

