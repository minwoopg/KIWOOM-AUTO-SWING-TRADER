"""원장 검토·최소 정정 (M0 커밋 2) — 브로커·API 호출 없음.

    python tools/ledger_review.py status                      # 원장 보유·최근 검토 기록
    python tools/ledger_review.py note 005930 --reason "HTS와 일치 확인"
    python tools/ledger_review.py correct 005930 --side BUY --qty 1 --price 251000 --trade-date 2026-10-14 \
        --order-id 0012345 --evidence "HTS 체결내역 10/14 09:12 1주 251,000" --reason "재시작 중 체결 누락"   # 미리보기
    ... 위 명령 + --confirm                                      # 실제 기록

- 정정 = 브로커 근거가 있는 빠진 체결을 원장에 **추가**(BROKER_FILL). 원장 줄 수정·삭제 없음, 같은 주문번호·수량은 한 번만.
- 매도 정정은 원장 보유 수량을 넘을 수 없음. 원장 손상이면 거부.
- ERROR·orphan은 풀지 않음 — 정정 뒤에도 `commands/ack_error_*.json`(recovery_id) 복구 명령이 따로 필요.
- 실행기(`app.main`)·M1 수동 주문 도구가 돌고 있으면 잠금으로 거부.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import load_settings  # noqa: E402
from domain.service.lot_ledger import LotMatchError, apply_events  # noqa: E402
from infra.broker.execution_mode import ExecutionModeError, resolve_execution_mode  # noqa: E402
from infra.storage.execution_guard import order_path_locks  # noqa: E402
from infra.storage.fill_ledger import FillLedgerCorruptError, FillLedgerStore  # noqa: E402
from infra.storage.ledger_review import (  # noqa: E402
    LedgerReviewError, LedgerReviewStore, correction_event, new_review_id, review_path,
)


def _held(events, symbol: str) -> int:
    pos = apply_events(events).positions()
    return pos[symbol].quantity if symbol in pos else 0


def main(argv=None, *, settings=None, now=None) -> int:
    ap = argparse.ArgumentParser(description="원장 검토·최소 정정 (API 호출 없음)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    n = sub.add_parser("note")
    n.add_argument("symbol")
    n.add_argument("--reason", required=True)
    c = sub.add_parser("correct")
    c.add_argument("symbol")
    c.add_argument("--side", required=True, choices=["BUY", "SELL"])
    c.add_argument("--qty", required=True, type=int)
    c.add_argument("--price", required=True, type=int)
    c.add_argument("--trade-date", required=True)
    c.add_argument("--order-id", required=True)
    c.add_argument("--evidence", required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--confirm", action="store_true", help="실제 기록(없으면 미리보기만)")
    a = ap.parse_args(argv)

    if settings is None:
        from app.main import load_dotenv
        load_dotenv(str(ROOT / ".env"))
        settings = load_settings(ROOT / "config" / "settings.yaml")
    now = now or datetime.now()
    try:
        mode = resolve_execution_mode(settings.broker)
    except ExecutionModeError as exc:
        print(f"[중단] 실행 모드 거부: {exc}")
        return 2
    scope = settings.broker.account_scope_id.strip() or "unscoped"
    ledger = FillLedgerStore(settings.storage.fill_ledger_file)
    reviews = LedgerReviewStore(review_path(settings.storage.fill_ledger_file))

    try:
        events = ledger.load()
        positions = apply_events(events).positions()
    except (FillLedgerCorruptError, LotMatchError) as exc:
        print(f"[중단] 원장을 신뢰할 수 없음 — 정정 도구도 쓰지 않음(파일을 사람이 확인): {exc}")
        return 2

    if a.cmd == "status":
        print(f"실행 모드 {mode} · 계좌 라벨 {scope} · 원장 사건 {len(events)}건")
        for sym, p in sorted(positions.items()):
            print(f"  {sym}: {p.quantity}주")
        if not positions:
            print("  보유 없음")
        for r in reviews.load()[-10:]:
            print(f"  [{r['at']}] {r['action']} {r['symbol']} — {r['reason']} ({r['review_id']})")
        return 0

    try:
        with order_path_locks(settings):
            if a.cmd == "note":
                rec = reviews.append(action="NOTE", scope=scope, mode=mode, symbol=a.symbol, reason=a.reason,
                                     ledger_qty_before=_held(events, a.symbol),
                                     ledger_qty_after=_held(events, a.symbol), now=now)
                print(f"[검토] {rec['review_id']} 기록 — 원장 변경 없음")
                return 0
            try:
                td = date.fromisoformat(a.trade_date)
            except ValueError:
                print(f"[사용법 오류] --trade-date {a.trade_date!r}")
                return 2
            if a.qty <= 0 or a.price <= 0:
                print("[사용법 오류] --qty·--price는 양수")
                return 2
            if td > now.date():
                print("[사용법 오류] 미래 거래일")
                return 2
            before = _held(events, a.symbol)
            if a.side == "SELL" and a.qty > before:
                print(f"[거부] 매도 정정 {a.qty}주 > 원장 보유 {before}주")
                return 2
            rid = new_review_id(now)
            ev = correction_event(scope=scope, review_id=rid, side=a.side, symbol=a.symbol, quantity=a.qty,
                                  price=a.price, trade_date=td, order_id=a.order_id, reason=a.reason, now=now)
            if any(e.event_id == ev.event_id for e in events):
                print(f"[거부] 같은 정정(주문 {a.order_id}·{a.qty}주)이 이미 원장에 있음: {ev.event_id}")
                return 2
            after = before + (a.qty if a.side == "BUY" else -a.qty)
            print(f"[정정 {'기록' if a.confirm else '미리보기'}] {a.symbol} {a.side} {a.qty}주 @ {a.price:,} "
                  f"(거래일 {td}, 주문 {a.order_id}) · 원장 보유 {before} → {after}주")
            print(f"  근거: {a.evidence}\n  사유: {a.reason}")
            if not a.confirm:
                print("  --confirm 없이 기록하지 않음")
                return 0
            ledger.append(ev)
            reviews.append(action="FILL_CORRECTION", scope=scope, mode=mode, symbol=a.symbol, reason=a.reason,
                           evidence=a.evidence, fill_event_id=ev.event_id, ledger_qty_before=before,
                           ledger_qty_after=after, now=now, review_id=rid)
            print(f"  기록됨 {rid}. ERROR·orphan이 있으면 복구 명령(ack_error/ack_orphan)이 따로 필요 — 자동 해소 안 함")
            return 0
    except LedgerReviewError as exc:
        print(f"[거부] {exc}")
        return 2
    except RuntimeError as exc:
        if "중복 실행 차단" in str(exc):
            print(f"[중단] 실행기·주문 도구가 돌고 있음 — 끝난 뒤 다시: {exc}")
            return 2
        raise


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
