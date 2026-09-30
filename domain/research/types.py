from __future__ import annotations

"""연구 계층 공통 타입: 3값 판정(Tri), 값+사유(FV), 조건 판정(Check)."""

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class Tri(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


def combine(tris: Iterable["Tri"]) -> "Tri":
    """하나라도 FAIL → FAIL, 아니면 하나라도 UNKNOWN → UNKNOWN, 모두 PASS → PASS.
    빈 입력은 UNKNOWN (조건 없이 통과시키지 않음)."""
    items = list(tris)
    if not items:
        return Tri.UNKNOWN
    if any(t == Tri.FAIL for t in items):
        return Tri.FAIL
    if any(t == Tri.UNKNOWN for t in items):
        return Tri.UNKNOWN
    return Tri.PASS


@dataclass(frozen=True)
class FV:
    """계산값. value가 None이면 UNKNOWN이고 reason에 이유가 있습니다.
    NaN·무한대는 만들 때 UNKNOWN으로 바꿉니다(통과로 새지 않게)."""

    value: float | None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.value is not None and (not isinstance(self.value, (int, float)) or
                                       isinstance(self.value, bool) or not math.isfinite(self.value)):
            object.__setattr__(self, "reason", self.reason or f"NON_FINITE:{self.value!r}")
            object.__setattr__(self, "value", None)
        if self.value is None and not self.reason:
            object.__setattr__(self, "reason", "UNKNOWN")

    @property
    def ok(self) -> bool:
        return self.value is not None

    @staticmethod
    def unknown(reason: str) -> "FV":
        return FV(None, reason)


def ratio(num: FV | float | None, den: FV | float | None, *, what: str = "") -> FV:
    """num/den. 입력 UNKNOWN 또는 분모 0이면 UNKNOWN."""
    n = num.value if isinstance(num, FV) else num
    d = den.value if isinstance(den, FV) else den
    if n is None:
        return FV.unknown(num.reason if isinstance(num, FV) else f"{what}:NUM_UNKNOWN")
    if d is None:
        return FV.unknown(den.reason if isinstance(den, FV) else f"{what}:DEN_UNKNOWN")
    if d == 0:
        return FV.unknown(f"{what}:ZERO_DENOMINATOR" if what else "ZERO_DENOMINATOR")
    return FV(n / d)


@dataclass(frozen=True)
class Check:
    """조건 하나의 판정 기록 (값·기준·사유)."""

    name: str
    result: Tri
    value: float | str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "result": self.result.value, "value": self.value, "detail": self.detail}


def compare(name: str, value: FV, op: str, threshold: float, detail: str = "") -> Check:
    """value op threshold 판정. value UNKNOWN → UNKNOWN."""
    if not value.ok:
        return Check(name, Tri.UNKNOWN, None, value.reason)
    v = value.value
    ok = {">": v > threshold, ">=": v >= threshold, "<": v < threshold, "<=": v <= threshold}[op]
    return Check(name, Tri.PASS if ok else Tri.FAIL, v, detail or f"{op} {threshold}")
