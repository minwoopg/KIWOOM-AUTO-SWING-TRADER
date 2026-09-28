# -*- coding: utf-8 -*-
"""추출 경계 회귀 테스트 (스윙 분리 1라운드, 2026-09-28).

1. 단타 매매 로직 모듈이 이 레포로 새어 들어오지 않았는지
2. provenance.json에 "unchanged"로 기록된 파일이 정말 원본과 바이트 동일한지
   (단타 레포 수정 사항을 나중에 비교·반영할 때의 기준점)
3. 설정에 단타 전용 섹션이 남아있지 않은지
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


# ── 1. 단타 매매 로직 모듈 import 금지 ─────────────────────
FORBIDDEN_MODULES = (
    "domain.service.trading_service",
    "domain.strategy.strategy_router",
    "domain.strategy.breakout_strategy",
    "domain.strategy.neutral_strategy",
    "domain.strategy.bottom_strategy",
    "domain.strategy.hold_strategy",
    "domain.strategy.candidate_a_guard",
    "domain.strategy.entry_quality_shadow",
    "domain.market_regime",
    "domain.risk.risk_manager",
    "infra.storage.state_reconciler",
    "infra.storage.daily_reporter",
    "infra.storage.minute_bar_saver",
    "infra.websocket",
    "app.target_selection",
    "test_run_once_integration",
)
offenders = []
for py in Path(".").rglob("*.py"):
    rel = py.as_posix()
    if rel.startswith((".venv/", "venv/")) or rel == "test_extraction_boundary.py":
        continue
    # tools/equivalence/는 단타 레포의 TradingService와 동작을 비교하는 도구라
    # orig 모드에서 단타 모듈을 (단타 레포 안에서) import함 — 스윙 코드가 아님
    if rel.startswith("tools/equivalence/"):
        continue
    tree = ast.parse(py.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        for n in names:
            if any(n == f or n.startswith(f + ".") for f in FORBIDDEN_MODULES):
                offenders.append(f"{rel}: {n}")
check("1-1) 단타 매매 로직 모듈을 import하는 파일이 없음", not offenders)
for o in offenders[:10]:
    print(f"       └ {o}")

# ── 2. provenance.json — unchanged 파일 해시 일치 ─────────────
prov = json.loads(Path("provenance.json").read_text(encoding="utf-8"))
check("2-1) provenance.json에 원본 커밋이 기록됨", len(prov.get("source_commit", "")) == 40)
mismatch, missing = [], []
for rel, meta in prov["files"].items():
    if meta.get("status") != "unchanged":
        continue
    p = Path(rel)
    if not p.exists():
        missing.append(rel)
        continue
    if hashlib.sha256(p.read_bytes()).hexdigest() != meta["source_sha256"]:
        mismatch.append(rel)
check("2-2) unchanged로 기록된 파일이 모두 존재", not missing)
check("2-3) unchanged로 기록된 파일이 원본과 바이트 동일(수정했다면 status를 modified로)", not mismatch)
for m in mismatch[:10]:
    print(f"       └ {m}")

# ── 3. 설정에 단타 전용 섹션 없음 ───────────────────────────
import yaml  # noqa: E402

from config.settings import Settings, load_settings  # noqa: E402

raw = yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8"))
day_sections = {"trading", "strategy", "market_regime", "risk", "entry_watch",
                "websocket", "experimental", "targets"}
check("3-1) settings.yaml에 단타 전용 섹션이 없음", not (day_sections & set(raw)))
check("3-2) Settings 필드는 app/broker/storage/kakao/market_data뿐",
      set(Settings.__dataclass_fields__) == {"app", "broker", "storage", "kakao", "market_data"})
s = load_settings()
check("3-3) 실제 settings.yaml이 로드됨", s.app.name == "swing-auto-trader")
check("3-4) 계좌 라벨이 설정됨(단타 계좌 라벨 'acct-a'와 다름)",
      bool(s.broker.account_scope_id.strip()) and s.broker.account_scope_id != "acct-a")

print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
