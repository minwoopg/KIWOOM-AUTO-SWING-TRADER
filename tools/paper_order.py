"""M1 주문 경로 실측 — 키움 모의투자 계좌 1주 수동 주문 (M0 커밋 3, GPT 지시서 2026-10-13).

    python tools/paper_order.py status
    python tools/paper_order.py check-orders                      # 오늘 M1 주문들의 주문·체결 조회(읽기 전용)
    python tools/paper_order.py check-orders --order 0112293:005930 --order 0112348:005930
    python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014            # 미리보기(네트워크 없음)
    python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014 --execute  # 실제 모의 주문
    python tools/paper_order.py sell-held 005930 --command-id m1-sell-1014 --execute

전략과 무관한 **주문 경로 시험**입니다. 하루 수명주기와 같은 OrderExecutor·FillRecorder·안전 한도를 그대로 쓰고,
전략 게이트만 없습니다(계좌·복구·금액·시간대 한도는 그대로 — 09:05~15:15 정규장).

- 실행 모드 KIWOOM_PAPER만(`use_mock: false`, mockapi 주소). MOCK_LOCAL·실전은 거부.
- 1주 시장가 고정. sell-held는 원장·잔고 둘 다 1주 이상일 때만.
- `--command-id`는 한 번만 — 기록(`data/paper_orders.jsonl`)에 있으면 결과와 상관없이 다시 실행하지 않음(새 ID로).
  기록은 네트워크 전에 먼저 남김(중간에 끊겨도 같은 명령이 다시 나가지 않게).
- 기동 점검은 app.main과 같음: 인증·잔고·미해결 주문 흔적·원장·장부 대조. 미해결 주문·대조 불일치·원장 오류가 있으면 주문 안 함.
- 체결·원장 기록·주문 종료를 최대 `--max-wait`초(기본 180) 확인. 그 안에 끝나지 않으면 UNRESOLVED — 재전송 없음,
  다음 기동에서 ERROR로 복원되고 사람 확인(HTS → ack_error / 원장 정정 도구).
- 키움 응답 원문은 비밀값(토큰·앱키·계좌번호)을 지워 `data/m1_fixtures/<명령 ID>/`에 저장.
- 실행기(`app.main`)·원장 정정 도구와 동시에 돌지 않음(잠금). 작업 스케줄러 실행 시간과 겹치지 않게.
"""
from __future__ import annotations

import argparse
import time as _time
import asyncio
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import load_settings  # noqa: E402
from domain.service.lot_ledger import LotMatchError, apply_events  # noqa: E402
from domain.strategy.interface import OrderIntent  # noqa: E402
from infra.broker.execution_mode import KIWOOM_PAPER, ExecutionModeError, resolve_execution_mode  # noqa: E402
from infra.storage.execution_guard import DataModeMismatchError, commit_data_mode, order_path_locks  # noqa: E402
from infra.storage.fill_ledger import FillLedgerCorruptError, FillLedgerStore  # noqa: E402

COMMAND_ID = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
PROBE_QTY = 1


def registry_path(settings) -> Path:
    return Path(settings.storage.state_file).resolve().parent / "paper_orders.jsonl"


def fixtures_dir(settings, command_id: str) -> Path:
    return Path(settings.storage.state_file).resolve().parent / "m1_fixtures" / command_id


def load_registry(settings) -> list[dict]:
    p = registry_path(settings)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise RuntimeError(f"{p}:{i}: 읽을 수 없는 줄 — 사람 확인(명령 중복 여부를 알 수 없음): {exc}") from exc
    return out


def append_registry(settings, rec: dict) -> None:
    p = registry_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _logger(settings):
    from infra.storage.logger import build_app_logger
    return build_app_logger(settings.storage.app_log_file, settings.app.log_level)


def main(argv=None, *, settings=None, broker=None, clock=None, sleep=None, logger=None) -> int:
    ap = argparse.ArgumentParser(description="M1: 키움 모의투자 1주 수동 주문 경로 실측")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    co = sub.add_parser("check-orders", help="주문번호별 미체결(ka10075)·체결(ka10076) 조회 — 읽기 전용, 원문 저장")
    co.add_argument("--order", action="append", default=[], help="주문번호:종목코드 (여러 번). 없으면 기록의 그날 주문")
    co.add_argument("--date", help="기록에서 고를 날짜 YYYY-MM-DD(기본 오늘)")
    for name in ("buy-one", "sell-held"):
        p = sub.add_parser(name)
        p.add_argument("symbol")
        p.add_argument("--command-id", required=True)
        p.add_argument("--execute", action="store_true", help="실제 모의 주문(없으면 미리보기 — 네트워크 없음)")
        p.add_argument("--max-wait", type=float, default=180.0, help="체결·종료 확인 최대 대기(초)")
        p.add_argument("--poll", type=float, default=3.0, help="확인 간격(초, 2 이상)")
    a = ap.parse_args(argv)

    if settings is None:
        from app.main import load_dotenv
        load_dotenv(str(ROOT / ".env"))
        settings = load_settings(ROOT / "config" / "settings.yaml")
    if clock is None:
        from utils.time_utils import now_local
        clock = now_local
    try:
        mode = resolve_execution_mode(settings.broker)
    except ExecutionModeError as exc:
        print(f"[중단] 실행 모드 거부: {exc}")
        return 2
    scope = settings.broker.account_scope_id.strip() or "unscoped"

    if a.cmd == "status":
        print(f"실행 모드 {mode} · 계좌 라벨 {scope}")
        for r in load_registry(settings)[-12:]:
            print(f"  [{r.get('at')}] {r.get('command_id')} {r.get('event')} {r.get('side', '')} {r.get('symbol', '')} "
                  f"{r.get('status', '')} {r.get('order_id', '') or ''}")
        return 0

    if a.cmd == "check-orders":
        if broker is None and mode != KIWOOM_PAPER:
            print(f"[중단] 실행 모드 {mode} — 주문 조회는 키움 모의투자(KIWOOM_PAPER)에서만")
            return 2
        return _check_orders(a, settings, broker=broker, clock=clock, sleep=sleep)

    if broker is None and mode != KIWOOM_PAPER:
        print(f"[중단] 실행 모드 {mode} — M1은 키움 모의투자(KIWOOM_PAPER)에서만. settings.yaml `use_mock: false`(mockapi 주소) 확인")
        return 2
    if not COMMAND_ID.match(a.command_id):
        print("[사용법 오류] --command-id는 영문·숫자·_·- 3~64자")
        return 2
    if a.poll < 2:
        print("[사용법 오류] --poll은 2초 이상(호출 빈도)")
        return 2
    side = "BUY" if a.cmd == "buy-one" else "SELL"
    sym = a.symbol.strip()
    if not re.fullmatch(r"\d{6}", sym):
        print("[사용법 오류] 종목코드 6자리")
        return 2

    try:
        with order_path_locks(settings):
            return _run(a, settings, mode, scope, side, sym, broker=broker, clock=clock, sleep=sleep, logger=logger)
    except RuntimeError as exc:
        if "중복 실행 차단" in str(exc):
            print(f"[중단] 실행기·정정 도구가 돌고 있음(잠금) — 끝난 뒤 다시: {exc}")
            return 2
        raise


def _run(a, settings, mode, scope, side, sym, *, broker, clock, sleep, logger) -> int:
    reg = load_registry(settings)
    if any(r.get("command_id") == a.command_id for r in reg):
        print(f"[거부] 명령 ID {a.command_id}는 이미 기록됨 — 같은 명령을 다시 내지 않음(새 ID로, 결과는 status)")
        return 2
    try:
        events = FillLedgerStore(settings.storage.fill_ledger_file).load()
        positions = apply_events(events).positions()
    except (FillLedgerCorruptError, LotMatchError) as exc:
        print(f"[중단] 원장을 신뢰할 수 없음: {exc}")
        return 2
    held = positions[sym].quantity if sym in positions else 0
    if side == "SELL" and held < PROBE_QTY:
        print(f"[거부] 원장 보유 {held}주 — sell-held는 이 프로그램 원장에 기록된 보유만 팜")
        return 2
    print(f"[M1] {'실행' if a.execute else '미리보기'} · 모드 {mode} · 계좌 라벨 {scope} · {sym} {side} {PROBE_QTY}주 시장가 · "
          f"명령 {a.command_id} · 원장 보유 {held}주")
    if not a.execute:
        print("  --execute 없이 네트워크·주문 없음. 실행 전: 작업 스케줄러 실행기와 시간이 겹치지 않는지, 관리자와 같은 앱키라면 "
              "호출 빈도 확인")
        return 0

    now = clock()
    base = {"command_id": a.command_id, "side": side, "symbol": sym, "quantity": PROBE_QTY,
            "execution_mode": mode, "account_scope": scope}
    append_registry(settings, {**base, "event": "STARTED", "at": now.isoformat(timespec="seconds")})

    from app.main import StartupBlockedError, build_session_runner, run_startup_checks
    log = logger or _logger(settings)
    recording = broker is None
    if recording:
        from infra.broker.response_recorder import RecordingPaperKiwoomBroker
        broker = RecordingPaperKiwoomBroker(settings.broker)
    result: dict = {}
    try:
        rep = asyncio.run(run_startup_checks(settings, broker, log))
        problems = []
        if rep.has_unresolved_orders:
            problems.append("미해결 주문 흔적")
        if rep.ledger_error:
            problems.append(f"원장 오류 {rep.ledger_error}")
        if rep.reconcile is None or not rep.reconcile.ok:
            problems.append("장부 대조 불일치·미확인(계좌에 원장 밖 보유가 있으면 먼저 정리)")
        if problems:
            result = {"status": "REFUSED", "reason": "; ".join(problems)}
        else:
            if rep.data_mode_action:
                commit_data_mode(settings, rep.execution_mode or mode, rep.data_mode_action)
            runner, executor = build_session_runner(settings, broker, log, clock=clock, sleep=sleep)
            try:
                price = _price(broker, sym, log)
                (sleep or _time.sleep)(1.5)      # 시세 조회 직후 잔고 조회가 붙지 않게(요청 간격)
                result = runner.run_probe(OrderIntent(sym, side, PROBE_QTY, price, reason="M1 수동 주문 경로 실측",
                                                      context={"m1_command_id": a.command_id}),
                                          max_wait_sec=a.max_wait, poll_sec=a.poll)
                result["reference_price"] = price
            finally:
                executor.shutdown()
    except (StartupBlockedError, DataModeMismatchError) as exc:
        result = {"status": "REFUSED", "reason": str(exc)}
    except Exception as exc:          # noqa: BLE001 — 기록 후 다시 올림
        result = {"status": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
        append_registry(settings, {**base, "event": "FINISHED", "at": clock().isoformat(timespec="seconds"), **result})
        _save(broker, settings, a.command_id, recording)
        raise
    append_registry(settings, {**base, "event": "FINISHED", "at": clock().isoformat(timespec="seconds"), **result})
    saved = _save(broker, settings, a.command_id, recording)
    print(f"[M1] 결과 {result.get('status')} · 주문번호 {result.get('order_id') or '-'} · 원장 기록 "
          f"{result.get('fills_recorded', 0)}건 · 장부 대조 {result.get('reconcile_ok')} · 사유 {result.get('reason', '') or '-'}")
    if saved:
        print(f"  응답 원문(비밀값 제거) {len(saved)}개: {saved[0].parent}")
    if result.get("status") == "UNRESOLVED":
        print("  주문 종료를 확인하지 못함 — 재전송하지 않음. HTS에서 체결 확인 → 다음 기동의 복구 안내(ack_error)·원장 정정 도구")
    return 0 if result.get("status") == "FILLED" else 1


def _check_orders(a, settings, *, broker, clock, sleep) -> int:
    """주문번호별 주문·체결 조회(읽기 전용). 원장·상태·명령 기록을 바꾸지 않음. 종목마다 ka10075·ka10076 한 번씩."""
    from infra.broker.kiwoom_order_status import build_order_status_evidence, normalize_order_id
    targets: list[tuple[str, str]] = []
    for item in a.order:
        oid, _, sym = item.partition(":")
        if not oid.strip() or not re.fullmatch(r"\d{6}", sym.strip()):
            print(f"[사용법 오류] --order 주문번호:종목코드 — {item!r}")
            return 2
        targets.append((oid.strip(), sym.strip()))
    if not targets:
        day = a.date or clock().date().isoformat()
        for r in load_registry(settings):
            if r.get("event") == "FINISHED" and r.get("order_id") and str(r.get("at", "")).startswith(day):
                targets.append((str(r["order_id"]), str(r["symbol"])))
    if not targets:
        print("[조회] 대상 주문 없음 — --order 주문번호:종목코드로 지정")
        return 2
    recording = broker is None
    if recording:
        from infra.broker.response_recorder import RecordingPaperKiwoomBroker
        broker = RecordingPaperKiwoomBroker(settings.broker)
    pause = sleep or _time.sleep
    broker.authenticate()
    out, ok = [], True
    symbols = sorted({s for _, s in targets})
    for i, sym in enumerate(symbols):
        if i:
            pause(1.2)                   # 같은 TR 1초 1회(실측 유량=1)
        try:
            oso = broker._fetch_open_orders_raw(sym)
            pause(1.2)
            cntr = broker._fetch_fill_history_raw(sym)
        except Exception as exc:         # noqa: BLE001
            print(f"[조회 실패] {sym}: {type(exc).__name__}: {exc}")
            ok = False
            continue
        for oid, s in targets:
            if s != sym:
                continue
            ev = build_order_status_evidence(oid, sym, oso, cntr)
            bo = ev.broker_order
            rec = {"order_id": oid, "normalized": normalize_order_id(oid), "symbol": sym, "status": bo.status.value,
                   "side": bo.side.value if bo.side else None, "requested": bo.requested_quantity,
                   "open": bo.open_quantity, "filled": bo.filled_quantity, "filled_price": bo.filled_price,
                   "order_type_raw": bo.order_type_raw, "matched_cntr": len(ev.matched_cntr_entries),
                   "matched_oso": len(ev.matched_oso_entries), "evidence_error": ev.evidence_error,
                   "cntr_rows": ev.matched_cntr_entries}
            out.append(rec)
            print(f"[조회] {oid} {sym} {rec['status']} {rec['side'] or '-'} 요청 {rec['requested']} 체결 {rec['filled']} "
                  f"@ {rec['filled_price']} 미체결 {rec['open']} · 체결행 {rec['matched_cntr']}개 · 유형 {rec['order_type_raw']}")
            if len(ev.matched_cntr_entries) > 1:
                print(f"  여러 체결 행(분할 체결 가능) — 원문 확인: {ev.matched_cntr_entries}")
    folder = Path(settings.storage.state_file).resolve().parent / "m1_fixtures" / f"check-{clock():%Y%m%d_%H%M%S}"
    if recording and getattr(broker, "recorded", None):
        try:
            saved = broker.save_fixtures(folder)
            from infra.broker.response_recorder import redact
            (folder / "orders.json").write_text(json.dumps(redact(out, broker.secrets()), ensure_ascii=False, indent=2),
                                               encoding="utf-8")
            print(f"  응답 원문(비밀값 제거) {len(saved)}개 + orders.json: {folder}")
        except Exception as exc:         # noqa: BLE001
            print(f"  [경고] 원문 저장 실패: {type(exc).__name__}: {exc}")
    return 0 if ok and all(r["status"] in ("FILLED",) for r in out) else 1


def _price(broker, sym: str, log) -> int:
    from infra.broker.kiwoom_broker import KiwoomBroker
    if isinstance(broker, KiwoomBroker):
        from infra.market_data.quote_source import KiwoomQuoteSource
        px = KiwoomQuoteSource(broker, logger=log).get_prices([sym]).get(sym, 0)
    else:
        px = broker.get_market_price(sym).current_price
    if not px or px <= 0:
        raise RuntimeError(f"{sym} 현재가 확인 실패")
    return int(px)


def _save(broker, settings, command_id: str, recording: bool) -> list[Path]:
    if not recording or not getattr(broker, "recorded", None):
        return []
    try:
        return broker.save_fixtures(fixtures_dir(settings, command_id))
    except Exception as exc:          # noqa: BLE001
        print(f"  [경고] 응답 원문 저장 실패: {type(exc).__name__}: {exc}")
        return []


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
