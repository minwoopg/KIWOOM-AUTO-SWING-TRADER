"""S1 신호 빈도 분석 (E1) — 연구 DB에 이미 있는 일봉으로 과거 거래일마다 S1·비교 변형을 다시 계산. 조회 전용·API 호출 없음.

    python tools/s1_frequency.py                               # 관심·S1 종목(config/watchlist.yaml), 최근 250거래일
    python tools/s1_frequency.py --days 120 --codes 005930,000660
    python tools/s1_frequency.py --all-series                  # 연구 DB의 모든 종목 시계열
    python tools/s1_frequency.py --group base5=005930,005380,010120,058470,196170 --group all=watch   # 그룹 비교(E3)

결과: reports/research/s1_frequency_<끝날짜>_<실행 ID>.md(요약) · .csv(어느 변형이든 PASS인 종목·날짜) — 고유 ID·배타적 생성(E3)
연구 DB는 읽기 전용(mode=ro)으로만 엶. 일봉이 모자라면 먼저
    python tools/research_collect.py backfill --codes <코드들> --from 2024-06-01
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.research.market import RISK_ON  # noqa: E402,F401
from domain.research.s1 import Eligibility, S1Config  # noqa: E402
from domain.research.s1_frequency import VARIANT_LABEL, VARIANTS, analyze  # noqa: E402
from domain.research.s1_funnel import funnel, funnel_line  # noqa: E402
from domain.research.s1_stats import monthly, period_summary, pullback_reasons  # noqa: E402
from infra.research.report_files import create_exclusive  # noqa: E402
from domain.research.universe import STOCK_MARKETS  # noqa: E402
from infra.research.s1_scanner import INDEX_IDS, SCAN_LOOKBACK_SESSIONS  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

CAVEAT = ("연구용 근사 — 지금 저장된 수정주가 기준(그날 실제로 보였던 값·20:30 완성 기준이 아님), 종목 자격은 최근 종목 목록 기준"
          "(과거 관리·경고 상태 아님). 신호 수만 셈 — 진입·체결·수익은 계산하지 않음. 비교 변형은 측정용이며 운영 조건이 아님.")


def watch_codes(config: Path) -> list[str]:
    from domain.watchlist.config import parse_text
    if not config.exists():
        return []
    raw, err = parse_text(config.read_text(encoding="utf-8"))
    if err or not isinstance(raw, dict):
        return []
    out = []
    for it in raw.get("symbols") or []:
        i = (it or {}).get("interest") or {}
        if i.get("enabled") and i.get("s1_analysis"):
            out.append(str(it["code"]).strip())
    return out


def load_inputs(rs: ResearchStore, codes: list[str]):
    snap = rs.latest_snapshot()
    rows = {r["code"]: r for r in rs.snapshot_rows(snap["snapshot_id"])} if snap else {}
    stocks, missing = {}, {}
    for c in codes:
        r = rows.get(c)
        try:
            bars = rs.research_series(f"STOCK:{c}").bars
        except KeyError:
            missing[c] = "연구 DB에 일봉 없음 — backfill 필요"
            continue
        market, index_id = STOCK_MARKETS.get(str(r["market_code"]) if r else "", (None, None))
        if r is None or index_id is None:
            missing[c] = "종목 목록에 없음·시장 미상"
            continue
        elig = Eligibility(r["security_type"], "OK" if not r["risk_flags"] else "FLAGGED", ",".join(r["risk_flags"]),
                           market_index_id=index_id)
        stocks[c] = {"bars": bars, "eligibility": elig, "index_id": index_id, "name": r["name"], "market": market}
    indexes = {}
    for iid in INDEX_IDS:
        try:
            indexes[iid] = rs.research_series(iid).bars
        except KeyError:
            pass
    return stocks, indexes, missing, snap


def render(res, f_base, stocks, missing, dates, args, snap) -> str:
    L = [f"# S1 신호 빈도 분석 — {dates[0]} ~ {dates[-1]} ({len(dates)}거래일 · {len(stocks)}종목)", "",
         f"> {CAVEAT}", "", f"생성 {datetime.now().isoformat(timespec='seconds')} · 종목 목록 스냅숏 "
         f"{snap['snapshot_id'] if snap else '-'} · 기준 S1 {S1Config().config_hash()}", ""]
    L += [f"거래일 기준: KOSPI 지수 일봉 날짜(운영 달력은 운영 연도만 다룸). {getattr(args, 'cal_note', '') or '달력 연도 구간 일치'}",
          "", "용어: **종목·날짜 신호 수** = BASE 최종 PASS인 (종목, 날짜) 수 · **7일 묶음(탐색용)** = 같은 종목 PASS가 7일 안에 "
          "이어지면 1로 셈(진입 수 아님) · **운영 규칙 적용 진입 수**는 `tools/s1_outcome.py` 순차 결과. 종목 수에 비례해 "
          "거래 횟수를 추정하지 않음.", ""]
    L += _compare_section(res, dates, args)
    L += ["", "## 1. 단계별 건수 (운영 S1, 종목·날짜 단위, 전체 종목)", "", funnel_line(f_base), "",
          "| 주요 탈락 조건 | 건수 |", "|---|---:|"]
    L += [f"| {k} | {v:,} |" for k, v in list(f_base["fail_checks"].items())[:12]]
    L += ["", "> 탈락 조건은 한 줄에서 여러 개가 동시에 실패할 수 있음(중복) — 순위만으로 '이 조건을 풀면 신호가 몇 건 는다'고 "
          "판단하지 않음. 효과는 2장 비교 변형으로만 봄.", "", "눌림(PULLBACK) 실패 세부 사유:", "",
          "| 사유 | 건수 |", "|---|---:|"]
    L += [f"| {k} | {v:,} |" for k, v in pullback_reasons(res.rows).items()]
    L += ["", "## 2. 비교 변형 (측정용 — 운영 조건 아님)", "", "| 변형 | 최종 PASS(종목·날짜) | PASS 종목 수 | PASS 있는 날 |",
          "|---|---:|---:|---:|"]
    for v in VARIANTS:
        pr = [r for r in res.rows if r["variants"].get(v) == "PASS"]
        L.append(f"| {v} — {VARIANT_LABEL[v]} | {len(pr):,} | {len({r['code'] for r in pr})} | {len({r['date'] for r in pr})} |")
    L += ["", "## 3. 시장 판정 분포 (지수별 거래일 수)", "", "| 지수 | RISK_ON | MIXED | RISK_OFF | UNKNOWN |", "|---|---:|---:|---:|---:|"]
    for iid in INDEX_IDS:
        sts = [reg.get(iid) for reg in res.regimes.values() if reg.get(iid)]
        L.append(f"| {iid} | " + " | ".join(str(sts.count(s)) for s in ("RISK_ON", "MIXED", "RISK_OFF", "UNKNOWN")) + " |")
    L += ["", "## 4. 종목별", "", "| 코드 | 이름 | 데이터 준비 일 | 패턴 PASS 일 | 최종 PASS 일 | 시장 제외 시 PASS 일 |",
          "|---|---|---:|---:|---:|---:|"]
    for c, s in sorted(stocks.items()):
        rr = [r for r in res.rows if r["code"] == c]
        L.append(f"| {c} | {s['name']} | {sum(r['data_status'] == 'OK' for r in rr)} | "
                 f"{sum(r['pattern_pass'] == 'PASS' for r in rr)} | {sum(r['eligible_signal'] == 'PASS' for r in rr)} | "
                 f"{sum(r['variants'].get('NO_MARKET') == 'PASS' for r in rr)} |")
    L += ["", "## 5. 월별 (그룹별)", ""]
    for name, codes in args.groups:
        rr = [r for r in res.rows if r["code"] in codes]
        L += [f"### {name} ({len(codes)}종목)", "", "| 월 | 분석 표본 | 데이터 준비 | 데이터 보류(사유) | 패턴 PASS | 최종 PASS | 신호 날 수 |",
              "|---|---:|---:|---|---:|---:|---:|"]
        for m in monthly(rr):
            holds = ", ".join(f"{k} {v}" for k, v in sorted(m["holds"].items())) or "-"
            L.append(f"| {m['month']} | {m['rows']} | {m['ready']} | {holds} | {m['pattern']} | {m['final']} | {m['signal_days']} |")
        L.append("")
    if missing or res.skipped:
        L += ["", "## 6. 분석 못 한 종목", ""] + [f"- {c}: {w}" for c, w in {**missing, **res.skipped}.items()]
    return "\n".join(L) + "\n"


def _compare_section(res, dates, args) -> list[str]:
    recent = [d for d in dates if d >= args.recent_from]
    L = [f"## 0. 그룹·기간 비교 (전체 {dates[0]}~{dates[-1]} / 최근 {args.recent_from}~)", "",
         "| 그룹 | 기간 | 거래일 | 분석 표본 | 데이터 준비 | 패턴 PASS | 종목·날짜 신호 | 신호 날 | 신호 종목 | 7일 묶음(탐색용) | 가장 긴 무신호 |",
         "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for name, codes in args.groups:
        rr = [r for r in res.rows if r["code"] in codes]
        for label, ds in (("전체", dates), ("최근", recent)):
            if not ds:
                continue
            s = period_summary(rr, ds)
            g = s["longest_gap"]
            gap = f"{g['days']}거래일 ({g['start']}~{g['end']}{', 진행 중' if g['ongoing'] else ''})" if g["days"] else "-"
            L.append(f"| {name} | {label} | {s['days']} | {s['rows']} | {s['ready']} | {s['pattern']} | {s['final']} | "
                     f"{s['signal_days']} | {s['symbols']} | {s['episodes_7d']} | {gap} |")
    return L


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S1 신호 빈도 분석(조회 전용)")
    p.add_argument("--db", default=str(ROOT / "data" / "research" / "research.sqlite3"))
    p.add_argument("--config", default=str(ROOT / "config" / "watchlist.yaml"))
    p.add_argument("--codes")
    p.add_argument("--all-series", action="store_true")
    p.add_argument("--days", type=int, default=250)
    p.add_argument("--end", help="YYYY-MM-DD (기본: 지수 일봉의 마지막 날)")
    p.add_argument("--out-dir", default=str(ROOT / "reports" / "research"))
    p.add_argument("--group", action="append", default=[],
                   help="비교 그룹 NAME=코드,코드 또는 NAME=watch(관심·S1 종목) — 여러 번")
    p.add_argument("--recent-from", default="2026-07-01", help="최근 구간 시작(YYYY-MM-DD)")
    args = p.parse_args(argv)
    args.recent_from = date.fromisoformat(args.recent_from)
    cal = TradingCalendar.load()
    with ResearchStore(args.db, readonly=True) as rs:
        groups = []
        for g in args.group:
            name, _, spec = g.partition("=")
            gc = watch_codes(Path(args.config)) if spec.strip() == "watch" else \
                [c.strip() for c in spec.split(",") if c.strip()]
            if not name.strip() or not gc:
                print(f"[중단] --group 형식 NAME=코드,코드 — {g!r}")
                return 2
            groups.append((name.strip(), gc))
        if groups:
            codes = sorted({c for _, gc in groups for c in gc})
        elif args.codes:
            codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        elif args.all_series:
            codes = [s.split(":", 1)[1] for s in rs.series_ids("STOCK")]
        else:
            codes = watch_codes(Path(args.config))
        if not codes:
            print("[중단] 분석할 종목 없음 — --codes 또는 watchlist의 관심·S1 종목")
            return 2
        stocks, indexes, missing, snap = load_inputs(rs, codes)
    if not stocks or len(indexes) < len(INDEX_IDS):
        print(f"[중단] 종목 {len(stocks)}·지수 {len(indexes)} — 연구 DB 일봉 부족(backfill). 빠진 종목: {missing}")
        return 2
    end = date.fromisoformat(args.end) if args.end else min(b[-1].date for b in indexes.values())
    # 거래일 = KOSPI 지수 일봉 날짜. 운영 달력(config/krx_calendar.yaml)은 운영 연도만 다뤄 과거 분석에 쓸 수 없음 —
    # 달력이 다루는 연도에서는 두 목록이 같은지 확인해 다르면 보고서에 표시.
    all_days = sorted(b.date for b in indexes["INDEX:KOSPI:001"] if b.date <= end)
    cal_note = ""
    cov = [d for d in all_days if d.year in cal.covered_years]
    if cov:
        cal_days = cal.trading_days_in_range(cov[0], cov[-1])
        if cal_days != cov:
            cal_note = (f"⚠ 달력과 지수 일봉 날짜가 다름({len(set(cal_days) ^ set(cov))}일) — "
                        f"{sorted(set(cal_days) ^ set(cov))[:5]}")
    dates = all_days[-args.days:]
    import bisect
    res = analyze(dates, lambda t: all_days[:bisect.bisect_right(all_days, t)][-SCAN_LOOKBACK_SESSIONS:],
                  stocks, indexes)
    args.cal_note = cal_note
    args.groups = [(n, [c for c in gc if c in stocks]) for n, gc in groups] or [("전체", sorted(stocks))]
    f_base = funnel(res.rows)
    rid, files = create_exclusive(args.out_dir, f"s1_frequency_{end.isoformat()}", (".md", ".csv"))
    md, cp = files[".md"], files[".csv"]
    md.write_text(f"<!-- 실행 ID {rid} -->\n" + render(res, f_base, stocks, missing, dates, args, snap), encoding="utf-8")
    with cp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        # 가격 열은 BASE(운영 S1) 계산값 — 변형별 값 아님
        w.writerow(["date", "code", "name", "market_state", *VARIANTS, "base_stop_ref", "base_entry_cap",
                    "base_pullback_days"])
        for r in res.rows:
            if any(r["variants"].get(v) == "PASS" for v in VARIANTS):
                w.writerow([r["date"], r["code"], stocks[r["code"]]["name"], r.get("market_state"),
                            *[r["variants"].get(v, "") for v in VARIANTS], r.get("stop_ref"), r.get("entry_cap"),
                            r.get("pullback_days")])
    print(funnel_line(f_base))
    for v in VARIANTS:
        print(f"{v:16s} 최종 PASS {sum(r['variants'].get(v) == 'PASS' for r in res.rows):,}")
    print(f"보고서 {md}\nCSV {cp}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
