"""S1 신호 결과 측정 (E3, 연구용 일봉 근사 — 운영 체결 재현 아님). 조회 전용·API 호출 없음.

    python tools/s1_outcome.py --days 250
    python tools/s1_outcome.py --codes 005930,000660 --exit-slip-bps 20
    python tools/s1_outcome.py --capital 10000000 --per-trade 3000000     # 계좌 기준(G3)

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
    BIG_WIN_PCT, DATA_GAP_UNCERTAIN, ENTERED, ENTRY_LABEL, EXIT_LABEL, OPEN_AT_END, OutcomeParams, account, confirmed,
    distance_buckets, distance_caps, gap_uncertain, metrics, sequential, simulate,
)
from domain.service.trade_costs import CostProfile, estimate_cost  # noqa: E402
from infra.research.report_files import create_exclusive  # noqa: E402
from infra.research.s1_scanner import INDEX_IDS, SCAN_LOOKBACK_SESSIONS  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from tools.s1_frequency import load_inputs, watch_codes  # noqa: E402

CAVEAT = ("**연구용 일봉 근사 — 운영 체결 재현이 아님.** 시가 진입 가정(운영은 09:05~09:30 현재가·호가 확인 뒤 상한 지정가, "
          "09:30 취소), 손절·시간 청산은 일봉 고가·저가·종가로 근사. 지금 저장된 수정주가·최근 종목 목록 기준이라 과거 시점의 "
          "종목 자격·상장폐지 종목(생존편향)을 재현하지 못함.")


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _fmt(v, pct=True):
    if v is None:
        return "-"
    return f"{v:+.2f}%" if pct else f"{v:+,.0f}"


def render(rid, args, params, sigs, res, seq, mi, ms, stocks, accts) -> str:
    L = [f"# S1 신호 결과 측정 — 신호일 {args.first} ~ {args.end} ({len(stocks)}종목)", "", f"> {CAVEAT}", "",
         f"실행 ID {rid} · 비용 {params.cost_label} · 진입 슬리피지 {params.entry_slip_bps}bp(지정가 넘지 않음) · "
         f"청산 슬리피지 {params.exit_slip_bps}bp(시장가 손절·갭·시간) · 가격 원 단위 반올림 후 비용 계산", ""]
    rv = list(res.values())
    n_entered = sum(r["status"] == ENTERED for r in rv)
    n_closed = sum(r["status"] == ENTERED and r["exit"] != OPEN_AT_END for r in rv)
    n_open = sum(r["status"] == ENTERED and r["exit"] == OPEN_AT_END for r in rv)
    gaps = gap_uncertain(rv)
    L += ["## 1. 신호 → 진입 → 청산 (신호별 독립)", "",
          f"종목·날짜 신호 {len(sigs)} → 진입 가정 성립 {n_entered} → 청산 완료 {n_closed} · 분석 종료까지 미청산 {n_open} · "
          f"**확정 손익 표본 {len(confirmed(rv))}** (보유 중 중간 공백으로 불확실 {len(gaps)}건 제외)", "",
          "| 진입 상태 | 건수 |", "|---|---:|"]
    for k, lab in ENTRY_LABEL.items():
        c = sum(r["status"] == k for r in rv)
        if c:
            L.append(f"| {lab} ({k}) | {c} |")
    L += ["", "## 2. 거래 수익률 (확정 표본 · 신호 특성 비교용 — 계좌 성과 아님)", "",
          "| 구분 | 건수 | 평균(비용 전) | 평균(비용 후) | 중앙값(비용 후) | 승률 | 평균 이익 | 평균 손실 | 최대 연속 손실 | "
          "손절/갭/시간 | 당일 진입·손절 불확실 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
    for name, m in (("신호별 독립", mi), ("순차 선택(최대 1포지션)", ms)):
        if not m.get("n"):
            L.append(f"| {name} | 0 | - | - | - | - | - | - | - | - | - |")
            continue
        be = m["by_exit"]
        L.append(f"| {name} | {m['n']} | {_fmt(m['gross_mean'])} | {_fmt(m['net_mean'])} | {_fmt(m['net_median'])} | "
                 f"{m['win_rate']:.0f}% | {_fmt(m['avg_win'])} | {_fmt(m['avg_loss'])} | {m['max_consec_losses']} | "
                 f"{be['STOP']}/{be['STOP_GAP']}/{be['TIME']} | {m['same_day_uncertain']} |")
    if ms.get("n"):
        L += ["", f"참고: 순차 선택 거래 수익률의 단순 합 {ms['sum_net_pct']:+.2f}%p, 그 합산선의 최대 하락 "
              f"{ms['mdd_pct_points']:.2f}%p — **수익률을 더한 값이라 계좌 수익률·계좌 최대 낙폭이 아님.** 계좌 기준은 3장."]
    L += _account_section(accts, args)
    L += _distance_section(rv)
    L += ["", "## 5. 데이터 품질", ""]
    if gaps:
        L += [f"보유 중 그 종목 봉이 없는 세션(중간 공백 — 수집 누락일 수 있어 그날 손절 여부 불명) → 확정 손익에서 제외 {len(gaps)}건:", "",
              "| 신호일 | 종목 | 진입일 | 공백 세션 | 이후 가격 기준 결과(참고) |", "|---|---|---|---|---:|"]
        for r in sorted(gaps, key=lambda r: (r["date"], r["code"])):
            L.append(f"| {r['date']} | {r['code']} {stocks[r['code']]['name']} | {r['entry_date']} | "
                     f"{', '.join(r['gap_days'][:5])}{' …' if len(r['gap_days']) > 5 else ''} | {_fmt(r.get('net_pct'))} |")
    else:
        L.append("보유 중 중간 공백 없음(확정 표본 = 청산 완료 전체).")
    nt = [r for r in rv if r.get("no_trades_days")]
    L += ["", f"보유 중 거래 없는 봉(거래량 0)을 건너뛴 거래 {len(nt)}건 — 체결·손절이 불가능한 날로 보고 확정 표본에 포함"
          "(연구상 처리 규칙 — 그날 실제로 거래 가능·불가능했는지 확인한 것은 아님)."]
    L += ["", "## 6. 처리 기준", "",
          "- 시가 > 지정가(상한 호가 내림): 장중 저가가 내려와도 체결로 보지 않음(미진입).",
          "- 시가 ≤ 손절 기준: 미진입(운영도 현재가 ≤ 손절 기준이면 주문 안 함).",
          "- 둘째 날부터 시가 ≤ 손절가: 갭 손절 — 시가 × (1 − 청산 슬리피지).",
          "- 진입일 저가 ≤ 손절가: 진입과 손절의 선후를 알 수 없음 → 불확실 표본, 보수적으로 손절 처리.",
          "- 시간 청산: 진입일 0일 → 10번째 거래일. 운영은 15:10 시장가, 여기서는 그날 종가 × (1 − 슬리피지) 근사. "
          "같은 날 손절 조건이면 손절 먼저.",
          "- 보유 중 **거래량 0 봉**: 거래 없는 날로 건너뜀(거래정지 확정 아님 — 과거 정지 이력 원천 없음).",
          "- 보유 중 **봉 없는 세션**(그 종목 첫 봉~마지막 봉 사이): 원인 불명 공백 → DATA_GAP_UNCERTAIN, 확정 손익에서 제외. "
          "순차 선택의 보유 기간은 이후 가격으로 근사.",
          "- 그 종목 마지막 봉 이후는 데이터 끝 — 그때까지 남으면 미청산(마지막 종가 평가, 손익 통계 제외).", "",
          "## 7. 순차 선택 거래", "",
          "| 신호일 | 종목 | 진입일 | 진입 | 손절 기준 | 손절 거리 | 청산일 | 청산 | 사유 | 비용 후 |",
          "|---|---|---|---:|---:|---:|---|---:|---|---:|"]
    for t in seq:
        flag = (" ⚠당일 불확실" if t.get("same_day_uncertain") else "") + \
               (" ⚠공백 불확실(확정 제외)" if t.get("quality") == DATA_GAP_UNCERTAIN else "")
        L.append(f"| {t['date']} | {t['code']} {stocks[t['code']]['name']} | {t['entry_date']} | {t['entry']:,.0f} | "
                 f"{t['stop']:,} | {t['stop_dist_pct']:.1f}% | {t.get('exit_date', '-')} | "
                 f"{t.get('exit_price', t.get('mark', 0)):,.0f} | {EXIT_LABEL.get(t['exit'], t['exit'])}{flag} | "
                 f"{_fmt(t.get('net_pct'))} |")
    return "\n".join(L) + "\n"


def _account_section(accts, args) -> list[str]:
    L = ["", f"## 3. 가상계좌 — 관찰 {args.n_codes}종목·순차 선택, 초기 자금 {args.capital:,}원 (**실제 운영 재현 아님**)", "",
         "현금·보유 평가액(그날 종가)·매수·매도 비용을 반영한 일별 평가 곡선. 수량 기준에 따라 원화 결과가 달라지므로 두 기준을 "
         "함께 냄 — 어느 쪽이 좋아 보이는지로 고르지 않음.", "",
         "**운영과 다른 점:** 수량만 운영과 같은 1주로 맞춘 가상계좌임. 주문 허용 종목(운영은 일부 종목만), 실제 진입 시간"
         "(09:05~09:30 현재가·호가 재확인), 계좌 안전 한도·일일 손실·실패 주문 중단을 재현하지 않음 — **현재 모의계좌에서 "
         "기대할 수 있는 성과로 해석하지 않음.**", "",
         "| 수량 기준 | 거래 | 최종 평가액 | 손익 | 계좌 수익률 | 계좌 최대 낙폭 | 최대 투입 비중 | 보유일 비율 | 비용 합 | 건너뜀 |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for label, a in accts:
        sk = ", ".join(f"{t['code']} {why}" for t, why in a["skipped"][:3]) or "-"
        if len(a["skipped"]) > 3:
            sk += f" 외 {len(a['skipped']) - 3}"
        L.append(f"| {label} | {a['n_trades']}{' (미청산 1)' if a['open_at_end'] else ''} | {a['final']:,.0f} | "
                 f"{a['net_won']:+,.0f} | {a['return_pct']:+.2f}% | {a['mdd_pct']:.2f}% ({a['mdd_won']:,.0f}원) | "
                 f"{a['max_exposure_pct']:.1f}% | {a['days_in_market'] / max(a['days'], 1) * 100:.0f}% | "
                 f"{a['total_cost']:,} | {sk} |")
    if any(a["gap_uncertain"] for _, a in accts):
        L += ["", "⚠ 계좌 곡선에 중간 공백 불확실 거래가 들어 있음(그 거래의 손익은 이후 가격 기준 근사)."]
    L += ["", "계좌 최대 낙폭 = 일별 평가액이 직전 최고치에서 내려간 최대 비율. 일일 손실 한도·실패 주문 중단은 반영하지 않음."]
    return L


def _distance_section(rv) -> list[str]:
    L = ["", "## 4. 진입 시 손절 거리 (확정 표본 · 신호별 독립 · 측정용 — 운영 조건 아님)", "",
         "손절 거리 = (진입 가정가 − 손절 기준) ÷ 진입 가정가. 운영 S1의 risk_ratio_at_cap은 상한(지정가) 기준이라 CSV에 따로 "
         "(stop_dist_cap_pct). 예상 손실액 = (진입 − 손절 기준) × 수량 + 예상 비용(CSV exp_loss_1sh).", "",
         f"| 손절 거리 구간 | 건수 | 평균 | 중앙값 | 최악 | 승률 | 큰 이익(+{BIG_WIN_PCT:g}% 이상) |", "|---|---:|---:|---:|---:|---:|---:|"]
    for b in distance_buckets(rv):
        if not b["n"]:
            L.append(f"| {b['label']} | 0 | - | - | - | - | - |")
            continue
        L.append(f"| {b['label']} | {b['n']} | {_fmt(b['mean'])} | {_fmt(b['median'])} | {_fmt(b['worst'])} | "
                 f"{b['win_rate']:.0f}% | {b['big_wins']} |")
    L += ["", "손절 거리 상한을 두면(측정만):", "",
          "| 남기는 신호 | 진입 가정 신호 | 확정 표본 | 평균 | 중앙값 | 최악 | 수익률 합 | 큰 이익 남음 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for c in distance_caps(rv):
        lab = "전체(상한 없음)" if c["cap"] is None else f"손절 거리 ≤ {c['cap']:g}%"
        if not c["n"]:
            L.append(f"| {lab} | {c['entered']} | 0 | - | - | - | - | 0/{c['all_big']} |")
            continue
        L.append(f"| {lab} | {c['entered']} | {c['n']} | {_fmt(c['mean'])} | {_fmt(c['median'])} | {_fmt(c['worst'])} | "
                 f"{c['sum']:+.2f}%p | {c['big_wins']}/{c['all_big']} |")
    L += ["", "> 표본이 작아 구간별 차이는 관찰일 뿐 — 특정 손절 비율을 운영에 넣을 근거로 쓰지 않음. 순차 계좌는 상한별로 다시 "
          "고르지 않음(제외하면 다른 신호가 들어올 수 있어 별도 측정 필요)."]
    return L


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S1 신호 결과 측정(연구용 일봉 근사, 조회 전용)")
    p.add_argument("--db", default=str(ROOT / "data" / "research" / "research.sqlite3"))
    p.add_argument("--config", default=str(ROOT / "config" / "watchlist.yaml"))
    p.add_argument("--codes")
    p.add_argument("--days", type=int, default=250, help="신호일 범위(최근 N거래일)")
    p.add_argument("--end", help="마지막 신호일(기본: 지수 일봉 마지막 날). 청산은 그 뒤 데이터도 씀")
    p.add_argument("--entry-slip-bps", type=float, default=0.0)
    p.add_argument("--exit-slip-bps", type=float, default=10.0)
    p.add_argument("--capital", type=int, default=10_000_000, help="계좌 기준 초기 자금(원)")
    p.add_argument("--per-trade", type=int, help="고정 금액 기준 1회 투입(원, 기본: 초기 자금 전액) — 수량 = 내림(금액 ÷ 진입가)")
    p.add_argument("--out-dir", default=str(ROOT / "reports" / "research"))
    args = p.parse_args(argv)
    if args.capital <= 0 or (args.per_trade is not None and args.per_trade <= 0):
        print("[중단] --capital·--per-trade는 양수")
        return 2
    args.per_trade = args.per_trade or args.capital
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
    args.n_codes = len(stocks)
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
    cfn = params.cost_fn
    acct_days = [d for d in all_days if d >= sig_days[0]]
    accts = [("1주 고정(운영과 같은 수량)", account(seq, bars, acct_days, args.capital, lambda px: 1, cfn)),
             (f"고정 금액 {args.per_trade:,}원(수량 내림)",
              account(seq, bars, acct_days, args.capital, lambda px: args.per_trade // px, cfn))]
    rid, files = create_exclusive(args.out_dir, f"s1_outcome_{end.isoformat()}", (".md", ".csv"))
    files[".md"].write_text(render(rid, args, params, sigs, res, seq, mi, ms, stocks, accts), encoding="utf-8")
    in_seq = {(t["date"], t["code"]) for t in seq}
    with files[".csv"].open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["signal_date", "code", "name", "status", "entry_date", "limit", "stop", "entry", "stop_dist_pct",
                    "stop_dist_cap_pct", "exp_loss_1sh", "exit", "exit_date", "exit_price", "hold_sessions",
                    "same_day_uncertain", "quality", "gap_days", "no_trades_days", "gross_pct", "net_pct", "net_won_1sh",
                    "in_sequential"])
        for (d, c), r in sorted(res.items()):
            w.writerow([d, c, stocks[c]["name"], r["status"], r.get("entry_date"), r.get("limit"), r.get("stop"),
                        r.get("entry"), _r(r.get("stop_dist_pct")), _r(r.get("stop_dist_cap_pct")), _r(r.get("exp_loss")),
                        r.get("exit"), r.get("exit_date"), r.get("exit_price", r.get("mark")), r.get("hold_sessions"),
                        r.get("same_day_uncertain"), r.get("quality"), " ".join(r.get("gap_days") or []),
                        r.get("no_trades_days"), r.get("gross_pct"), r.get("net_pct"), r.get("net"), (d, c) in in_seq])
    print(f"신호 {len(sigs)} · 진입 {sum(r['status'] == ENTERED for r in res.values())} · 확정 표본 {mi.get('n', 0)} "
          f"(공백 불확실 {len(gap_uncertain(res.values()))}) · 순차 거래 {len(seq)}")
    for label, a in accts:
        print(f"계좌 {label}: 손익 {a['net_won']:+,.0f}원 ({a['return_pct']:+.2f}%) · 최대 낙폭 {a['mdd_pct']:.2f}%")
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
