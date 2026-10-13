from __future__ import annotations

"""체결 근거 (M3 A2) — 원장 사건 하나에 붙는 브로커 체결가·수수료·세금.

원장(`fill_ledger.jsonl`)은 수정하지 않습니다(append-only). 원장에 추정가(ORDER_ESTIMATE)나
잔고 역산 원가(BROKER_AVG)로 기록된 사건도, 나중에 주문별 체결조회(ka10076)로 실제 체결가를
확인하면 **별도 기록**(`fill_evidence.jsonl`)을 하나 추가합니다. 손익·비용 계산은
`apply_fill_evidence()`로 원장 사건에 이 기록을 겹쳐 "실제 체결가"를 씁니다.

규칙
- 사건 하나에 근거 기록 하나(event_id 기준). 같은 내용이 다시 오면 무시, 다른 가격·수량이면
  덮어쓰지 않고 충돌(사람 확인).
- 근거는 주문번호·종목·방향·수량이 원장 사건과 정확히 같을 때만 적용합니다. 다르면 적용하지 않고
  `mismatched`로 보고합니다.
- 원장 사건이 이미 BROKER_FILL이면 가격은 그대로 두고(같은 근거), 가격이 다르면 `mismatched`.
- 수량·보유·진입일 계산에는 영향이 없습니다(가격만 바뀜).
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable

from domain.position.fill_event import FillEvent

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_ORIGINS = ("AUTO", "AUTO_RETRY", "CONFIRM_TOOL")


class FillEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class FillDetail:
    """주문 하나의 브로커 체결 근거 (키움 ka10076 체결 행, 2026-10-13 실측 — `infra/broker/fill_detail.py`).

    전량 체결 signature(ord_stt=체결 · ord_qty=cntr_qty · oso_qty=0)가 정확히 맞는 **한 행**에서만 만듭니다.
    side: "BUY"/"SELL"(알 수 없으면 None). commission / tax: 그 주문의 당일 수수료·세금
    (tdy_trde_cmsn / tdy_trde_tax) — 해석할 수 없으면 None(0으로 위장하지 않음).
    """

    order_id: str
    symbol: str
    side: str | None
    quantity: int
    price: int
    commission: int | None = None
    tax: int | None = None
    source: str = "ka10076"
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FillEvidenceRecord:
    event_id: str
    order_id: str
    symbol: str
    side: str            # BUY / SELL
    quantity: int
    price: int
    commission: int | None
    tax: int | None
    source: str          # "ka10076"
    origin: str          # AUTO / AUTO_RETRY / CONFIRM_TOOL
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not str(self.event_id).strip() or not str(self.order_id).strip():
            raise FillEvidenceError("event_id·order_id가 비어 있음")
        if self.side not in ("BUY", "SELL"):
            raise FillEvidenceError(f"side={self.side!r}")
        for name in ("quantity", "price"):
            v = getattr(self, name)
            if type(v) is not int or v <= 0:
                raise FillEvidenceError(f"{name}는 양의 정수 — {v!r}")
        for name in ("commission", "tax"):
            v = getattr(self, name)
            if v is not None and (type(v) is not int or v < 0):
                raise FillEvidenceError(f"{name}는 0 이상 정수 또는 None — {v!r}")
        if self.origin not in EVIDENCE_ORIGINS:
            raise FillEvidenceError(f"origin={self.origin!r}")

    def content_key(self) -> tuple:
        """중복 판단용 — 기록 시각·출처 경로는 제외."""
        return (self.event_id, self.order_id, self.symbol, self.side, self.quantity, self.price,
                self.commission, self.tax, self.source)

    def to_dict(self) -> dict:
        return {"v": EVIDENCE_SCHEMA_VERSION, "event_id": self.event_id, "order_id": self.order_id,
                "symbol": self.symbol, "side": self.side, "quantity": self.quantity, "price": self.price,
                "commission": self.commission, "tax": self.tax, "source": self.source,
                "origin": self.origin, "recorded_at": self.recorded_at.isoformat()}

    @classmethod
    def from_dict(cls, d: dict) -> "FillEvidenceRecord":
        if d.get("v") != EVIDENCE_SCHEMA_VERSION:
            raise FillEvidenceError(f"스키마 버전 불일치: {d.get('v')!r}")
        return cls(event_id=str(d["event_id"]), order_id=str(d["order_id"]), symbol=str(d["symbol"]),
                   side=str(d["side"]), quantity=d["quantity"], price=d["price"],
                   commission=d.get("commission"), tax=d.get("tax"), source=str(d.get("source", "")),
                   origin=str(d["origin"]), recorded_at=datetime.fromisoformat(d["recorded_at"]))


def record_for_event(event: FillEvent, detail, *, origin: str, now: datetime) -> FillEvidenceRecord | None:
    """원장 사건과 브로커 체결 근거(`FillDetail`)가 같은 주문·방향·수량일 때만 근거 기록을 만듦."""
    if detail is None or not event.order_id:
        return None
    side = getattr(detail.side, "value", detail.side)
    if (str(detail.order_id).lstrip("0") != str(event.order_id).lstrip("0") or detail.symbol != event.symbol
            or side != event.kind or detail.quantity != event.quantity):
        return None
    return FillEvidenceRecord(
        event_id=event.event_id, order_id=event.order_id, symbol=event.symbol, side=event.kind,
        quantity=event.quantity, price=detail.price, commission=detail.commission, tax=detail.tax,
        source=getattr(detail, "source", "ka10076"), origin=origin, recorded_at=now)


@dataclass(frozen=True)
class EvidenceOverlay:
    events: list            # 실제 체결가를 겹친 사건 목록(원장 순서)
    applied: dict           # event_id → FillEvidenceRecord
    mismatched: list        # 적용하지 못한 근거 설명

    @property
    def confirmed_ids(self) -> set:
        return set(self.applied)


def apply_fill_evidence(events: Iterable[FillEvent], records: Iterable[FillEvidenceRecord]) -> EvidenceOverlay:
    by_id: dict[str, FillEvidenceRecord] = {}
    mismatched: list[str] = []
    for r in records:
        prev = by_id.get(r.event_id)
        if prev is not None and prev.content_key() != r.content_key():
            mismatched.append(f"{r.event_id}: 근거 기록이 서로 다름 — 적용 안 함")
            by_id[r.event_id] = None  # type: ignore[assignment]
            continue
        if prev is None and r.event_id in by_id:
            continue   # 이미 충돌로 막힌 사건
        by_id[r.event_id] = r
    out: list[FillEvent] = []
    applied: dict[str, FillEvidenceRecord] = {}
    seen_ids: set[str] = set()
    for ev in events:
        seen_ids.add(ev.event_id)
        r = by_id.get(ev.event_id)
        if r is None:
            out.append(ev)
            continue
        if (r.order_id.lstrip("0") != str(ev.order_id).lstrip("0") or r.symbol != ev.symbol
                or r.side != ev.kind or r.quantity != ev.quantity):
            mismatched.append(f"{ev.event_id}: 주문번호·종목·방향·수량이 원장과 다름 — 적용 안 함")
            out.append(ev)
            continue
        if ev.price_source == "BROKER_FILL":
            if ev.price != r.price:
                mismatched.append(f"{ev.event_id}: 원장 체결가 {ev.price:,} ≠ 근거 {r.price:,} — 원장 유지")
            else:
                applied[ev.event_id] = r
            out.append(ev)
            continue
        applied[ev.event_id] = r
        out.append(replace(ev, price=r.price, price_source="BROKER_FILL",
                           note=(ev.note + " | " if ev.note else "") +
                           f"체결가 확인 {r.source} (원장 {ev.price_source} {ev.price:,})"))
    for eid in by_id:
        if eid not in seen_ids and by_id[eid] is not None:
            mismatched.append(f"{eid}: 원장에 없는 사건의 근거 — 적용 안 함")
    return EvidenceOverlay(out, applied, mismatched)
