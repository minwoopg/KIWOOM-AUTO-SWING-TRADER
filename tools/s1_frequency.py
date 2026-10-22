"""S1 신호 빈도 분석 (E1) — 연구 DB에 이미 있는 일봉으로 과거 거래일마다 S1·비교 변형을 다시 계산. 조회 전용·API 호출 없음.

    python tools/s1_frequency.py                               # 관심·S1 종목(config/watchlist.yaml), 최근 250거래일
    python tools/s1_frequency.py --days 120 --codes 005930,000660
    python tools/s1_frequency.py --all-series                  # 연구 DB의 모든 종목 시계열

결과: reports/research/s1_frequency_<끝날짜>.md(요약) · .csv(어느 변형이든 PASS인 종목·날짜)
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
          "", "## 1. 단계별 건수 (운영 S1, 종목·날짜 단위)", "", funnel_line(f_base), "",
          "| 주요 탈락 조건 | 건수 |", "|---|---:|"]
    L += [f"| {k} | {v:,} |" for k, v in list(f_base["fail_checks"].items())[:12]]
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
    if missing or res.skipped:
        L += ["", "## 5. 분석 못 한 종목", ""] + [f"- {c}: {w}" for c, w in {**missing, **res.skipped}.items()]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="S1 신호 빈도 분석(조회 전용)")
    p.add_argument("--db", default=str(ROOT / "data" / "research" / "research.sqlite3"))
    p.add_argument("--config", default=str(ROOT / "config" / "watchlist.yaml"))
    p.add_argument("--codes")
    p.add_argument("--all-series", action="store_true")
    p.add_argument("--days", type=int, default=250)
    p.add_argument("--end", help="YYYY-MM-DD (기본: 지수 일봉의 마지막 날)")
    p.add_argument("--out-dir", default=str(ROOT / "reports" / "research"))
    args = p.parse_args(argv)
    cal = TradingCalendar.load()
    with ResearchStore(args.db, readonly=True) as rs:
        if args.codes:
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
    f_base = funnel(res.rows)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = out / f"s1_frequency_{end.isoformat()}.md"
    if md.exists():          # E2: 같은 끝날짜로 다시 돌려도 이전 결과를 덮어쓰지 않음(비교 보존)
        md = out / f"s1_frequency_{end.isoformat()}_{datetime.now():%Y%m%d_%H%M%S}_{len(stocks)}sym.md"
    md.write_text(render(res, f_base, stocks, missing, dates, args, snap), encoding="utf-8")
    cp = md.with_suffix(".csv")
    with cp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "code", "name", "market_state", *VARIANTS, "stop_ref", "entry_cap", "pullback_days"])
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
