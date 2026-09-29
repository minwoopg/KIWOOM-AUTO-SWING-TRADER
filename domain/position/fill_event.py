from __future__ import annotations

"""체결 사건 모델 (스윙 분리 4라운드, 2026-09-28).

저장은 `infra/storage/fill_ledger.py`, 계산은 `domain/service/lot_ledger.py`.
가격 출처 구분은 저장소 모듈 docstring 참고.
"""

from dataclasses import dataclass
from datetime import date, datetime

FILL_KINDS = ("BUY", "SELL", "OPENING")
PRICE_SOURCES = ("BROKER_FILL", "BROKER_AVG", "ORDER_ESTIMATE")
LEDGER_SCHEMA_VERSION = 1


class FillEventError(ValueError):
    """사건 값이 유효하지 않음."""


@dataclass(frozen=True)
class FillEvent:
    """체결 사건 하나.

    kind:
      BUY / SELL — 체결로 보유 수량이 늘거나 줄어든 사건
      OPENING    — 이 프로그램 밖에서 생긴 기존 보유분을 사람이 확인하고 인수
                   (원가 = 잔고 평균단가, price_source=BROKER_AVG)
    event_id: 같은 사건을 두 번 기록하지 않기 위한 키
      (예: "{계좌범위}|{주문거래일}|{BUY/SELL}|{종목}|{주문번호}|{누적체결수량}" — 8-C)
    trade_date: 체결 거래일 (보유 거래일수 계산 기준)
    """

    event_id: str
    kind: str
    symbol: str
    quantity: int
    price: int
    price_source: str
    trade_date: date
    occurred_at: datetime
    order_id: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not str(self.event_id).strip():
            raise FillEventError("event_id가 비어 있음")
        if self.kind not in FILL_KINDS:
            raise FillEventError(f"kind={self.kind!r} (허용: {FILL_KINDS})")
        if not str(self.symbol).strip():
            raise FillEventError("symbol이 비어 있음")
        for name in ("quantity", "price"):
            v = getattr(self, name)
            if type(v) is not int or v <= 0:
                raise FillEventError(f"{name}는 양의 정수여야 함 — {v!r}")
        if self.price_source not in PRICE_SOURCES:
            raise FillEventError(f"price_source={self.price_source!r} (허용: {PRICE_SOURCES})")
        if self.kind == "OPENING" and self.price_source != "BROKER_AVG":
            raise FillEventError("OPENING은 잔고 평균단가(BROKER_AVG)로만 기록")
        if not isinstance(self.trade_date, date) or isinstance(self.trade_date, datetime):
            raise FillEventError("trade_date는 date여야 함")
        if not isinstance(self.occurred_at, datetime):
            raise FillEventError("occurred_at은 datetime이어야 함")

    @property
    def is_estimate(self) -> bool:
        return self.price_source == "ORDER_ESTIMATE"

    def to_dict(self) -> dict:
        return {
            "v": LEDGER_SCHEMA_VERSION, "event_id": self.event_id, "kind": self.kind,
            "symbol": self.symbol, "quantity": self.quantity, "price": self.price,
            "price_source": self.price_source, "trade_date": self.trade_date.isoformat(),
            "occurred_at": self.occurred_at.isoformat(), "order_id": self.order_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FillEvent":
        if d.get("v") != LEDGER_SCHEMA_VERSION:
            raise FillEventError(f"스키마 버전 불일치: {d.get('v')!r}")
        return cls(
            event_id=str(d["event_id"]), kind=str(d["kind"]), symbol=str(d["symbol"]),
            quantity=d["quantity"], price=d["price"], price_source=str(d["price_source"]),
            trade_date=date.fromisoformat(d["trade_date"]),
            occurred_at=datetime.fromisoformat(d["occurred_at"]),
            order_id=str(d.get("order_id", "")), note=str(d.get("note", "")),
        )
