"""사용자 지정 종목 설정·검증·데이터 준비 (조회 전용, 주문 없음) — docs/watchlist.md

사용법 (스윙 레포 루트, PowerShell)
    python tools/watchlist.py init                          # config/watchlist.yaml 새로 만들기(종목 없음)
    python tools/watchlist.py add 005930 --interest --s1 --band 70000-72000:눌림
    python tools/watchlist.py add 000660 --holding --qty 10 --avg 180000 --stop 165000 --target 210000
    python tools/watchlist.py set 005930 --band 68000-70000 --clear-bands   # 가격대 바꾸기
    python tools/watchlist.py disable 005930                # 관심 감시 끄기(보유가 있으면 보유 감시는 유지)
    python tools/watchlist.py enable 005930
    python tools/watchlist.py holding-close 000660          # 수동 보유 정보 지우기(청산)
    python tools/watchlist.py remove 005930                 # 항목 삭제(보유가 있으면 거부)
    python tools/watchlist.py validate                      # 검사만(기록 안 함)
    python tools/watchlist.py apply                         # 파일을 직접 고친 뒤 적용(이력 기록)
    python tools/watchlist.py prepare                       # 등록 종목 + 국내 지수만 일봉 갱신·준비 상태 판정
    python tools/watchlist.py status
    python tools/watchlist.py history

- 바꾸는 명령(add·set·enable·disable·holding-close·remove)은 바꾼 결과를 먼저 검증하고, 틀리면 파일을 바꾸지 않습니다.
- add는 적용 뒤 그 종목의 과거 일봉을 바로 받습니다(--no-fetch로 생략, 나중에 prepare).
- 저장: config/watchlist.yaml(git 제외), data/watch/watch.sqlite3(적용 이력·준비 상태), 일봉은 연구 DB를 함께 씀.
- 전체 시장 수집·S1 스캔은 그대로 tools/research_collect.py(별도 명령).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domain.watchlist.config import (  # noqa: E402
    Interest, PriceBand, dump_document, empty_document, find_item, parse_text,
)
from infra.research.collector import BAR_COMPLETE_AFTER_CLOSE, CollectError, ResearchCollector  # noqa: E402
from infra.research.kiwoom_readonly import ResearchApiError, ResearchConfigError  # noqa: E402
from infra.research.store import ResearchStore  # noqa: E402
from infra.watch.manager import (  # noqa: E402
    READY, WatchNotReady, check_text, entry_gate, listing_from_store, load_state, prepare_data, sync_config,
)
from infra.watch.store import WatchStore  # noqa: E402
from tools.research_collect import _LazyClient, make_client  # noqa: E402
from utils.time_utils import now_local  # noqa: E402
from utils.trading_calendar import TradingCalendar  # noqa: E402

DEFAULT_CONFIG = ROOT / "config" / "watchlist.yaml"
DEFAULT_WATCH_DB = ROOT / "data" / "watch" / "watch.sqlite3"
DEFAULT_RESEARCH_DB = ROOT / "data" / "research" / "research.sqlite3"


class EditError(ValueError):
    pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="사용자 지정 종목 설정·검증·데이터 준비 (조회 전용)")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--watch-db", default=str(DEFAULT_WATCH_DB))
    p.add_argument("--db", default=str(DEFAULT_RESEARCH_DB), help="연구 수집 DB(일봉·종목 목록, 함께 씀)")
    p.add_argument("--env-file", default=str(ROOT / ".env"))
    p.add_argument("--base-url", default="https://mockapi.kiwoom.com")
    p.add_argument("--sleep", type=float, default=1.0)
    p.add_argument("--after-close-min", type=int, default=int(BAR_COMPLETE_AFTER_CLOSE.total_seconds() // 60))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    v = sub.add_parser("validate")
    v.add_argument("--no-list", action="store_true", help="종목 목록 대조 없이 형식만")
    sub.add_parser("apply")
    for name in ("add", "set"):
        a = sub.add_parser(name)
        a.add_argument("code")
        a.add_argument("--name")
        a.add_argument("--memo")
        a.add_argument("--band", action="append", default=[], help="관심 가격대 LOW-HIGH[:이름] (여러 번)")
        a.add_argument("--qty", type=int)
        a.add_argument("--avg", type=int)
        a.add_argument("--stop", type=int)
        a.add_argument("--target", type=int)
        if name == "add":
            a.add_argument("--interest", action="store_true", help="관심 감시 켜기")
            a.add_argument("--s1", action="store_true", help="S1 분석 대상")
            a.add_argument("--holding", action="store_true", help="수동 보유 정보(--qty·--avg 필수)")
            a.add_argument("--no-fetch", action="store_true", help="과거 일봉을 바로 받지 않음")
        else:
            a.add_argument("--s1", choices=("on", "off"))
            a.add_argument("--clear-bands", action="store_true", help="기존 가격대를 지우고 --band로 새로")
            a.add_argument("--clear-stop", action="store_true")
            a.add_argument("--clear-target", action="store_true")
    for name in ("enable", "disable", "holding-close", "remove"):
        a = sub.add_parser(name)
        a.add_argument("code")
        if name == "disable":
            a.add_argument("--holding", action="store_true", help="(보유 감시는 끌 수 없음 — 안내만)")
    pr = sub.add_parser("prepare")
    pr.add_argument("--codes", help="쉼표로 구분한 등록 종목만 조회(상태 판정은 전체)")
    pr.add_argument("--no-list-refresh", action="store_true", help="오늘 종목 목록을 다시 받지 않음")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    hi = sub.add_parser("history")
    hi.add_argument("--limit", type=int, default=20)
    return p


def print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_issues(errors, warnings) -> None:
    for e in errors:
        print(f"  [오류] {e['code']} {e['field']}: {e['message']}")
    for w in warnings:
        print(f"  [경고] {w['code']} {w['field']}: {w['message']}")


def _band(s: str) -> dict:
    rng, _, label = s.partition(":")
    lo, sep, hi = rng.partition("-")
    try:
        out = {"low": int(lo.replace(",", "")), "high": int(hi.replace(",", ""))}
    except ValueError:
        raise EditError(f"--band 형식: LOW-HIGH[:이름] (현재 {s!r})") from None
    if not sep:
        raise EditError(f"--band 형식: LOW-HIGH[:이름] (현재 {s!r})")
    if label:
        out["label"] = label
    return out


def _code(s: str) -> str:
    return s.strip().upper()


def _apply_holding_args(item: dict, args, *, create: bool) -> None:
    vals = {"quantity": args.qty, "avg_price": args.avg, "stop_price": args.stop, "target_price": args.target}
    if create:
        item["holding"] = {k: v for k, v in vals.items() if v is not None}
        return
    if any(v is not None for v in vals.values()) or args.clear_stop or args.clear_target:
        if item.get("holding") is None:
            if args.qty is None or args.avg is None:
                raise EditError("보유 정보가 없음 — 새로 넣으려면 --qty와 --avg를 함께")
            item["holding"] = {}
        h = item["holding"]
        h.update({k: v for k, v in vals.items() if v is not None})
        if args.clear_stop:
            h.pop("stop_price", None)
        if args.clear_target:
            h.pop("target_price", None)


def edit_document(doc: dict, args) -> str:
    """명령에 따라 원본 사전을 바꿈. 반환: 이력에 남길 설명."""
    code = _code(args.code)
    items = doc.setdefault("symbols", [])
    if items is None:
        items = doc["symbols"] = []
    item = find_item(doc, code)
    if args.cmd == "add":
        if item is not None:
            raise EditError(f"{code}는 이미 있음 — set으로 바꾸세요")
        if not (args.interest or args.holding):
            raise EditError("--interest 또는 --holding 중 하나는 필요")
        item = {"code": code}
        if args.name:
            item["name"] = args.name
        if args.memo:
            item["memo"] = args.memo
        if args.interest or args.s1 or args.band:
            item["interest"] = {"enabled": bool(args.interest), "s1_analysis": bool(args.s1),
                                "price_bands": [_band(b) for b in args.band]}
        if args.holding:
            _apply_holding_args(item, args, create=True)
        elif any(x is not None for x in (args.qty, args.avg, args.stop, args.target)):
            raise EditError("보유 값(--qty 등)은 --holding과 함께")
        items.append(item)
        return f"add {code}"
    if item is None:
        raise EditError(f"{code}가 설정에 없음")
    if args.cmd == "set":
        if args.name is not None:
            item["name"] = args.name
        if args.memo is not None:
            item["memo"] = args.memo
        if args.s1 is not None or args.band or args.clear_bands:
            it = item.setdefault("interest", {"enabled": False, "s1_analysis": False, "price_bands": []})
            if args.s1 is not None:
                it["s1_analysis"] = args.s1 == "on"
            if args.clear_bands:
                it["price_bands"] = []
            it["price_bands"] = list(it.get("price_bands") or []) + [_band(b) for b in args.band]
        _apply_holding_args(item, args, create=False)
        return f"set {code}"
    if args.cmd in ("enable", "disable"):
        if args.cmd == "disable" and getattr(args, "holding", False):
            raise EditError("보유 감시는 끌 수 없음 — 보유가 끝났으면 holding-close로 수동 보유 정보를 지우세요")
        it = item.setdefault("interest", {"enabled": False, "s1_analysis": False, "price_bands": []})
        it["enabled"] = args.cmd == "enable"
        return f"{args.cmd} {code}"
    if args.cmd == "holding-close":
        if item.get("holding") is None:
            raise EditError(f"{code}에 수동 보유 정보가 없음")
        item.pop("holding")
        if item.get("interest") is None:
            item["interest"] = {"enabled": False, "s1_analysis": False, "price_bands": []}   # 항목은 비활성으로 남김
        return f"holding-close {code}"
    if args.cmd == "remove":
        if item.get("holding") is not None:
            raise EditError(f"{code}에 수동 보유 정보가 있음 — 보유 감시를 끊지 않도록 삭제 거부(먼저 holding-close)")
        items.remove(item)
        return f"remove {code}"
    raise EditError(args.cmd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _status_table(state, wstore: WatchStore, listing) -> None:
    s = state.summary()
    if not state.can_monitor:
        print(f"설정: {state.block_reason}")
    else:
        print(f"설정: v{s['active_version']} 사용 중(적용 {s['active_applied_at']}) · 최근 시도 v{s['latest_version']} "
              f"{s['latest_status']} · 신규 매수 차단 {'예 — ' + state.block_reason if state.entry_blocked else '아니오'}")
    if state.config_error:
        print("최근 시도 오류(적용 안 됨):")
        _print_issues(state.config_error, [])
    if state.config is None:
        return
    ready = wstore.readiness()
    print("\n| 대상 | 이름 | 관심 | S1 | 관심 가격대 | 수동 보유(증권사 잔고 아님) | 데이터 | 위험 표시 | 신규 진입 관찰 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for sid in ("INDEX:KOSPI:001", "INDEX:KOSDAQ:101"):
        r = ready.get(sid)
        print(f"| {sid} | - | - | - | - | - | {_ready_cell(r)} | - | - |")
    for sym in state.config.symbols:
        r = ready.get(f"STOCK:{sym.code}")
        lr = None if listing is None else listing.get(sym.code)
        it = sym.interest or Interest(False)
        bands = ", ".join(f"{b.low:,}~{b.high:,}{'(' + b.label + ')' if b.label else ''}" for b in it.price_bands)
        h = sym.holding
        hold = "-" if h is None else (f"{h.quantity:,}주 @ {h.avg_price:,} · 손절 {_n(h.stop_price)} · 목표 "
                                      f"{_n(h.target_price)} (수동)")
        if not sym.active:
            gate = "감시 안 함(비활성)"
        else:
            ok, why = entry_gate(state, sym, r)
            gate = "가능" if ok else "제외 " + ",".join(why)
        risk = "-" if lr is None else (",".join(lr.risk_flags) or "없음")
        print(f"| {sym.code} | {lr.name if lr else (sym.name or '?')} | {'켜짐' if sym.interest_active else '꺼짐'} | "
              f"{'예' if it.s1_analysis else '-'} | {bands or '-'} | {hold} | "
              f"{_ready_cell(r) if sym.active else '-'} | {risk} | {gate} |")


def _ready_cell(r) -> str:
    if r is None:
        return "UNKNOWN(준비 안 함 — prepare)"
    if r["status"] == READY:
        return READY
    d = r.get("detail") or {}
    extra = f" {d['have']}/{d['need']}봉" if "have" in d and r["reason"] in ("INSUFFICIENT_HISTORY", "MISSING_SESSIONS") \
        else ""
    return f"UNKNOWN({r['reason']}{extra})"


def _n(v) -> str:
    return "-" if v is None else f"{v:,}"


def main(argv: list[str] | None = None, *, client=None, now=now_local, calendar: TradingCalendar | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config)
    after_close = timedelta(minutes=args.after_close_min)

    if args.cmd == "init":
        if cfg_path.exists():
            print(f"[중단] 이미 있음: {cfg_path}")
            return 2
        _atomic_write(cfg_path, dump_document(empty_document()))
        print(f"만듦: {cfg_path} (종목 없음 — add로 추가)")
        return 0

    calendar = calendar or TradingCalendar.load()
    with ResearchStore(args.db) as rstore, WatchStore(args.watch_db) as wstore:
        listing, snap_id = listing_from_store(rstore)

        if args.cmd == "validate":
            text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
            res = check_text(text, listing, check_list=not args.no_list)
            print(f"{cfg_path}: {'정상' if res.ok else '오류'}" + ("" if snap_id is None or args.no_list
                                                                 else f" (종목 목록 스냅숏 {snap_id} 대조)"))
            _print_issues([vars(e) for e in res.errors], [vars(w) for w in res.warnings])
            return 0 if res.ok else 2

        if args.cmd in ("apply", "status", "history"):
            if args.cmd != "history":
                state, att = sync_config(wstore, cfg_path, listing, snap_id, now=now())
                if args.cmd == "apply":
                    print(f"v{att['version']} {att['status']}" + ("" if att["new"] else " (직전과 같음 — 새 기록 없음)"))
                    _print_issues(att["errors"], att["warnings"])
                if args.cmd == "status" and args.json:
                    print_json({**state.summary(), "readiness": wstore.readiness(),
                                "prepare_runs": wstore.prepare_runs(5)})
                else:
                    _status_table(state, wstore, listing)
                if args.cmd == "apply":
                    return 0 if att["status"] == "APPLIED" else 2
                return 0 if state.can_monitor else 2
            for h in wstore.history(args.limit):
                print(f"v{h['version']} {h['attempted_at']} {h['status']} {h['origin']} hash={h['config_hash']}"
                      f" 목록={h['list_snapshot_id']}" + (f" 오류 {len(h['errors'])}" if h["errors"] else ""))
            return 0

        if client is None:
            client = _LazyClient(lambda: make_client(args))
        collector = ResearchCollector(client, rstore, calendar, after_close=after_close, log=print)

        if args.cmd in ("add", "set", "enable", "disable", "holding-close", "remove"):
            if cfg_path.exists():
                raw, perr = parse_text(cfg_path.read_text(encoding="utf-8"))
                if perr:
                    print(f"[중단] 지금 파일을 읽을 수 없음 — 직접 고친 뒤 apply: {perr}")
                    return 2
                doc = raw if isinstance(raw, dict) else empty_document()
            else:
                doc = empty_document()
            try:
                what = edit_document(doc, args)
            except EditError as exc:
                print(f"[거부] {exc}")
                return 2
            text = dump_document(doc)
            res = check_text(text, listing)
            if not res.ok:
                print("[거부] 바꾼 결과가 검증을 통과하지 못해 파일을 바꾸지 않았습니다:")
                _print_issues([vars(e) for e in res.errors], [vars(w) for w in res.warnings])
                return 2
            _atomic_write(cfg_path, text)
            state, att = sync_config(wstore, cfg_path, listing, snap_id, now=now(), origin=f"CLI:{what}")
            print(f"{what} → v{att['version']} {att['status']}")
            _print_issues([], att["warnings"])
            if args.cmd == "add" and not args.no_fetch and state.config.symbol(_code(args.code)).active:
                try:
                    res2 = prepare_data(wstore, rstore, collector, calendar, state, listing, now=now,
                                        codes=[_code(args.code)], after_close=after_close, log=print)
                except (ResearchConfigError, ResearchApiError, CollectError) as exc:
                    print(f"[과거 일봉 수집 못 함 — 설정은 적용됨, 나중에 prepare] {exc}")
                    return 1
                r = next(x for x in res2["rows"] if x["code"] == _code(args.code) and x["kind"] == "STOCK")
                print(f"데이터: {_ready_cell(r)} (준비 실행 {res2['run_id']}, 조회 {res2['calls']}회)")
            return 0

        if args.cmd == "prepare":
            snap = rstore.latest_snapshot()
            if not args.no_list_refresh and (snap is None or snap["snapshot_date"] < now().date().isoformat()):
                s = collector.snapshot_universe()            # 목록 조회(2회)만 — 전체 시장 일봉 수집은 하지 않음
                print(f"종목 목록 스냅숏 {s['snapshot_id']} (오늘 첫 준비)")
                listing, snap_id = listing_from_store(rstore)
            state, att = sync_config(wstore, cfg_path, listing, snap_id, now=now())
            if att["errors"]:
                print(f"[설정 오류] v{att['version']} 거부 — " + ("마지막 정상 설정으로 진행" if state.can_monitor
                                                            else "정상 설정이 없어 시작 안 함"))
                _print_issues(att["errors"], [])
            codes = [_code(c) for c in args.codes.split(",")] if args.codes else None
            try:
                res = prepare_data(wstore, rstore, collector, calendar, state, listing, now=now, codes=codes,
                                   after_close=after_close, log=print)
            except WatchNotReady as exc:
                print(f"[시작 안 함] {exc}")
                return 2
            print_json({k: v for k, v in res.items() if k != "rows"})
            _status_table(state, wstore, listing)
            return 0 if res["status"] == "COMPLETE" else 1
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
        print("\n[중단] 사용자 중단 — 진행 중이던 종목만 저장되지 않았습니다. 다시 prepare하면 이어서 받습니다.")
        sys.exit(130)
