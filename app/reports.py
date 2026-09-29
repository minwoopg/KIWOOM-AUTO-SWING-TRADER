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
) -> Path:
    """trade_date가 오늘(today, 기본 date.today())보다 이전이면 과거 재생성:
    기준일까지의 원장만 쓰고, 현재 잔고·메타·주문 상태는 쓰지 않습니다 (8-D, F6)."""
    calendar = calendar or TradingCalendar.load()
    historical = trade_date < (today or date.today())
    events = FillLedgerStore(settings.storage.fill_ledger_file).load()
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
    elif reconcile_report is None and balance is not None:
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
        session_lines=session_lines or [], generated_at=datetime.now(), historical=historical,
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
    체결 원장 전체, state.json, 주문 저널, 실행 기준선, 그날 리포트, 체결조회 관측의 그날 줄.
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
