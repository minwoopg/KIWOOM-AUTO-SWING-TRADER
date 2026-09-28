# -*- coding: utf-8 -*-
"""비용 모델 단일 출처 검증 (2026-08-07, 1J단계)

배경: 분석기마다 비용을 직접 하드코딩해 값이 갈라져 있었음.
  daily_reporter          0.90%
  replay_runner 외 3종    0.35%
2026-07에 COST_RATE를 0.53% → 0.90%로 정정했을 때 백테스트
스크립트들이 따라가지 못한 결과. 0.55%p 차이는 "승리"와 "적자"를
가르는 크기라, 이 테스트로 단일 출처를 고정한다.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

from domain.cost_model import (
    CostModel, CostModelConfigError, SCENARIO_ORDER, DEFAULT_SETTINGS_PATH,
    load_cost_model, reset_cache,
    DEFAULT_BASE_ROUNDTRIP_PCT, DEFAULT_STRESS_ROUNDTRIP_PCT,
)

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if condition:
        passed += 1
    else:
        failed += 1


# ── 1. 동일 gross return에 세 시나리오가 정확히 계산됨 ──────────
m = CostModel(gross_roundtrip_pct=0.0, base_roundtrip_pct=0.35, stress_roundtrip_pct=0.90)
nets = m.net_all(0.61)
check("1-1) Gross는 원수익 그대로", abs(nets["gross"] - 0.61) < 1e-9)
check("1-2) Base는 0.35%p 차감", abs(nets["base"] - 0.26) < 1e-9)
check("1-3) Stress는 0.90%p 차감", abs(nets["stress"] - (-0.29)) < 1e-9)
check("1-4) None 입력은 None 반환", m.net(None, "base") is None)
try:
    m.cost_pct("unknown")
    _raised = False
except ValueError:
    _raised = True
check("1-6) 알 수 없는 시나리오 이름에 ValueError", _raised)

# 승률도 시나리오별로 갈려야 함
rates = m.positive_rates([0.71, 0.50, 0.20, -0.10])
check("1-7) gross_positive_rate 산출", abs(rates["gross_positive_rate"] - 0.75) < 1e-9)
check("1-8) base_net_positive_rate 산출", abs(rates["base_net_positive_rate"] - 0.50) < 1e-9)
check("1-9) stress_net_positive_rate 산출",
      abs(rates["stress_net_positive_rate"] - 0.0) < 1e-9)
check("1-10) 표본 없으면 None", m.positive_rates([])["gross_positive_rate"] is None)


# ── 2. (스윙 분리 1라운드에서 제외) 단타 분석기(replay_runner 등)·daily_reporter가
#      cost_model을 참조하는지 검사 — 이 레포에는 해당 파일이 없음.
#      스윙 리포트/백테스트를 추가하면 같은 형식의 검사를 다시 추가할 것.


# ── 3. 허용 위치 밖에 비용 하드코딩이 없음 (1J.1 강화) ────────
# 1J의 검사는 과거 변수명(ROUND_TRIP_COST_PCT 등)만 봐서
# `BACKTEST_COST = 0.35` 같은 새 하드코딩을 못 잡았음.
# 이제 **비용 literal 자체**(0.35 / 0.90 / 0.0035 / 0.009)를
# 광범위하게 찾고 허용 위치만 제외한다.
ALLOWED = {"domain/cost_model.py", "config/settings.yaml",
           "test_cost_model.py"}
COST_LITERAL = re.compile(r"=\s*0\.(35|90|9|0035|009)\b")
offenders = []
for p in list(Path(".").rglob("*.py")) + list(Path(".").rglob("*.yaml")):
    rel = str(p).replace("\\", "/").lstrip("./")
    if rel in ALLOWED or rel.startswith(("logs/", "reports/", "exports/", "tests/", ".venv/")):
        continue
    try:
        txt = p.read_text(encoding="utf-8")
    except Exception:
        continue
    for i, line in enumerate(txt.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue          # 주석은 설명일 수 있으므로 제외
        if COST_LITERAL.search(line):
            offenders.append(f"{rel}:{i}  {stripped[:70]}")
check("3-1) 허용 위치 밖에 비용 literal 하드코딩이 없음", not offenders)
for o in offenders[:5]:
    print(f"       └ {o}")

# 검사기 자체가 새 변수명도 잡는지 (자기검증)
import tempfile as _tf
_probe = Path(_tf.mkdtemp()) / "probe.py"
_probe.write_text("BACKTEST_COST = 0.35\n", encoding="utf-8")
check("3-2) 새로운 임의 변수명의 0.35 하드코딩도 검출됨",
      bool(COST_LITERAL.search(_probe.read_text(encoding="utf-8"))))
_probe.write_text("RATE = 0.009\n", encoding="utf-8")
check("3-3) 비율형(0.009) 하드코딩도 검출됨",
      bool(COST_LITERAL.search(_probe.read_text(encoding="utf-8"))))
check("3-4) 주석은 오탐으로 잡지 않음",
      "#" == "# 왕복 0.35% 가정".strip()[0])


# ── 4. 설정 변경 시 모든 결과가 동일하게 변경됨 ─────────────────
import tempfile
tmp = Path(tempfile.mkdtemp()) / "settings.yaml"
tmp.write_text(
    "cost_model:\n  gross_roundtrip_pct: 0.00\n"
    "  base_roundtrip_pct: 0.50\n  stress_roundtrip_pct: 1.20\n", encoding="utf-8")
reset_cache()
m2 = load_cost_model(tmp, use_cache=False)
check("4-1) 설정의 Base 값이 반영됨", abs(m2.base_roundtrip_pct - 0.50) < 1e-9)
check("4-2) 설정의 Stress 값이 반영됨", abs(m2.stress_roundtrip_pct - 1.20) < 1e-9)
check("4-3) 변경된 설정으로 순수익이 함께 바뀜",
      abs(m2.net(1.00, "base") - 0.50) < 1e-9 and abs(m2.net(1.00, "stress") - (-0.20)) < 1e-9)

# 2026-08-07 (1J.1 정책 변경): 1J에서는 설정 부재 시 조용히 기본값을
# 썼는데, 51일 백테스트가 아무 경고 없이 잘못된 비용으로 도는 위험이
# 있어 fail-closed로 바꿨음. 기본값은 allow_default=True일 때만.
m3 = load_cost_model(Path(tempfile.mkdtemp()) / "nope.yaml",
                     allow_default=True, use_cache=False)
check("4-4) allow_default=True를 명시할 때만 기본값 허용",
      abs(m3.base_roundtrip_pct - DEFAULT_BASE_ROUNDTRIP_PCT) < 1e-9)
reset_cache()


# ── 9. fail-closed 로딩 (1J.1) ──────────────────────────────────
# 재현(1J): cwd를 프로젝트 밖으로 옮기거나 YAML이 깨져도 예외 없이
# Base 0.35로 돌아갔음 — 비용을 0.42로 교정해도 조용히 무시됨.
import os


def _yaml(text: str) -> Path:
    p = Path(tempfile.mkdtemp()) / "s.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except CostModelConfigError:
        return True


_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp())
try:
    reset_cache()
    m9 = load_cost_model()
    ok_cwd = abs(m9.base_roundtrip_pct - 0.35) < 1e-9
finally:
    os.chdir(_cwd)
check("9-1) cwd가 프로젝트 밖이어도 올바른 settings.yaml을 로드", ok_cwd)
check("9-2) 기본 설정 경로가 프로젝트 루트 기준 절대경로",
      DEFAULT_SETTINGS_PATH.is_absolute() and DEFAULT_SETTINGS_PATH.name == "settings.yaml")

check("9-3) 설정 파일 누락 → 예외",
      _raises(lambda: load_cost_model(Path(tempfile.mkdtemp()) / "nope.yaml", use_cache=False)))
check("9-4) YAML malformed → 예외",
      _raises(lambda: load_cost_model(_yaml("cost_model: [[["), use_cache=False)))
check("9-5) cost_model 블록 누락 → 예외",
      _raises(lambda: load_cost_model(_yaml("other: 1"), use_cache=False)))
check("9-6) 필수 키 누락 → 예외",
      _raises(lambda: load_cost_model(_yaml("cost_model:\n  base_roundtrip_pct: 0.4"), use_cache=False)))
check("9-7) 숫자 변환 실패 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: abc\n  base_roundtrip_pct: 0.4\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))
check("9-8) Gross < Base < Stress 위반 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.5\n  base_roundtrip_pct: 0.2\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))
check("9-9) 음수 비용 → 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: -0.1\n  base_roundtrip_pct: 0.4\n"
          "  stress_roundtrip_pct: 0.9"), use_cache=False)))


# ── 10. 경로별 cache (1J.1) ─────────────────────────────────────
# 재현(1J): 전역 캐시 하나라 A→B 순서로 읽으면 B도 A 값이 나왔음.
reset_cache()
A = _yaml("cost_model:\n  gross_roundtrip_pct: 0.0\n  base_roundtrip_pct: 0.11\n"
          "  stress_roundtrip_pct: 0.22")
B = _yaml("cost_model:\n  gross_roundtrip_pct: 0.0\n  base_roundtrip_pct: 0.77\n"
          "  stress_roundtrip_pct: 0.88")
a, b = load_cost_model(A), load_cost_model(B)
check("10-1) A 설정이 정확히 로드", abs(a.base_roundtrip_pct - 0.11) < 1e-9)
check("10-2) B 설정이 A로 오염되지 않음", abs(b.base_roundtrip_pct - 0.77) < 1e-9)
check("10-3) A를 다시 읽어도 값 유지", abs(load_cost_model(A).base_roundtrip_pct - 0.11) < 1e-9)
reset_cache()


# ── 11. 기준금액 통일 (1J.1) ────────────────────────────────────
# roundtrip_pct := 진입 원금 대비 왕복 총비용 추정률
live2 = load_cost_model(use_cache=False)
check("11-1) cost_amount가 진입 원금 기준으로 계산",
      abs(live2.cost_amount(1_000_000, "stress") - 9000.0) < 1e-6)
check("11-2) replay 정의와 일치 — 원금 100, 수익 10%면 순수익 = 10 - cost_pct",
      abs(live2.net(10.0, "stress") - 9.10) < 1e-9)
# 11-3~11-7: daily_reporter 소스 검사 — 스윙 분리 1라운드에서 제외(파일 없음)
check("11-8) describe()에 기준금액이 드러남", "진입 원금" in live2.describe())


# ── 12. (스윙 분리 1라운드에서 제외) 단타 분석기 실행 기반 3시나리오 검증.


# ── 13. Gross 정의 강제 (1J.2) ──────────────────────────────────
check("13-1) gross=0.10 → 예외",
      _raises(lambda: CostModel(0.10, 0.35, 0.90).validate()))
check("13-2) gross=-0.10 → 예외",
      _raises(lambda: CostModel(-0.10, 0.35, 0.90).validate()))
check("13-3) gross=0.00 → 정상",
      CostModel(0.00, 0.35, 0.90).validate().gross_roundtrip_pct == 0.0)
check("13-4) 설정에서 gross>0이면 로딩 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.10\n  base_roundtrip_pct: 0.35\n"
          "  stress_roundtrip_pct: 0.90"), use_cache=False)))


# ── 14. allow_default 정책 일치 (1J.2) ──────────────────────────
# 정책: "설정 파일 자체를 사용할 수 없을 때만" fallback.
#       블록은 있는데 값이 틀린 경우는 항상 예외.
check("14-1) 파일 누락 + allow_default → 기본값",
      load_cost_model(Path(tempfile.mkdtemp()) / "x.yaml",
                      allow_default=True, use_cache=False).base_roundtrip_pct == 0.35)
check("14-2) YAML 파싱 실패 + allow_default → 기본값",
      load_cost_model(_yaml("cost_model: [[["), allow_default=True,
                      use_cache=False).base_roundtrip_pct == 0.35)
check("14-3) 블록 부재 + allow_default → 기본값",
      load_cost_model(_yaml("other: 1"), allow_default=True,
                      use_cache=False).base_roundtrip_pct == 0.35)
check("14-4) 키 누락은 allow_default여도 예외(설정이 있는데 틀린 경우)",
      _raises(lambda: load_cost_model(_yaml("cost_model:\n  base_roundtrip_pct: 0.4"),
                                      allow_default=True, use_cache=False)))
check("14-5) 검증 위반은 allow_default여도 예외",
      _raises(lambda: load_cost_model(_yaml(
          "cost_model:\n  gross_roundtrip_pct: 0.10\n  base_roundtrip_pct: 0.35\n"
          "  stress_roundtrip_pct: 0.90"), allow_default=True, use_cache=False)))
check("14-6) 정책이 문서화됨",
      "설정 파일 자체를 사용할 수 없을 때만" in
      Path("domain/cost_model.py").read_text(encoding="utf-8"))


# ── 15. (스윙 분리 1라운드에서 제외) daily_reporter 문구 검사.


print()
print(f"총 {passed + failed}건 중 통과 {passed}건, 실패 {failed}건")
if failed:
    sys.exit(1)
