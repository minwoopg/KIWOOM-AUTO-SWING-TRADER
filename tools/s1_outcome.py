"""S1 신호 결과 측정 (E3, 연구용 일봉 근사 — 운영 체결 재현 아님). 조회 전용·API 호출 없음.

    python tools/s1_outcome.py --days 250
    python tools/s1_outcome.py --codes 005930,000660 --exit-slip-bps 20

BASE(운영 S1) 신호마다 다음 거래일 **시가 진입 가정** → E1(손절·10거래일 시간 청산) 근사.
신호별 독립 결과와 최대 1포지션 순차 계좌 결과를 따로 냄. 규칙·한계는 domain/research/s1_outcome.py.
결과: reports/research/s1_outcome_<끝날짜>_<실행 ID>.md · .csv (고유 ID·배타적 생성)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.research.s1_frequency import analyze  # noqa: E402
from domain.research.s1_outcome import (  # noqa: E402
    ENTERED, ENTRY_LABEL, EXIT_LABEL, OPEN_AT_END, OutcomeParams, metrics, sequential, simulate,
)
from domain.service.trade_costs import CostProfile, estimate_cost  # noqa: E402
from infra.research.report_files import create_exclusive  # noqa: E402
from infra.research.s1_scanner import INDEX_IDS, SCAN_LOOKBACK_SESSIONS  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from tools.s1_frequency import load_inputs, watch_codes  # noqa: E402

CAVEAT = ("**연구용 일봉 근사 — 운영 체결 재현이 아님.** 시가 진입 가정(운영은 09:05~09:30 현재가·호가 확인 뒤 상한 지정가, "
          "09:30 취소), 손절·시간 청산은 일봉 고가·저가·종가로 근사. 지금 저장된 수정주가·최근 종목 목록 기준이라 과거 시점의 "
          "종목 자격·상장폐지 종목(생존편향)을 재현하지 못함.")


def _fmt(v, pct=True):
    if v is None:
        return "-"
    return f"{v:+.2f}%" if pct else f"{v:+,.0f}"


def render(rid, args, params, sigs, res, seq, mi, ms, stocks) -> str:
    L = [f"# S1 신호 결과 측정 — 신호일 {args.first} ~ {args.end} ({len(stocks)}종목)", "", f"> {CAVEAT}", "",
         f"실행 ID {rid} · 비용 {params.cost_label} · 진입 슬리피지 {params.entry_slip_bps}bp(지정가 넘지 않음) · "
         f"청산 슬리피지 {params.exit_slip_bps}bp(시장가 손절·갭·시간) · 수량 1주 · 가격 원 단위 반올림 후 비용 계산", ""]
    n_entered = sum(r["status"] == ENTERED for r in res.values())
    n_closed = sum(r["status"] == ENTERED and r["exit"] != OPEN_AT_END for r in res.values())
    n_open = sum(r["status"] == ENTERED and r["exit"] == OPEN_AT_END for r in res.values())
    L += ["## 1. 신호 → 진입 → 청산 (신호별 독립)", "",
          f"종목·날짜 신호 {len(sigs)} → 진입 가정 성립 {n_entered} → 청산 완료 {n_closed} · 분석 종료까지 미청산 {n_open}", "",
          "| 진입 상태 | 건수 |", "|---|---:|"]
    for k, lab in ENTRY_LABEL.items():
        c = sum(r["status"] == k for r in res.values())
        if c:
            L.append(f"| {lab} ({k}) | {c} |")
    L += ["", "## 2. 손익 (청산 완료 기준, 1주)", "",
          "| 구분 | 건수 | 평균(비용 전) | 평균(비용 후) | 중앙값(비용 후) | 승률 | 평균 이익 | 평균 손실 | 최대 연속 손실 | "
          "손절/갭/시간 | 당일 진입·손절 불확실 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for name, m in (("신호별 독립", mi), ("순차 계좌(최대 1포지션)", ms)):
        if not m.get("n"):
            L.append(f"| {name} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        be = m["by_exit"]
        L.append(f"| {name} | {m['n']} | {_fmt(m['gross_mean'])} | {_fmt(m['net_mean'])} | {_fmt(m['net_median'])} | "
                 f"{m['win_rate']:.0f}% | {_fmt(m['avg_win'])} | {_fmt(m['avg_loss'])} | {m['max_consec_losses']} | "
                 f"{be['STOP']}/{be['STOP_GAP']}/{be['TIME']} | {m['same_day_uncertain']} |")
    if ms.get("n"):
        L += ["", f"순차 계좌: 거래 {ms['n']}건 · 수익률 합 {ms['sum_net_pct']:+.2f}%p · 최대 낙폭 {ms['mdd_pct_points']:.2f}%p "
              f"(거래 수익률을 단순 합산한 누적선 기준) · 1주 기준 손익 합 {ms['sum_net_won_1sh']:+,.0f}원, 최대 낙폭 "
              f"{ms['mdd_won_1sh']:,.0f}원"]
    L += ["", "순차 계좌 규칙: 진입일에 보유가 없을 때만(이전 청산일 < 진입일), 같은 진입일 후보는 종목코드 순으로 진입 가정이 "
          "성립하는 첫 신호, 신호마다 한 번. 일일 손실 한도·실패 주문 중단은 반영하지 않음.", "",
          "## 3. 처리 기준", "",
          "- 시가 > 지정가(상한 호가 내림): 장중 저가가 내려와도 체결로 보지 않음(미진입).",
          "- 시가 ≤ 손절 기준: 미진입(운영도 현재가 ≤ 손절 기준이면 주문 안 함).",
          "- 둘째 날부터 시가 ≤ 손절가: 갭 손절 — 시가 × (1 − 청산 슬리피지).",
          "- 진입일 저가 ≤ 손절가: 진입과 손절의 선후를 알 수 없음 → 불확실 표본, 보수적으로 손절 처리.",
          "- 시간 청산: 진입일 0일 → 10번째 거래일. 운영은 15:10 시장가, 여기서는 그날 종가 × (1 − 슬리피지) 근사. "
          "같은 날 손절 조건이면 손절 먼저.",
          "- 보유 중 봉 없는 날(거래정지 등)은 거래 불가로 건너뜀. 데이터 끝까지 남으면 미청산(마지막 종가 평가, 손익 통계 제외).", "",
          "## 4. 순차 계좌 거래", "", "| 신호일 | 종목 | 진입일 | 진입 | 청산일 | 청산 | 사유 | 비용 후 |", "|---|---|---|---:|---|---:|---|---:|"]
    for t in seq:
        L.append(f"| {t['date']} | {t['code']} {stocks[t['code']]['name']} | {t['entry_date']} | {t['entry']:,.0f} | "
                 f"{t.get('exit_date', '-')} | {t.get('exit_price', t.get('mark', 0)):,.0f} | "
                 f"{EXIT_LABEL.get(t['exit'], t['exit'])}{' ⚠불확실' if t.get('same_day_uncertain') else ''} | "
                 f"{_fmt(t.get('net_pct'))} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S1 신호 결과 측정(연구용 일봉 근사, 조회 전용)")
    p.add_argument("--db", default=str(ROOT / "data" / "research" / "research.sqlite3"))
    p.add_argument("--config", default=str(ROOT / "config" / "watchlist.yaml"))
    p.add_argument("--codes")
    p.add_argument("--days", type=int, default=250, help="신호일 범위(최근 N거래일)")
    p.add_argument("--end", help="마지막 신호일(기본: 지수 일봉 마지막 날). 청산은 그 뒤 데이터도 씀")
    p.add_argument("--entry-slip-bps", type=float, default=0.0)
    p.add_argument("--exit-slip-bps", type=float, default=10.0)
    p.add_argument("--out-dir", default=str(ROOT / "reports" / "research"))
    args = p.parse_args(argv)
    with ResearchStore(args.db, readonly=True) as rs:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()] if args.codes else watch_codes(Path(args.config))
        if not codes:
            print("[중단] 분석할 종목 없음")
            return 2
        stocks, indexes, missing, _ = load_inputs(rs, codes)
    if not stocks or len(indexes) < len(INDEX_IDS):
        print(f"[중단] 일봉 부족(backfill) — 빠진 종목 {missing}")
        return 2
    all_days = sorted(b.date for b in indexes["INDEX:KOSPI:001"])
    end = date.fromisoformat(args.end) if args.end else min(b[-1].date for b in indexes.values())
    sig_days = [d for d in all_days if d <= end][-args.days:]
    args.first, args.end = sig_days[0], end
    fr = analyze(sig_days, lambda t: all_days[:bisect.bisect_right(all_days, t)][-SCAN_LOOKBACK_SESSIONS:], stocks, indexes)
    sigs = sorted(({"date": r["date"], "code": r["code"], "stop_ref": r.get("stop_ref"), "entry_cap": r.get("entry_cap")}
                   for r in fr.rows if r["eligible_signal"] == "PASS"), key=lambda s: (s["date"], s["code"]))
    prof = CostProfile()
    params = OutcomeParams(entry_slip_bps=args.entry_slip_bps, exit_slip_bps=args.exit_slip_bps,
                           cost_fn=lambda kind, qty, price: estimate_cost(kind, qty, price, prof).amount,
                           cost_label=prof.label)
    bars = {c: {b.date: b for b in s["bars"]} for c, s in stocks.items()}
    res = {(s["date"], s["code"]): simulate(s, all_days, bars[s["code"]], params) for s in sigs}
    seq = sequential(sigs, res)
    mi, ms = metrics(res.values()), metrics(seq)
    rid, files = create_exclusive(args.out_dir, f"s1_outcome_{end.isoformat()}", (".md", ".csv"))
    files[".md"].write_text(render(rid, args, params, sigs, res, seq, mi, ms, stocks), encoding="utf-8")
    in_seq = {(t["date"], t["code"]) for t in seq}
    with files[".csv"].open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["signal_date", "code", "name", "status", "entry_date", "limit", "stop", "entry", "exit", "exit_date",
                    "exit_price", "hold_sessions", "same_day_uncertain", "missing_days", "gross_pct", "net_pct", "net_won_1sh",
                    "in_sequential"])
        for (d, c), r in sorted(res.items()):
            w.writerow([d, c, stocks[c]["name"], r["status"], r.get("entry_date"), r.get("limit"), r.get("stop"),
                        r.get("entry"), r.get("exit"), r.get("exit_date"), r.get("exit_price", r.get("mark")),
                        r.get("hold_sessions"), r.get("same_day_uncertain"), r.get("missing_days"),
                        r.get("gross_pct"), r.get("net_pct"), r.get("net"), (d, c) in in_seq])
    print(f"신호 {len(sigs)} · 진입 {sum(r['status'] == ENTERED for r in res.values())} · 독립 청산 {mi.get('n', 0)} · "
          f"순차 거래 {len(seq)}")
    if mi.get("n"):
        print(f"독립 비용 후 평균 {mi['net_mean']:+.2f}% 중앙값 {mi['net_median']:+.2f}% 승률 {mi['win_rate']:.0f}%")
    print(f"보고서 {files['.md']}\nCSV {files['.csv']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
