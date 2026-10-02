"""A2 연구 데이터 수집 (조회 전용, 주문 없음, 모의 도메인 전용).

사용법 (스윙 레포 루트, PowerShell)
    python tools/research_collect.py universe                 # 종목 목록 스냅숏 (2회 호출)
    python tools/research_collect.py universe --from-probe logs\\probes\\research_sources_XXXX.jsonl --dry-run
    python tools/research_collect.py backfill --limit 20      # 시험: 20종목만 (작업은 재개 가능)
    python tools/research_collect.py backfill                 # 이어서 전부 (약 2,546종목 × 4~5페이지, 1초 간격 ≈ 3시간)
        # 끝난 뒤 다시 실행하면 아무것도 하지 않음. 전체를 새로 받을 때만 --new (매일은 update)
    python tools/research_collect.py update                   # 매일 18:10 이후: 목록 스냅숏 + 새 봉 추가 → S1 스캔·보고서
    python tools/research_collect.py scan                     # S1 스캔만 (지금 시각 기준, 조회 없음)
    python tools/research_collect.py scan --at 2026-10-02T19:30:00 --verify   # 그 시각 스캔 재현·비교
    python tools/research_collect.py status
    python tools/research_collect.py inspect-unproven         # 읽기 전용: 시각 보정·관찰 기록 보정 미리 보기(이전·백업 없음)
    python tools/research_collect.py holidays --from-year 2017 --to-year 2025

- 저장: data/research/research.sqlite3 (git 제외). 테스트·수집 모두 commands/·원장과 무관.
- 인증: .env의 KIWOOM_APP_KEY / KIWOOM_SECRET_KEY. 허용 TR은 ka10099·ka10081·ka20006뿐.
- 중단(Ctrl+C)해도 그 종목만 저장되지 않고, 다시 실행하면 같은 base_dt로 이어서 받습니다.
- 당일 봉은 정규장 종료 + 160분(기본 18:10) 이후에 받아야 저장됩니다. 그 전이면 다음 실행 때 추가됩니다.
- S1 관찰 기록: data/research/s1_scans.sqlite3, 보고서: reports/research/s1/ (둘 다 git 제외). 주문 없음.
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
from infra.research.s1_scanner import S1Scanner, ScanError  # noqa: E402
from infra.research.scan_report import write_report  # noqa: E402
from infra.research.scan_store import ScanStore  # noqa: E402
from infra.research.store import SCHEMA_VERSION, ResearchStore  # noqa: E402
from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

DEFAULT_DB = ROOT / "data" / "research" / "research.sqlite3"
DEFAULT_REPORT_DIR = ROOT / "reports" / "research" / "s1"


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
    p.add_argument("--scan-db", help="S1 관찰 기록 DB (기본: --db와 같은 폴더의 s1_scans.sqlite3)")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
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
    b.add_argument("--new", action="store_true",
                   help="새 전체 작업 생성(열린 작업이 없을 때만). 처음 한 번은 없어도 만들어짐")
    b.add_argument("--recheck-shortfall", action="store_true",
                   help="이력이 짧게 끝난 시계열(HISTORY_END·PAGE_CAP)만 새 작업으로 다시 받음(검증 후 교체)")
    up = sub.add_parser("update")
    up.add_argument("--limit", type=int)
    up.add_argument("--skip-universe", action="store_true")
    up.add_argument("--no-scan", action="store_true", help="갱신 뒤 S1 스캔을 하지 않음")
    sc = sub.add_parser("scan")
    sc.add_argument("--at", help="스캔 시각(Asia/Seoul, 예 2026-10-02T19:30:00). 기본: 지금")
    sc.add_argument("--verify", action="store_true", help="같은 시각 실행이 이미 있으면 다시 계산해 저장값과 비교")
    sub.add_parser("status")
    iu = sub.add_parser("inspect-unproven", help="읽기 전용 — 스키마를 올리지 않고 UNPROVEN·MIGRATED 봉 재점검, "
                                                 "관찰 기록 final 보정 미리 보기")
    iu.add_argument("--limit", type=int, default=30)
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


class _LazyClient:
    """API 키·세션은 실제 조회가 필요할 때 처음 만듭니다 — 조회 없이 끝나는 명령(끝난 백필 재실행 등)이
    .env 설정 오류로 실패하지 않게 (GPT a2u 검토 보완)."""

    def __init__(self, factory) -> None:
        self._factory = factory
        self._client = None

    @property
    def created(self) -> bool:
        return self._client is not None

    def ensure(self) -> None:
        """조회할 일이 확실할 때 먼저 만들어 설정 오류를 일찍 알림(작업을 만들기 전에)."""
        if self._client is None:
            self._client = self._factory()

    def __getattr__(self, name):
        # 일반 속성 조회가 실패했을 때만 불림 — fetch_page·calls 등을 실제 클라이언트로 넘김
        if name.startswith("_"):
            raise AttributeError(name)
        if name in ("calls", "retries") and self._client is None:
            return 0
        self.ensure()
        return getattr(self._client, name)


def _ensure_client(client) -> None:
    if isinstance(client, _LazyClient):
        client.ensure()


def _report_ok(path: str | None) -> bool:
    if not path:
        return False
    md = Path(path)
    return md.exists() and md.with_suffix(".json").exists()


def run_scan(args, store: ResearchStore, calendar: TradingCalendar, scan_at: datetime, now, *,
             verify: bool = False) -> int:
    """S1 관찰 스캔 (읽기만, 주문 없음). 반환: 0 완료·건너뜀 / 2 스캔 실패 / 1 보고서 실패(관찰 기록은 저장됨).

    같은 시각 실행이 이미 완료돼 건너뛸 때, 보고서가 없으면(이전 저장 실패·삭제) 저장된 실행으로 보고서만
    다시 만듭니다 — 다시 계산하지 않음 (GPT R4)."""
    with ScanStore(_scan_db(args)) as sstore:
        if sstore.backup_path:
            print(f"[관찰 저장소 스키마 변경] 바꾸기 전 백업: {sstore.backup_path}")
        if sstore.upgrade_summary:
            print(f"[관찰 저장소 이전] {json.dumps(sstore.upgrade_summary, ensure_ascii=False)}")
        scanner = S1Scanner(store, sstore, calendar, after_close=timedelta(minutes=args.after_close_min), log=print)
        try:
            res = scanner.run(scan_at, now=now, verify=verify)
        except (ScanError, CollectError) as exc:
            print(f"[스캔 실패] {exc}")
            return 2
        if res["status"] == "COMPLETE":
            run = res
        else:
            print_json({k: v for k, v in res.items() if k != "evals"})
            v = res.get("verify")
            if v is not None and not v["identical"]:
                return 2
            if _report_ok(res.get("report_path")):
                return 0
            run = sstore.load_run(res["run_id"])
            print(f"[보고서 없음] 저장된 실행 {res['run_id']}로 보고서만 다시 만듭니다(재계산 없음)")
        try:
            path = write_report(run, args.report_dir)
            sstore.set_report_path(run["run_id"], path)
        except OSError as exc:
            print(f"[보고서 저장 실패 — 관찰 기록은 저장됨, 같은 명령을 다시 실행하면 보고서만 다시 만듦] {exc}")
            path = None
        code = 0 if _report_ok(path) else 1
        c = run["counts"]
        print_json({"run_id": run["run_id"], "signal_date": run["context"]["signal_date"],
                    "contract_hash": run["context"].get("contract_hash"),
                    "universe": c["universe"], "signals": c["signals"], "by_signal": c["by_signal"],
                    "data_hold_total": c["data_hold_total"], "index_status": c["index_status"],
                    "market_regime": c["market_regime"], "no_trades_hold": c["no_trades_hold"],
                    "observations": c.get("observations"), "report": path})
        return code


def _scan_db(args) -> str:
    return args.scan_db or str(Path(args.db).with_name("s1_scans.sqlite3"))


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

    if args.cmd == "inspect-unproven":            # 읽기 전용 — ResearchStore를 열지 않음(자동 이전·백업 없음)
        from infra.research.store_inspect import inspect_unproven
        print_json(inspect_unproven(args.db, limit=args.limit, scan_db=_scan_db(args)))
        return 0

    with ResearchStore(args.db) as store:
        if store.backup_path:
            print(f"[저장소 스키마 변경] 바꾸기 전 백업: {store.backup_path}")
        if store.upgrade_summary:
            print(f"[시각 재점검 {SCHEMA_VERSION}] {json.dumps(store.upgrade_summary, ensure_ascii=False)}")
        if client is None:
            client = _LazyClient(lambda: make_client(args))
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
                _ensure_client(client)
                print(f"열린 작업 이어서: {job_id} (base_dt={open_jobs[0]['base_dt']} 고정) {store.job_counts(job_id)}")
            else:
                done_jobs = store.conn.execute("SELECT job_id FROM job ORDER BY created_at DESC").fetchall()
                if done_jobs and not (args.new or args.recheck_shortfall or args.codes):
                    # 끝난 작업만 있을 때 그냥 backfill을 다시 실행하면 전체를 새로 받지 않음 (약 3시간·1.1만 호출 방지)
                    print(f"열린 백필 작업 없음 — 마지막 작업 {done_jobs[0][0]} 완료. 새 날짜는 update로 받습니다.\n"
                          "전체를 정말 다시 받으려면 --new, 짧게 끝난 시계열만은 --recheck-shortfall, 일부 종목은 --codes")
                    return 0
                codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
                sids = None
                if args.recheck_shortfall:
                    sids = [r[0] for r in store.conn.execute(
                        "SELECT series_id FROM series WHERE coverage IN ('HISTORY_END','PAGE_CAP') ORDER BY series_id")]
                    if not sids:
                        print("다시 받을 HISTORY_END·PAGE_CAP 시계열이 없음")
                        return 0
                _ensure_client(client)
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
            def prog(p):
                if p["n"] % 100 == 0 or p["n"] == p["of"] or p["action"] not in ("APPEND", "UNCHANGED"):
                    print(f"  {p['n']}/{p['of']} {p['series_id']} {p['action']} {p.get('reason', '')}")
            code = 0
            _ensure_client(client)                       # .env 오류는 갱신·스캔 전에 바로 알림
            try:
                if not args.skip_universe:
                    snap = col.snapshot_universe()
                    print(f"목록 스냅숏 {snap['snapshot_id']}: 수집 대상 {snap['collect']} / 현재 자격 {snap['eligible_now']}")
                res = col.run_update(now=now, limit=args.limit, progress=prog)
                print_json(res)
                code = 0 if res["failed"] == 0 else 1
            except (ResearchApiError, CollectError) as exc:      # 갱신이 멈춰도 스캔은 종목별 상태로 판단
                print(f"[갱신 중단] {type(exc).__name__}: {exc}")
                code = 1
            if args.no_scan:
                return code
            scan_code = run_scan(args, store, calendar, now(), now)
            return max(code, scan_code)

        if args.cmd == "scan":
            scan_at = datetime.fromisoformat(args.at) if args.at else now()
            return run_scan(args, store, calendar, scan_at, now, verify=args.verify)

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
                   "time_basis": {
                       "current": {k: v for k, v in store.conn.execute(
                           "SELECT time_basis, COUNT(*) FROM bar GROUP BY time_basis")},
                       "history": {k: v for k, v in store.conn.execute(
                           "SELECT time_basis, COUNT(*) FROM bar_history GROUP BY time_basis")}},
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
