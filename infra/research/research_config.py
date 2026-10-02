from __future__ import annotations

"""연구 운영 설정 `config/research.yaml` (A5-1). 모르는 키·잘못된 값은 오류(fail-closed) — 조용히 기본값을 쓰지 않음."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_RESEARCH_CONFIG = Path(__file__).resolve().parents[2] / "config" / "research.yaml"
CURRENT = "current"
_HASH = re.compile(r"^[0-9a-f]{12}$")


class ResearchSettingsError(ValueError):
    """config/research.yaml 형식 오류."""


@dataclass(frozen=True)
class OpenCheckSettings:
    offset_min: int = 5
    on_time_tolerance_sec: int = 120


@dataclass(frozen=True)
class ResearchSettings:
    active_contract: str = CURRENT          # "current" 또는 12자리 계약 해시
    open_check: OpenCheckSettings = OpenCheckSettings()


def _only(d, keys: set, where: str) -> dict:
    if not isinstance(d, dict):
        raise ResearchSettingsError(f"{where}: 사전(dict)이어야 함")
    extra = set(d) - keys
    if extra:
        raise ResearchSettingsError(f"{where}: 모르는 키 {sorted(extra)}")
    return d


def _int(v, lo: int, hi: int, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ResearchSettingsError(f"{where}: {lo}~{hi} 정수여야 함 (현재 {v!r})")
    return v


def load_research_settings(path: str | Path = DEFAULT_RESEARCH_CONFIG) -> ResearchSettings:
    p = Path(path)
    if not p.exists():
        raise ResearchSettingsError(f"연구 설정 파일 없음: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    _only(raw, {"s1", "a5"}, "research.yaml")
    s1 = _only(raw.get("s1", {}), {"active_contract"}, "s1")
    contract = str(s1.get("active_contract", CURRENT)).strip().lower()
    if contract != CURRENT and not _HASH.match(contract):
        raise ResearchSettingsError(f"s1.active_contract: 'current' 또는 12자리 해시여야 함 (현재 {contract!r})")
    a5 = _only(raw.get("a5", {}), {"open_check"}, "a5")
    oc = _only(a5.get("open_check", {}), {"offset_min", "on_time_tolerance_sec"}, "a5.open_check")
    return ResearchSettings(contract, OpenCheckSettings(
        _int(oc.get("offset_min", 5), 0, 60, "a5.open_check.offset_min"),
        _int(oc.get("on_time_tolerance_sec", 120), 0, 3600, "a5.open_check.on_time_tolerance_sec")))
