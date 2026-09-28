from __future__ import annotations

"""스윙 실행 상태 모델 (스윙 분리 4라운드, 2026-09-28).

단타 레포의 `RuntimeState`는 당일 기준 필드(당일 진입횟수, 30분 쿨다운, 당일
손실 카운트 등)로 이뤄져 있어 여러 날 보유를 담을 수 없습니다. 이 모델로
교체합니다.

**역할 분리 (한 사실은 한 곳에만)**
| 사실 | 원천 |
|---|---|
| 보유 수량·매입 단가·진입 거래일 | 체결 원장 (`infra/storage/fill_ledger.py`) |
| 주문 진행 상태 | 포지션 상태머신 (PSM, 메모리) + 주문 저널 |
| 재시작 시 "보냈는지 모르는 주문" | `unresolved_order_intents` (이 파일) |
| 전략이 붙인 메타데이터(전략 ID, 손절가 등) | `positions` (이 파일) |

수량이나 가격을 여기에 중복 저장하지 않습니다 — 두 곳에 있으면 언젠가 어긋납니다.

`OrderExecutor`는 `unresolved_order_intents`와 `last_order_id_by_symbol`만
씁니다(단타 `RuntimeState`와 같은 이름·형식 유지).

손절가 등 메타데이터 **값을 정하는 규칙**은 이 파일에 없습니다(매매 로직 제외).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SWING_STATE_SCHEMA_VERSION = 1

POSITION_ORIGINS = ("ORDER", "ADOPTED")


class SwingStateFormatError(ValueError):
    """상태 파일 내용이 이 스키마와 맞지 않음."""


@dataclass
class PositionMeta:
    """보유 종목에 전략이 붙여두는 메타데이터. 수량·단가는 원장이 원천.

    origin:
      ORDER   — 이 프로그램이 낸 주문으로 생긴 포지션
      ADOPTED — 이미 계좌에 있던 보유분을 사람이 확인하고 인수한 것
    needs_review: 사람이 확인하기 전까지 전략이 이 종목을 자동으로 다루지
      않게 하기 위한 표시(판단은 호출부 책임).
    meta: 전략별 추가 값(JSON으로 저장 가능한 값만).
    """

    symbol: str
    strategy_id: str = ""
    stop_price: int | None = None
    origin: str = "ORDER"
    needs_review: bool = False
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.symbol or not str(self.symbol).strip():
            raise SwingStateFormatError("symbol이 비어 있음")
        if self.origin not in POSITION_ORIGINS:
            raise SwingStateFormatError(f"{self.symbol}: origin={self.origin!r} (허용: {POSITION_ORIGINS})")
        if self.stop_price is not None and (type(self.stop_price) is not int or self.stop_price <= 0):
            raise SwingStateFormatError(f"{self.symbol}: stop_price는 양의 정수 또는 None — {self.stop_price!r}")
        if not isinstance(self.meta, dict):
            raise SwingStateFormatError(f"{self.symbol}: meta는 dict여야 함")
        now = datetime.now().isoformat(timespec="seconds")
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = self.created_at

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "strategy_id": self.strategy_id,
            "stop_price": self.stop_price, "origin": self.origin,
            "needs_review": self.needs_review, "created_at": self.created_at,
            "updated_at": self.updated_at, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PositionMeta":
        if not isinstance(d, dict):
            raise SwingStateFormatError(f"포지션 메타가 dict가 아님: {type(d).__name__}")
        unknown = set(d) - {"symbol", "strategy_id", "stop_price", "origin", "needs_review",
                            "created_at", "updated_at", "meta"}
        if unknown:
            raise SwingStateFormatError(f"알 수 없는 포지션 메타 필드: {sorted(unknown)}")
        return cls(
            symbol=str(d["symbol"]), strategy_id=str(d.get("strategy_id", "")),
            stop_price=d.get("stop_price"), origin=str(d.get("origin", "ORDER")),
            needs_review=bool(d.get("needs_review", False)),
            created_at=str(d.get("created_at", "")), updated_at=str(d.get("updated_at", "")),
            meta=d.get("meta", {}) if d.get("meta") is not None else {},
        )


@dataclass
class SwingState:
    """state.json에 저장되는 스윙 실행 상태."""

    # 주문 전송 직전에 기록, 확정되면 삭제 (OrderExecutor가 관리 — 단타와 같은 형식)
    unresolved_order_intents: dict[str, dict] = field(default_factory=dict)
    last_order_id_by_symbol: dict[str, str] = field(default_factory=dict)
    positions: dict[str, PositionMeta] = field(default_factory=dict)
    # 마지막으로 프로세스가 기동한 거래일 (YYYY-MM-DD) — 일자 경계 감지용
    last_session_date: str | None = None

    # ── 메타데이터 편의 메서드 (값을 정하는 규칙은 호출부 책임) ──
    def upsert_position_meta(self, meta: PositionMeta) -> None:
        existing = self.positions.get(meta.symbol)
        if existing is not None:
            meta.created_at = existing.created_at
        meta.updated_at = datetime.now().isoformat(timespec="seconds")
        self.positions[meta.symbol] = meta

    def remove_position_meta(self, symbol: str) -> PositionMeta | None:
        return self.positions.pop(symbol, None)

    # ── 직렬화 ────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "schema_version": SWING_STATE_SCHEMA_VERSION,
            "unresolved_order_intents": self.unresolved_order_intents,
            "last_order_id_by_symbol": self.last_order_id_by_symbol,
            "positions": {s: m.to_dict() for s, m in sorted(self.positions.items())},
            "last_session_date": self.last_session_date,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SwingState":
        if not isinstance(raw, dict):
            raise SwingStateFormatError(f"최상위가 dict가 아님: {type(raw).__name__}")
        version = raw.get("schema_version")
        if version is None:
            raise SwingStateFormatError(
                "schema_version 없음 — 단타 RuntimeState 형식이거나 다른 프로그램의 파일로 보임"
            )
        if version != SWING_STATE_SCHEMA_VERSION:
            raise SwingStateFormatError(
                f"schema_version 불일치 (파일={version!r}, 코드={SWING_STATE_SCHEMA_VERSION})"
            )
        intents = raw.get("unresolved_order_intents", {})
        last_ids = raw.get("last_order_id_by_symbol", {})
        positions_raw = raw.get("positions", {})
        for name, value in (("unresolved_order_intents", intents),
                            ("last_order_id_by_symbol", last_ids),
                            ("positions", positions_raw)):
            if not isinstance(value, dict):
                raise SwingStateFormatError(f"{name}가 dict가 아님")
        if not all(isinstance(v, dict) for v in intents.values()):
            raise SwingStateFormatError("unresolved_order_intents 값이 dict가 아님")
        positions = {}
        for sym, d in positions_raw.items():
            m = PositionMeta.from_dict(d)
            if m.symbol != sym:
                raise SwingStateFormatError(f"positions 키({sym})와 symbol({m.symbol})이 다름")
            positions[sym] = m
        lsd = raw.get("last_session_date")
        if lsd is not None and not isinstance(lsd, str):
            raise SwingStateFormatError("last_session_date는 문자열 또는 null")
        return cls(
            unresolved_order_intents=dict(intents),
            last_order_id_by_symbol={str(k): str(v) for k, v in last_ids.items()},
            positions=positions,
            last_session_date=lsd,
        )
