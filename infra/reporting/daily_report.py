from __future__ import annotations

"""스윙 일일 리포트 (스윙 분리 7라운드, 2026-09-28).

체결 원장을 원천으로 하루를 정리한 Markdown 파일을 만듭니다.
`reports/daily_report_YYYY-MM-DD.md` (원자적 쓰기).

- 보유: 원장 로트 기준 수량·평균단가·진입일·보유 거래일수, 평가는 **완성된 일봉
  종가** 기준(장중 가격을 쓰지 않음 — 없으면 '—').
- 실현손익: 그날 매도 거래일 기준 매칭. 비용 전(gross)과 비용 시나리오(Base/
  Stress)를 함께 표시하고, 주문가 추정이 섞였으면 표시합니다.
- 장부 대조·미해결 주문·세션 요약을 같이 남겨, 하루 운영 상태를 한 파일로
  확인할 수 있게 합니다.

`build_daily_report()`는 순수 함수(입력 → 문자열)입니다.
"""

import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from domain.position.fill_event import FillEvent
from domain.position.position_book import ReconcileReport
from domain.position.swing_state import PositionMeta
from domain.service.lot_ledger import apply_events, net_pnl
from utils.trading_calendar import TradingCalendar

WEEKDAYS_KO = "월화수목금토일"


@dataclass
class ReportInputs:
    trade_date: date
    events: list[FillEvent]
    metas: dict[str, PositionMeta] = field(default_factory=dict)
    closes: dict[str, tuple[date, int]] = field(default_factory=dict)   # 종목 → (기준일, 종가)
    unresolved_intents: list[str] = field(default_factory=list)
    journal_symbols: list[str] = field(default_factory=list)
    reconcile: ReconcileReport | None = None
    balance_available: bool = True
    session_lines: list[str] = field(default_factory=list)
    generated_at: datetime | None = None


def _won(v: float | int | None) -> str:
    return "—" if v is None else f"{v:,.0f}"


def _signed(v: float | int | None) -> str:
    return "—" if v is None else f"{v:+,.0f}"


def build_daily_report(inp: ReportInputs, *, calendar: TradingCalendar, cost_model=None) -> str:
    d = inp.trade_date
    ledger = apply_events(inp.events)
    positions = ledger.positions()
    L: list[str] = []
    L.append(f"# 스윙 일일 리포트 — {d.isoformat()} ({WEEKDAYS_KO[d.weekday()]})")
    L.append("")
    gen = (inp.generated_at or datetime.now()).isoformat(timespec="seconds")
    L.append(f"생성 {gen} · 원천: 체결 원장(`fill_ledger.jsonl`)")
    L.append("")

    # ── 요약 ──
    cost_total = sum(p.cost_basis for p in positions.values())
    eval_total, unreal_total, missing_eval = 0, 0, []
    for sym, p in positions.items():
        c = inp.closes.get(sym)
        if c is None:
            missing_eval.append(sym)
            continue
        eval_total += c[1] * p.quantity
        unreal_total += c[1] * p.quantity - p.cost_basis
    today_matches = ledger.realized_between(d, d)
    realized_today = sum(m.gross_pnl for m in today_matches)
    realized_all = sum(m.gross_pnl for m in ledger.realized)
    est_today = any(m.is_estimate for m in today_matches)
    est_hold = any(p.includes_estimate for p in positions.values())

    L.append("## 요약")
    L.append("")
    L.append("| 항목 | 값 |")
    L.append("|---|---:|")
    L.append(f"| 보유 종목 | {len(positions)} |")
    L.append(f"| 원가 합계 | {_won(cost_total)} |")
    if positions:
        L.append(f"| 평가액 (완성 일봉 종가) | {_won(eval_total) if not missing_eval else _won(eval_total) + ' (일부 제외)'} |")
        L.append(f"| 평가손익 (비용 전) | {_signed(unreal_total)}{' (일부 제외)' if missing_eval else ''} |")
    L.append(f"| 당일 실현손익 (비용 전) | {_signed(realized_today)}{' ⚠추정가 포함' if est_today else ''} |")
    if cost_model is not None and today_matches:
        L.append(f"| 당일 실현손익 (Base 비용 차감) | {_signed(net_pnl(today_matches, cost_model, 'base'))} |")
        L.append(f"| 당일 실현손익 (Stress 비용 차감) | {_signed(net_pnl(today_matches, cost_model, 'stress'))} |")
    L.append(f"| 누적 실현손익 (비용 전) | {_signed(realized_all)} |")
    L.append("")
    notes = []
    if missing_eval:
        notes.append(f"평가 제외(완성 일봉 없음): {', '.join(missing_eval)} — `tools/update_daily_bars.py`로 갱신")
    if est_hold:
        notes.append("보유 원가에 주문가 추정치가 섞여 있음")
    if cost_model is not None:
        notes.append(f"비용 기준: {cost_model.describe()}")
    for n in notes:
        L.append(f"- {n}")
    if notes:
        L.append("")

    # ── 보유 ──
    L.append("## 보유 종목")
    L.append("")
    if not positions:
        L.append("보유 없음")
    else:
        L.append("| 종목 | 수량 | 평균단가 | 첫 진입일 | 보유 거래일 | 종가(기준일) | 평가손익 | 손절가 | 전략 | 비고 |")
        L.append("|---|---:|---:|---|---:|---:|---:|---:|---|---|")
        for sym, p in positions.items():
            m = inp.metas.get(sym)
            c = inp.closes.get(sym)
            try:
                held_days = calendar.trading_days_between(p.first_entry_date, d)
            except Exception:
                held_days = None
            unreal = c[1] * p.quantity - p.cost_basis if c else None
            flags = []
            if p.includes_estimate:
                flags.append("추정가")
            if m is None:
                flags.append("메타 없음")
            elif m.needs_review:
                flags.append("검토 필요")
            L.append(
                f"| {sym} | {p.quantity:,} | {_won(p.avg_price)} | {p.first_entry_date} | "
                f"{'—' if held_days is None else held_days} | "
                f"{_won(c[1]) + ' (' + c[0].isoformat() + ')' if c else '—'} | {_signed(unreal)} | "
                f"{_won(m.stop_price) if m and m.stop_price else '—'} | {m.strategy_id if m and m.strategy_id else '—'} | "
                f"{', '.join(flags) if flags else ''} |")
    L.append("")

    # ── 당일 체결 ──
    todays = [e for e in inp.events if e.trade_date == d]
    L.append("## 당일 체결 (원장)")
    L.append("")
    if not todays:
        L.append("없음")
    else:
        L.append("| 시각 | 종목 | 구분 | 수량 | 가격 | 가격 출처 | 주문번호 |")
        L.append("|---|---|---|---:|---:|---|---|")
        for e in sorted(todays, key=lambda e: e.occurred_at):
            L.append(f"| {e.occurred_at:%H:%M:%S} | {e.symbol} | {e.kind} | {e.quantity:,} | {e.price:,} | "
                     f"{e.price_source} | {e.order_id or '—'} |")
    L.append("")

    # ── 당일 실현 매칭 ──
    L.append("## 당일 실현 (선입선출 매칭)")
    L.append("")
    if not today_matches:
        L.append("없음")
    else:
        L.append("| 종목 | 수량 | 매수일 | 매수가 | 매도가 | 손익(비용 전) | 보유 거래일 | 추정 |")
        L.append("|---|---:|---|---:|---:|---:|---:|---|")
        for m in today_matches:
            try:
                hd = calendar.trading_days_between(m.buy_date, m.sell_date)
            except Exception:
                hd = None
            L.append(f"| {m.symbol} | {m.quantity:,} | {m.buy_date} | {m.buy_price:,} | {m.sell_price:,} | "
                     f"{_signed(m.gross_pnl)} | {'—' if hd is None else hd} | {'예' if m.is_estimate else ''} |")
    L.append("")

    # ── 운영 상태 ──
    L.append("## 운영 상태")
    L.append("")
    if inp.unresolved_intents or inp.journal_symbols:
        L.append(f"- ⚠ 미해결 주문: 주문 의도 {inp.unresolved_intents or '없음'} / 저널 {inp.journal_symbols or '없음'}"
                 " — 다음 기동 시 ERROR로 복원됨, HTS 확인 후 `commands/ack_error_<종목>.json`")
    else:
        L.append("- 미해결 주문 없음")
    if not inp.balance_available:
        L.append("- 잔고 없이 생성 — 장부 대조 생략")
    elif inp.reconcile is None:
        L.append("- 장부 대조 결과 없음")
    elif not inp.reconcile.issues:
        L.append("- 장부 대조: 원장·잔고·메타 일치")
    else:
        L.append(f"- 장부 대조: {'불일치' if not inp.reconcile.ok else '참고 사항'} {len(inp.reconcile.issues)}건")
        for line in inp.reconcile.lines():
            L.append(f"  - `{line}`")
    if inp.session_lines:
        L.append("")
        L.append("### 세션 요약")
        L.append("")
        for line in inp.session_lines:
            L.append(f"- {line}")
    L.append("")
    return "\n".join(L)


def write_report(directory: str | Path, trade_date: date, text: str) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"daily_report_{trade_date.isoformat()}.md"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=path.name + ".",
                                         suffix=".tmp", delete=False, newline="\n") as fh:
            tmp = Path(fh.name)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return path


def latest_closes(store, symbols, as_of: date) -> dict[str, tuple[date, int]]:
    """DailyBarStore에서 as_of 이하 마지막 완성 봉의 종가. 없거나 손상이면 제외."""
    out = {}
    for sym in symbols:
        try:
            bars, _ = store.load(sym)
        except Exception:
            continue
        bars = [b for b in bars if b.date <= as_of]
        if bars:
            out[sym] = (bars[-1].date, bars[-1].close)
    return out
