"""A2 연구 데이터 수집 (조회 전용, 주문 없음, 모의 도메인 전용).

사용법 (스윙 레포 루트, PowerShell)
    python tools/research_collect.py universe                 # 종목 목록 스냅숏 (2회 호출)
    python tools/research_collect.py universe --from-probe logs\\probes\\research_sources_XXXX.jsonl --dry-run
    python tools/research_collect.py backfill --limit 20      # 시험: 20종목만 (작업은 재개 가능)
    python tools/research_collect.py backfill                 # 이어서 전부 (약 2,546종목 × 4~5페이지, 1초 간격 ≈ 3시간)
    python tools/research_collect.py update                   # 매일 18:10 이후: 목록 스냅숏 + 새 봉 추가
    python tools/research_collect.py status
    python tools/research_collect.py holidays --from-year 2017 --to-year 2025

- 저장: data/research/research.sqlite3 (git 제외). 테스트·수집 모두 commands/·원장과 무관.
- 인증: .env의 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY. 허용 TR은 ka10099·ka10081·ka20006뿐.
- 중단(Ctrl+C)해도 그 종목만 저장되지 않고, 다시 실행하면 같은 base_dt로 이어서 받습니다.
- 당일 봉은 정규장 종료 + 160분(기본 18:10) 이후에 받아야 저장됩니다. 그 전이면 다음 실행 때 추가됩니다.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.research.holiday_candidates import holiday_candidates, to_yaml_snippet  # noqa: E402
from domain.research.universe import UniversePolicy, classify_rows, summarize  # noqa: E402
from infra.research.collector import (  # noqa: E402
    BAR_COMPLETE_AFTER_CLOSE, DEFAULT_MAX_PAGES, DEFAULT_REQUIRED_FROM, CollectError, ResearchCollector,
    load_probe_list,
)
from infra.research.kiwoom_readonly import ReadOnlyResearchClient, ResearchApiError, ResearchConfigError  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

DEFAULT_DB = ROOT / "data" / "research" / "research.sqlite3"


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ResearchConfigError(f".env 파일 없음: {path}")
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.strip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A2 연구 데이터 수집 (조회 전용)")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--sleep", type=float, default=1.0, help="호출 간격(초, 0.5 이상)")
    p.add_argument("--after-close-min", type=int, default=int(BAR_COMPLETE_AFTER_CLOSE.total_seconds() // 60),
                   help="당일 봉 완성 기준: 정규장 종료 후 분")
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("universe")
    u.add_argument("--from-probe", help="A1 프로브 jsonl에서 목록을 읽음(호출 없음)")
    u.add_argument("--dry-run", action="store_true", help="저장하지 않고 분류 요약만 출력")
    b = sub.add_parser("backfill")
    b.add_argument("--from", dest="required_from", default=DEFAULT_REQUIRED_FROM.isoformat())
    b.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    b.add_argument("--limit", type=int)
    b.add_argument("--codes", help="쉼표로 구분한 종목코드만 (새 작업을 만들 때)")
    b.add_argument("--no-index", action="store_true")
    b.add_argument("--new", action="store_true", help="열린 작업이 없을 때만 새 작업 생성")
    b.add_argument("--recheck-shortfall", action="store_true",
                   help="이력이 짧게 끝난 시계열(HISTORY_END·PAGE_CAP)만 새 작업으로 다시 받음(검증 후 교체)")
    up = sub.add_parser("update")
    up.add_argument("--limit", type=int)
    up.add_argument("--skip-universe", action="store_true")
    sub.add_parser("status")
    h = sub.add_parser("holidays")
    h.add_argument("--from-year", type=int, default=2017)
    h.add_argument("--to-year", type=int, default=2025)
    h.add_argument("--out", default=str(ROOT / "reports" / "research" / "holiday_candidates.yaml"))
    return p


def make_client(args):
    import requests
    env = load_env(Path(args.env_file))
    key, secret = env.get("KIWOOM_APP_KEY", ""), env.get("KIWOOM_SECRET_KEY", "")
    if not key or not secret:
        raise ResearchConfigError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY가 .env에 없음")
    return ReadOnlyResearchClient(requests.Session(), args.base_url, key, secret, min_interval_sec=args.sleep,
                                  log=print)


def print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None, *, client=None, now=now_local, calendar: TradingCalendar | None = None) -> int:
    args = build_parser().parse_args(argv)
    calendar = calendar or TradingCalendar.load()
    after_close = timedelta(minutes=args.after_close_min)

    if args.cmd == "universe" and args.from_probe and args.dry_run:
        rows, _, t0, _ = load_probe_list(Path(args.from_probe))
        print_json({"observed_at": t0, "policy_version": UniversePolicy().policy_version,
                    **summarize(classify_rows(rows))})
        return 0

    with ResearchStore(args.db) as store:
        need_net = args.cmd in ("backfill", "update") or (args.cmd == "universe" and not args.from_probe)
        if need_net and client is None:
            client = make_client(args)
        required_from = (date.fromisoformat(args.required_from) if args.cmd == "backfill"
                         else DEFAULT_REQUIRED_FROM)
        col = ResearchCollector(client, store, calendar, required_from=required_from,
                                max_pages=getattr(args, "max_pages", DEFAULT_MAX_PAGES), after_close=after_close,
                                log=print)
        if args.cmd == "universe":
            if args.from_probe:
                rows, raw_pages, t0, t1 = load_probe_list(Path(args.from_probe))
                res = col.snapshot_from_rows(rows, raw_pages, t0, t1, f"probe:{Path(args.from_probe).name}")
            else:
                if args.dry_run:
                    raise SystemExit("--dry-run은 --from-probe와 함께만")
                res = col.snapshot_universe()
            print_json(res)
            return 0

        if args.cmd == "backfill":
            open_jobs = store.open_jobs()
            if open_jobs:
                if args.new:
                    print(f"[중단] 열린 작업이 있음: {open_jobs[0]['job_id']} — --new 없이 실행하면 이어서 받습니다")
                    return 2
                job_id = open_jobs[0]["job_id"]
                print(f"열린 작업 이어서: {job_id} (base_dt={open_jobs[0]['base_dt']} 고정) {store.job_counts(job_id)}")
            else:
                codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
                sids = None
                if args.recheck_shortfall:
                    sids = [r[0] for r in store.conn.execute(
                        "SELECT series_id FROM series WHERE coverage IN ('HISTORY_END','PAGE_CAP') ORDER BY series_id")]
                    if not sids:
                        print("다시 받을 HISTORY_END·PAGE_CAP 시계열이 없음")
                        return 0
                job_id = col.create_backfill_job(now=now(), codes=codes, include_index=not args.no_index,
                                                 series_ids=sids)
                job = store.get_job(job_id)
                print(f"새 작업: {job_id} base_dt={job['base_dt']} required_from={job['required_from']} "
                      f"{store.job_counts(job_id)}")

            def prog(p):
                if p["n"] % 50 == 0 or p["n"] == p["of"] or p["status"] != "DONE":
                    print(f"  {p['n']}/{p['of']} {p['series_id']} {p['status']}")
            res = col.run_backfill(job_id, now=now, limit=args.limit, progress=prog)
            print_json(res)
            return 0 if res["ERROR"] == 0 else 1

        if args.cmd == "update":
            if not args.skip_universe:
                snap = col.snapshot_universe()
                print(f"목록 스냅숏 {snap['snapshot_id']}: 수집 대상 {snap['collect']} / 현재 자격 {snap['eligible_now']}")

            def prog(p):
                if p["n"] % 100 == 0 or p["n"] == p["of"] or p["action"] not in ("APPEND", "UNCHANGED"):
                    print(f"  {p['n']}/{p['of']} {p['series_id']} {p['action']} {p.get('reason', '')}")
            res = col.run_update(now=now, limit=args.limit, progress=prog)
            print_json(res)
            return 0 if res["failed"] == 0 else 1

        if args.cmd == "status":
            snap = store.latest_snapshot()
            out = {"db": args.db, "latest_snapshot": None if snap is None else
                   {k: snap[k] for k in ("snapshot_id", "snapshot_date", "observed_at", "market_phase",
                                         "policy_version", "source")} | {"summary": snap["summary"]},
                   "jobs": [{"job_id": j["job_id"], "base_dt": j["base_dt"], "status": j["status"],
                             "counts": store.job_counts(j["job_id"])}
                            for j in store.conn.execute("SELECT * FROM job ORDER BY created_at")],
                   "series": {k: v for k, v in store.conn.execute(
                       "SELECT coverage, COUNT(*) FROM series GROUP BY coverage")},
                   "bars": store.conn.execute("SELECT COUNT(*) FROM bar").fetchone()[0],
                   "no_trades_bars": store.conn.execute("SELECT COUNT(*) FROM bar WHERE quality='NO_TRADES'").fetchone()[0],
                   "invalid_bars": store.conn.execute("SELECT COUNT(*) FROM bar WHERE quality LIKE 'INVALID%'").fetchone()[0],
                   "rebases": store.conn.execute("SELECT COUNT(*) FROM series_event WHERE event='REBASE'").fetchone()[0],
                   "integrity": {k: v for k, v in store.conn.execute(
                       "SELECT integrity, COUNT(*) FROM series GROUP BY integrity")},
                   "verify_failed_events": store.conn.execute(
                       "SELECT COUNT(*) FROM series_event WHERE event='VERIFY_FAILED'").fetchone()[0],
                   "time_basis": {k: v for k, v in store.conn.execute(
                       "SELECT time_basis, COUNT(*) FROM bar GROUP BY time_basis")},
                   "revision_reasons": {k: v for k, v in store.conn.execute(
                       "SELECT reason, COUNT(*) FROM series_revision GROUP BY reason")},
                   "schema": store.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]}
            print_json(out)
            return 0

        if args.cmd == "holidays":
            meta = store.get_series("INDEX:KOSPI:001")
            if meta is None:
                print("[중단] KOSPI 지수 시계열이 없음 — 먼저 backfill")
                return 2
            k = [sb.raw.date for sb in store.load_bars("INDEX:KOSPI:001")]  # 현재 revision 전체
            q = [sb.raw.date for sb in store.load_bars("INDEX:KOSDAQ:101")] if store.get_series("INDEX:KOSDAQ:101") else None
            res = holiday_candidates(k, date(args.from_year, 1, 1), date(args.to_year, 12, 31), q)
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(to_yaml_snippet(res), encoding="utf-8")
            print_json({"by_year": res["by_year"], "candidates": len(res["candidates"]),
                        "fixed_holiday_but_traded": res["fixed_holiday_but_traded"],
                        "weekend_bars": res["weekend_bars"], "index_date_mismatch": res.get("index_date_mismatch"),
                        "written": str(out)})
            return 0
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except (ResearchConfigError, ResearchApiError, CollectError) as exc:
        print(f"[중단] {type(exc).__name__}: {exc}")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[중단] 사용자 중단 — 진행 중이던 종목만 저장되지 않았습니다. 다시 실행하면 이어서 받습니다.")
        sys.exit(130)
