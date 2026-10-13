from __future__ import annotations

"""원장 검토·정정 기록 — append-only (M0 커밋 2, GPT 지시서 2026-10-13).

체결 원장(`fill_ledger.jsonl`)은 수정·삭제하지 않습니다. 사람이 브로커 근거(HTS 체결내역·주문번호)를 확인한 뒤
**빠진 체결 사건을 추가**하는 것만 정정입니다. 이 파일(`ledger_reviews.jsonl`)에는 그 정정과 단순 검토 메모를
누가 언제 어떤 근거·사유로 했는지 남깁니다.

하지 않는 것
- 근거 없는 체결 생성, 과거 줄 수정·삭제, 불확실한 주문(ERROR·orphan)의 자동 해소 — ERROR·orphan은 기존 복구 명령
  (`commands/ack_error_*.json`·`ack_orphan_*.json`, recovery_id 필요)으로만 풀림.
- 정정으로 마감 검증(VERIFIED)을 만들지 않음 — 다음 기동의 장부 대조가 다시 판정.
"""

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path

from domain.position.fill_event import FillEvent

REVIEW_SCHEMA = 1
ACTIONS = ("NOTE", "FILL_CORRECTION")


class LedgerReviewError(RuntimeError):
    pass


def review_path(fill_ledger_file: str | Path) -> Path:
    return Path(fill_ledger_file).with_name("ledger_reviews.jsonl")


def correction_event(*, scope: str, review_id: str, side: str, symbol: str, quantity: int, price: int,
                     trade_date: date, order_id: str, reason: str, now: datetime) -> FillEvent:
    """사람 확인 정정 체결. event_id는 브로커 주문번호·수량 기준이라 같은 정정을 두 번 넣지 못함."""
    if side not in ("BUY", "SELL"):
        raise LedgerReviewError(f"side={side!r}")
    if not str(order_id).strip():
        raise LedgerReviewError("브로커 주문번호(--order-id) 필수 — 근거 없는 체결은 만들지 않음")
    return FillEvent(
        event_id=f"CORR|{scope}|{trade_date.isoformat()}|{side}|{symbol}|{order_id}|{quantity}",
        kind=side, symbol=symbol, quantity=quantity, price=price, price_source="BROKER_FILL",
        trade_date=trade_date, occurred_at=now, order_id=str(order_id),
        note=f"MANUAL_CORRECTION {review_id}: {reason}")


class LedgerReviewStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for i, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise LedgerReviewError(f"{self.path}:{i}: 읽을 수 없는 줄 — {exc}") from exc
        return out

    def append(self, *, action: str, scope: str, mode: str, symbol: str, reason: str, evidence: str = "",
               fill_event_id: str = "", ledger_qty_before: int | None = None, ledger_qty_after: int | None = None,
               now: datetime, review_id: str | None = None) -> dict:
        if action not in ACTIONS:
            raise LedgerReviewError(f"action={action!r}")
        if not reason.strip():
            raise LedgerReviewError("사유(--reason) 필수")
        if action == "FILL_CORRECTION" and not evidence.strip():
            raise LedgerReviewError("정정에는 브로커 근거(--evidence) 필수")
        rec = {"v": REVIEW_SCHEMA, "review_id": review_id or new_review_id(now),
               "at": now.isoformat(timespec="seconds"), "action": action, "account_scope": scope,
               "execution_mode": mode, "symbol": symbol, "reason": reason, "evidence": evidence,
               "fill_event_id": fill_event_id, "ledger_qty_before": ledger_qty_before,
               "ledger_qty_after": ledger_qty_after}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return rec


def new_review_id(now: datetime) -> str:
    return f"REV-{now:%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}"
