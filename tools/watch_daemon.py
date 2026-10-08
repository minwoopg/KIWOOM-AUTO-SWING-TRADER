"""W2 조회 전용 상시 실행 관리자 (주문 없음) — docs/watch_daemon.md

사용법 (스윙 레포 루트, PowerShell)
    python tools/watch_daemon.py run                 # 시작(창에서 실행 — Ctrl+C로 종료). 이미 실행 중이면 종료 코드 2
    python tools/watch_daemon.py run --until-idle    # 지금 실행할 작업만 하고 끝냄(미래 시각의 다시 시도·예산 회복은 기다리지 않음)
    python tools/watch_daemon.py status              # 실행 여부·heartbeat·설정 버전·작업 결과·다음 예정·오늘 호출 수
    python tools/watch_daemon.py stop                # 실행 중인 관리자에 중지 요청(다음 순회·대상 사이에서 멈춤)
    python tools/watch_daemon.py report --day 2026-10-08   # 그날 일일 보고서 다시 만들기(재실행 없음)
    python tools/watch_daemon.py doctor              # 기존 Windows 작업 스케줄러 항목과 겹치는지 확인(읽기만 — 바꾸지 않음)
    python tools/watch_daemon.py export-db --out exports\\watch_20261008\\db   # 관리자·관찰·개장 확인 DB 일관된 사본(공유용)

- 대상: 지정 종목(관심 켜짐·수동 보유) + KOSPI·KOSDAQ. 전체 시장 수집·S1 스캔은 tools/research_collect.py(별도).
- 저장: data/watch/daemon.sqlite3(작업 상태), watch_s1.sqlite3(지정 종목 S1 관찰), watch_open.sqlite3(개장 확인),
  보고서 reports/watch/, 로그 logs/watch_daemon.log. 모의 도메인·조회 TR만.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE  # noqa: E402
from infra.research.kiwoom_readonly import ResearchApiError, ResearchConfigError  # noqa: E402
from infra.research.research_config import (  # noqa: E402
    DEFAULT_RESEARCH_CONFIG, ResearchSettingsError, load_research_settings)
from infra.watch.apply import ConfigLockTimeout, file_lock  # noqa: E402
from infra.watch.daemon import (  # noqa: E402
    DaemonPaths, DaemonSettings, DaemonStore, WatchDaemon, daemon_lock, write_daily_report)
from infra.research.s1_scanner import calendar_version  # noqa: E402
from infra.watch.manager import load_state  # noqa: E402
from infra.watch.store import WatchStore  # noqa: E402
from tools.research_collect import _LazyClient, make_client  # noqa: E402
from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

DATA = ROOT / "data"
SCHEDULER_HINTS = ("research_collect", "watchlist.py", "watch_daemon", "main.py", "daily_report", "update_daily_bars")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="W2 조회 전용 상시 실행 관리자")
    p.add_argument("--config", default=str(ROOT / "config" / "watchlist.yaml"))
    p.add_argument("--watch-db", default=str(DATA / "watch" / "watch.sqlite3"))
    p.add_argument("--db", default=str(DATA / "research" / "research.sqlite3"))
    p.add_argument("--daemon-db", default=str(DATA / "watch" / "daemon.sqlite3"))
    p.add_argument("--watch-scan-db", default=str(DATA / "watch" / "watch_s1.sqlite3"))
    p.add_argument("--watch-open-db", default=str(DATA / "watch" / "watch_open.sqlite3"))
    p.add_argument("--report-dir", default=str(ROOT / "reports" / "watch"))
    p.add_argument("--log-file", default=str(ROOT / "logs" / "watch_daemon.log"))
    p.add_argument("--research-config", default=str(DEFAULT_RESEARCH_CONFIG), help="개장 확인 offset·허용 시간")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--sleep", type=float, default=1.0, help="API 호출 간격(초, 0.5 이상)")
    p.add_argument("--after-close-min", type=int, default=int(BAR_COMPLETE_AFTER_CLOSE.total_seconds() // 60))
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--poll-sec", type=float, default=60.0)
    r.add_argument("--daily-call-cap", type=int, default=3000,
                   help="하루(요청일) 실제 요청 상한 — 이 관리자 프로세스의 모든 작업·토큰·연속조회·재시도 합계")
    r.add_argument("--open-check-reserve", type=int, default=100,
                   help="마감 준비가 남겨 두는 개장 확인 몫(마감 준비는 cap−reserve에서 멈춤)")
    r.add_argument("--max-task-sec", type=float, default=1800.0, help="작업 하나의 시간 상한 — 넘으면 양보")
    r.add_argument("--max-ticks", type=int, help="시험용: 순회 횟수 상한")
    r.add_argument("--until-idle", action="store_true", help="지금 실행할 작업이 없어질 때까지만")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    st.add_argument("--poll-sec", type=float, default=60.0, help="응답 없음 판단 기준(heartbeat > 3×poll)")
    sub.add_parser("stop")
    rp = sub.add_parser("report")
    rp.add_argument("--day", required=True)
    sub.add_parser("doctor")
    ex = sub.add_parser("export-db", help="관리자·지정 종목 관찰·개장 확인 DB의 일관된 사본(SQLite 백업 API, 읽기만)")
    ex.add_argument("--out", required=True, help="사본을 둘 폴더(예: exports\\watch_20261008\\db)")
    return p


def paths_of(args) -> DaemonPaths:
    return DaemonPaths(Path(args.config), Path(args.watch_db), Path(args.db), Path(args.daemon_db),
                       Path(args.watch_scan_db), Path(args.watch_open_db), Path(args.report_dir),
                       Path(args.log_file) if args.log_file else None)


def _settings(args) -> DaemonSettings:
    rs = load_research_settings(args.research_config)
    return DaemonSettings(poll_sec=getattr(args, "poll_sec", 60.0), daily_call_cap=getattr(args, "daily_call_cap", 3000),
                          open_check_reserve=getattr(args, "open_check_reserve", 100),
                          max_task_sec=getattr(args, "max_task_sec", 1800.0),
                          open_offset_min=rs.open_check.offset_min,
                          on_time_tolerance_sec=rs.open_check.on_time_tolerance_sec,
                          after_close=timedelta(minutes=args.after_close_min))


def running(daemon_db: Path) -> bool:
    """관리자 잠금이 잡혀 있으면 실행 중."""
    try:
        with file_lock(Path(str(daemon_db) + ".lock"), timeout=0, what="상태 확인"):
            return False
    except ConfigLockTimeout:
        return True


def cmd_status(args, *, now, calendar) -> int:
    p = paths_of(args)
    if not p.daemon_db.exists():
        print("관리자 기록 없음 — 아직 한 번도 실행하지 않음(`run`)")
        return 2
    alive = running(p.daemon_db)
    with DaemonStore(p.daemon_db) as ds:
        last = ds.last_run()
        t_now = now()
        state = "기록 없음"
        if last is not None:
            age = (t_now - datetime.fromisoformat(last["heartbeat_at"])).total_seconds()
            if alive:
                state = (f"실행 중 · heartbeat {age:.0f}초 전" if age <= 3 * args.poll_sec
                         else f"응답 없음 — 잠금은 있으나 heartbeat {age:.0f}초 전(멈춤 의심: stop 후 다시 run)")
            elif last["state"] == "RUNNING":
                state = "비정상 종료 — 잠금 없음(프로세스가 끝남). 다시 run하면 중단된 작업을 이어서 함"
            else:
                state = f"중지됨({last['state']} {last['stopped_at']})"
        tasks = ds.tasks(limit=12)
        usage = ds.calls(t_now.date())
        since = ds.meta("since")
    with WatchStore(p.watch_db) as ws:
        st = load_state(ws)
    nxt, cal = None, {"status": "UNKNOWN", "message": ""}
    try:
        d = WatchDaemon(p, calendar, None, settings=_settings(args), now=now, log=lambda m: None, hook=lambda x: None)
        nxt = d.next_due(t_now)
        cal = d.calendar_status()
        d.dstore.close()
    except ResearchSettingsError:
        pass
    stop_resp = None
    if last and last.get("stop_requested_at") and last.get("stopped_at"):
        stop_resp = (datetime.fromisoformat(last["stopped_at"])
                     - datetime.fromisoformat(last["stop_requested_at"])).total_seconds()
    out = {"daemon": state, "run": last, "since": since, "config": st.summary(), "calls_today": usage,
           "calendar": cal, "calendar_version": calendar_version(calendar), "stop_response_sec": stop_resp,
           "kst_now": t_now.isoformat(timespec="seconds"),
           "next_due": nxt.isoformat() if nxt else None,
           "tasks": [{k: t[k] for k in ("kind", "trading_day", "status", "attempts", "failures", "finished_at",
                                        "next_retry_at", "config_version", "contract_hash", "error", "report_path",
                                        "report_error")} for t in tasks]}
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"관리자: {state}")
    if last:
        print(f"  run_id {last['run_id']} pid {last['pid']} 시작 {last['started_at']} 현재 작업 {last['current_task'] or '-'}"
              + (f" · 진척 {last['progress']}" if last.get("progress") else "")
              + (f" · 마지막 오류 {last['last_error']}" if last["last_error"] else ""))
        if stop_resp is not None:
            print(f"  중지 요청 {last['stop_requested_at']} → 종료 {last['stopped_at']} ({stop_resp:.0f}초)")
    if cal["status"] != "OK":
        print(f"거래일 달력: {cal['status']} {cal['message']}"
              + (" — config/trading_calendar를 갱신한 뒤 관리자를 다시 시작" if cal["status"] == "CALENDAR_UNAVAILABLE" else ""))
    s = st.summary()
    print("설정: " + (f"v{s['active_version']} 사용 중" if st.can_monitor else "정상 설정 없음")
          + (f" · 신규 진입 차단 — {st.block_reason}" if st.entry_blocked else "")
          + "  (마지막 반영 기준 — 관리자가 매 순회 다시 반영)")
    print(f"오늘 호출 {usage}회 · 다음 예정 {out['next_due'] or '-'} · 운영 시작일 {since}")
    print("\n| 작업 | 거래일 | 상태 | 시도/실패 | 끝 | 다음 시도 | 설정 | 오류 |")
    print("|---|---|---|---|---|---|---|---|")
    for t in tasks:
        print(f"| {t['kind']} | {t['trading_day']} | {t['status']} | {t['attempts']}/{t['failures']} | "
              f"{t['finished_at'] or '-'} | {t['next_retry_at'] or '-'} | v{t['config_version'] or '-'} | "
              f"{(t['error'] or (('보고서 실패: ' + t['report_error']) if t['report_error'] else '-'))[:70]} |")
    return 0


def _same_file(a: Path, b: Path) -> bool:
    """같은 파일인지 — 둘 다 있으면 OS 기준(심볼릭·하드 링크 포함), 아니면 실제 경로(대소문자는 OS 규칙) 비교."""
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def export_conflicts(sources, out: Path, *, protected=()) -> list[str]:
    """export-db 예정 경로 사전 검사: 대상이 원본(또는 보호 DB)과 같은 파일, 서로 다른 원본의 대상 이름 충돌이면 사유 목록."""
    out_msgs, seen = [], {}
    existing = [s for s in sources if s.exists()]
    for src in existing:
        dest = out / src.name
        key = os.path.normcase(dest.name)
        if key in seen and not _same_file(seen[key], src):
            out_msgs.append(f"대상 이름 충돌: {seen[key]} 와 {src} 가 모두 {dest}로 복사됨")
        seen[key] = src
        for other in (*sources, *protected):
            if other is not None and _same_file(dest, Path(other)):
                out_msgs.append(f"대상 {dest}이(가) 원본/보호 DB {other}와 같은 파일")
    return out_msgs


def cmd_export_db(args, *, now) -> int:
    """daemon·watch_s1·watch_open DB를 SQLite 백업 API로 복사(실행 중이어도 각 파일은 일관된 사본). 파일마다 복사 시작·끝 시각을
    남김 — 세 사본이 같은 시점이라는 보장은 없음(같은 시점이 필요하면 관리자를 stop한 뒤 실행). 감시 설정 DB(watch.sqlite3 —
    수동 보유 원문)와 연구 DB는 넣지 않음."""
    p = paths_of(args)
    out = Path(args.out)
    sources = (p.daemon_db, p.watch_scan_db, p.watch_open_db)
    problems = export_conflicts(sources, out, protected=(p.watch_db, p.research_db))
    if problems:                                     # 복사 전에 전체 경로를 검사 — 원본에 쓰거나 일부만 복사하지 않음 (E1)
        for m in problems:
            print(f"[거부] {m}")
        print("원본 DB 폴더가 아닌 다른 폴더를 --out으로 지정하세요(예: exports\\watch_<날짜>\\db). 아무것도 복사하지 않았습니다.")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    rows, rc = [], 0
    for path in sources:
        if not path.exists():
            rows.append({"file": path.name, "status": "MISSING"})
            continue
        t0 = now().isoformat(timespec="seconds")
        try:
            src = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            dst = sqlite3.connect(out / path.name)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            rows.append({"file": path.name, "status": "OK", "started_at": t0,
                         "finished_at": now().isoformat(timespec="seconds")})
        except sqlite3.Error as exc:
            rc = 1
            rows.append({"file": path.name, "status": f"FAILED {type(exc).__name__}: {exc}", "started_at": t0})
    (out / "export_db.json").write_text(json.dumps({"files": rows, "note": "파일별 일관된 사본 — 파일 사이 같은 시점은 보장 안 함"},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
    for r in rows:
        print(f"{r['file']}: {r['status']}" + (f" ({r['started_at']}~{r.get('finished_at', '-')})" if "started_at" in r else ""))
    return rc


def cmd_doctor(args, *, runner=None) -> int:
    """Windows 작업 스케줄러에서 이 레포의 매일 실행과 겹칠 수 있는 항목을 찾아 안내(읽기만)."""
    p = paths_of(args)
    print(f"관리자: {'실행 중' if p.daemon_db.exists() and running(p.daemon_db) else '실행 안 함'}")
    if runner is None:
        if sys.platform != "win32":
            print("작업 스케줄러 확인은 Windows에서만 — 이 환경에서는 건너뜀")
            return 0
        runner = lambda: subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/V"], capture_output=True,  # noqa: E731
                                        text=True, encoding="cp949", errors="replace", timeout=60).stdout
    try:
        text = runner()
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[확인 못 함] schtasks 실행 실패 {type(exc).__name__}: {exc}")
        return 2
    rows = list(csv.DictReader(io.StringIO(text)))
    hits = []
    for r in rows:
        run = next((v for k, v in r.items() if k and ("Task To Run" in k or "실행할 작업" in k)), "") or ""
        name = next((v for k, v in r.items() if k and ("TaskName" in k or "작업 이름" in k)), "") or ""
        if any(h in run for h in SCHEDULER_HINTS) and (name, run) not in hits:
            hits.append((name, run))
    if not hits:
        print("겹칠 만한 작업 스케줄러 항목 없음")
        return 0
    print("| 작업 이름 | 실행 명령 | 안내 |")
    print("|---|---|---|")
    for name, run in hits:
        why = ("관리자와 같은 일(지정 종목 준비)을 함 — 관리자를 쓰면 중지 권장" if "watchlist.py" in run else
               "전체 시장 수집·스캔·개장 확인 — 관리자와 대상이 달라 함께 써도 됨(같은 연구 DB에 써서 그 시간엔 느려질 수 있음)"
               if "research_collect" in run else
               "관리자를 이 항목으로 띄우는 중이면 run --until-idle 또는 한 번만 시작되게 확인" if "watch_daemon" in run
               else "확인 필요")
        print(f"| {name} | {run} | {why} |")
    print("\n이 명령은 작업 스케줄러를 바꾸지 않습니다. 정리는 작업 스케줄러에서 직접 하세요.")
    return 1


def _stop_daemon(d, state: str, err: str) -> bool:
    """종료 상태 저장. 실패하면 경고를 찍고 False — 상태 저장 실패를 정상으로 보이지 않게 (finally 안 return 없이 같은 동작)."""
    try:
        d.stop(state, err)
        return True
    except Exception as exc:                                 # noqa: BLE001
        print(f"[경고] 관리자 종료 상태 저장 실패 {type(exc).__name__}: {exc}")
        return False


def main(argv: list[str] | None = None, *, client=None, now=now_local, calendar: TradingCalendar | None = None,
         sleep=time.sleep, log=print) -> int:
    args = build_parser().parse_args(argv)
    calendar = calendar or TradingCalendar.load()
    if os.environ.get("WATCH_TEST_NOW"):                   # 시험용 고정 시계(별도 프로세스 시험) — 운영에서는 쓰지 않음
        fixed = datetime.fromisoformat(os.environ["WATCH_TEST_NOW"])
        now = lambda: fixed  # noqa: E731
    if args.cmd == "status":
        return cmd_status(args, now=now, calendar=calendar)
    if args.cmd == "export-db":
        return cmd_export_db(args, now=now)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    p = paths_of(args)
    if args.cmd == "stop":
        if not p.daemon_db.exists() or not running(p.daemon_db):
            print("실행 중인 관리자가 없음")
            return 0
        with DaemonStore(p.daemon_db) as ds:
            ok = ds.request_stop(now())
        print("중지 요청을 남김 — 다음 순회(작업 중이면 대상 사이)에서 멈춥니다" if ok else "실행 기록을 찾지 못함")
        return 0 if ok else 2
    if args.cmd == "report":
        with DaemonStore(p.daemon_db) as ds:
            try:
                path = write_daily_report(ds, p, date.fromisoformat(args.day).isoformat())
            except OSError as exc:
                print(f"[실패] 보고서 {type(exc).__name__}: {exc}")
                return 1
            for t in ds.tasks(args.day):
                if t["report_error"]:
                    ds.set_report(t["task_key"], path, None)
        print(f"보고서: {path}")
        return 0
    # run
    try:
        settings = _settings(args)
    except ResearchSettingsError as exc:
        print(f"[설정 오류] {exc}")
        return 2
    try:
        with daemon_lock(p.daemon_db):
            holder: dict = {}
            if client is None:                              # 모든 실제 요청 직전에 관리자의 예산·중지·우선 작업 검사(R3)
                # 인증·재발급·재시도 메시지도 관리자 log(시각·가림·로그 파일)로 (O1) — 연구 CLI는 기본 print 그대로
                client = _LazyClient(lambda: make_client(args, log=lambda m: holder["d"].log(m),
                                                         guard=lambda api: holder["d"].request_guard(api)))
            d = WatchDaemon(p, calendar, client, settings=settings, now=now, sleep=sleep, log=log)
            holder["d"] = d
            d.start()
            state, err = "STOPPED", ""
            try:
                if args.until_idle:
                    state = d.run_until_idle()
                else:
                    state = d.run_forever(max_ticks=args.max_ticks)
            except ResearchConfigError as exc:
                state, err = "FAILED", f"API 설정 오류: {exc}"
                print(f"[중단] {err}")
            except BaseException as exc:
                state, err = "FAILED", f"{type(exc).__name__}: {exc}"
                if not _stop_daemon(d, state, err):
                    return 1                                 # 이전과 같음: 종료 상태 저장도 실패하면 예외 대신 종료 코드 1
                raise
            if not _stop_daemon(d, state, err):
                return 1
            return 0 if state in ("STOPPED", "IDLE", "IDLE_CAP", "MAX_TICKS", "INTERRUPTED") else 1
    except ConfigLockTimeout:
        print("[중단] 이미 실행 중인 관리자가 있음 — `status`로 확인, 끝내려면 `stop`")
        return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except (ResearchConfigError, ResearchApiError) as exc:
        print(f"[중단] {type(exc).__name__}: {exc}")
        sys.exit(2)
