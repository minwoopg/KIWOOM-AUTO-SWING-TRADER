from __future__ import annotations

"""종목 목록 분류 — 수집 대상과 현재 신호 자격을 분리 (A2, universe_policy = u1).

원천: ka10099 종목정보 리스트(mrkt_tp 0=KOSPI, 10=KOSDAQ). A1 실측(2026-09-30 12:31) 필드:
code, name, marketCode, marketName, kind, auditInfo, state, orderWarning, companyClassName,
regDay, listCount, lastPrice, upName, upSizeName, nxtEnable.

두 판정을 섞지 않습니다 (A2 보완 2)
- **collect(수집 대상)**: 증권 유형만으로 정함. 현재 위험 표시가 있는 종목도 과거 일봉은 수집합니다
  — 현재 상태로 과거 표본을 고르면 선택 편향이 생기기 때문입니다. 과거 위험 상태는 UNKNOWN.
- **eligible_now(현재 신호 자격)**: 스냅숏을 관측한 시점의 상태로만 판정. 과거 날짜에 적용하지 않습니다.

증권 유형 (정책 u1)
- marketCode 0/10이 아니면 ETF(8)·ETN(60/70/90)·REIT(6)·INFRA(2)·MUTUAL(4)·OTHER.
- 주식 중 종목코드 마지막 글자가 '0'이 아니면 PREFERRED(우선주 **추정** — 이름이 아니라 코드 규칙.
  실측 114개 모두 이름이 '우'로 끝났지만 '에코글로우'처럼 보통주도 '우'로 끝나므로 이름은 쓰지 않음).
- companyClassName '스팩' → SPAC, '외국기업' → FOREIGN(사용자 결정 2026-09-30: 초기 S1 대상 제외).
- 나머지 COMMON. 수집·S1 모두 COMMON만 (policy.collect_types / s1_types).

현재 위험 표시 (세 필드가 서로 어긋나므로 **합집합**, 원래 값은 그대로 보존)
- auditInfo가 '정상'이 아니면 AUDIT:<원래 값> — 투자주의·투자주의환기종목 포함(사용자 결정: 초기 제외).
- state를 '|'로 나눈 토큰에 관리종목·거래정지가 있으면 STATE:<토큰>.
- orderWarning이 '0'이 아니면 ORDER_WARNING:<원래 숫자>. **숫자를 관리·정지 등으로 번역하지 않습니다**
  (A2 보완 3 — 공식 설명과 실측 상관을 코드 의미로 단정하지 않음).
- 필드가 없거나 비어 있으면 *_MISSING — 모르면 위험으로 봅니다(fail-closed).
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

UNIVERSE_POLICY_ID = "u1"

COMMON, PREFERRED, SPAC, FOREIGN = "COMMON", "PREFERRED", "SPAC", "FOREIGN"
ETF, ETN, REIT, INFRA, MUTUAL, OTHER = "ETF", "ETN", "REIT", "INFRA", "MUTUAL", "OTHER"

STOCK_MARKETS = {  # marketCode → (시장, 소속 시장 지수 source_id)
    "0": ("KOSPI", "INDEX:KOSPI:001"),
    "10": ("KOSDAQ", "INDEX:KOSDAQ:101"),
}
NON_STOCK_TYPES = {"8": ETF, "60": ETN, "70": ETN, "90": ETN, "6": REIT, "2": INFRA, "4": MUTUAL}


@dataclass(frozen=True)
class UniversePolicy:
    collect_types: tuple[str, ...] = (COMMON,)
    s1_types: tuple[str, ...] = (COMMON,)
    spac_class_names: tuple[str, ...] = ("스팩",)
    foreign_class_names: tuple[str, ...] = ("외국기업",)
    audit_ok_values: tuple[str, ...] = ("정상",)
    state_risk_tokens: tuple[str, ...] = ("관리종목", "거래정지")
    order_warning_ok_values: tuple[str, ...] = ("0",)

    @property
    def policy_version(self) -> str:
        return f"{UNIVERSE_POLICY_ID}:{self.policy_hash()}"

    def policy_hash(self) -> str:
        raw = json.dumps({"policy": UNIVERSE_POLICY_ID, **asdict(self)}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _s(row: dict, key: str) -> str | None:
    v = row.get(key)
    return None if v is None else str(v).strip()


def _int(v: str | None) -> int | None:
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _yyyymmdd(v: str | None) -> date | None:
    v = (v or "").strip()
    if len(v) != 8 or not v.isdigit():
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:]))
    except ValueError:
        return None


@dataclass(frozen=True)
class ListingRecord:
    code: str
    name: str
    market_code: str
    market: str | None               # KOSPI / KOSDAQ / None(주식 시장 아님)
    index_id: str | None             # 소속 시장 지수 source_id
    security_type: str
    type_basis: str                  # 유형 판정 근거 (MARKET_CODE:8 / CODE_SUFFIX:5 / COMPANY_CLASS:스팩 / DEFAULT)
    audit_info: str | None
    state: str | None
    order_warning: str | None        # 원래 값 그대로 (번역하지 않음)
    company_class: str | None
    reg_day: date | None
    list_count: int | None
    last_price: int | None
    risk_flags: tuple[str, ...]
    collect: bool
    eligible_now: bool
    exclusions: tuple[str, ...]      # eligible_now가 아닌 이유 (TYPE:… / RISK:…)
    raw: dict = field(compare=False, repr=False)


def security_type_of(row: dict, policy: UniversePolicy) -> tuple[str, str]:
    mc = _s(row, "marketCode") or ""
    if mc not in STOCK_MARKETS:
        t = NON_STOCK_TYPES.get(mc, OTHER)
        return t, f"MARKET_CODE:{mc or 'MISSING'}"
    code = _s(row, "code") or ""
    cls = _s(row, "companyClassName") or ""
    if not code:
        return OTHER, "CODE_MISSING"
    if not code.endswith("0"):
        return PREFERRED, f"CODE_SUFFIX:{code[-1]}"
    if cls in policy.spac_class_names:
        return SPAC, f"COMPANY_CLASS:{cls}"
    if cls in policy.foreign_class_names:
        return FOREIGN, f"COMPANY_CLASS:{cls}"
    return COMMON, "DEFAULT"


def risk_flags_of(row: dict, policy: UniversePolicy) -> tuple[str, ...]:
    flags: list[str] = []
    audit = _s(row, "auditInfo")
    if not audit:
        flags.append("AUDIT_MISSING")
    elif audit not in policy.audit_ok_values:
        flags.append(f"AUDIT:{audit}")
    state = _s(row, "state")
    if state is None:
        flags.append("STATE_MISSING")
    else:
        for tok in (t.strip() for t in state.split("|")):
            if tok in policy.state_risk_tokens:
                flags.append(f"STATE:{tok}")
    ow = _s(row, "orderWarning")
    if not ow:
        flags.append("ORDER_WARNING_MISSING")
    elif ow not in policy.order_warning_ok_values:
        flags.append(f"ORDER_WARNING:{ow}")
    return tuple(flags)


def classify_row(row: dict, policy: UniversePolicy | None = None) -> ListingRecord:
    policy = policy or UniversePolicy()
    stype, basis = security_type_of(row, policy)
    mc = _s(row, "marketCode") or ""
    market, index_id = STOCK_MARKETS.get(mc, (None, None))
    flags = risk_flags_of(row, policy)
    collect = stype in policy.collect_types
    excl = []
    if stype not in policy.s1_types:
        excl.append(f"TYPE:{stype}")
    excl += [f"RISK:{f}" for f in flags]
    return ListingRecord(
        code=_s(row, "code") or "", name=_s(row, "name") or "", market_code=mc, market=market, index_id=index_id,
        security_type=stype, type_basis=basis, audit_info=_s(row, "auditInfo"), state=_s(row, "state"),
        order_warning=_s(row, "orderWarning"), company_class=_s(row, "companyClassName"),
        reg_day=_yyyymmdd(_s(row, "regDay")), list_count=_int(_s(row, "listCount")),
        last_price=_int(_s(row, "lastPrice")), risk_flags=flags, collect=collect,
        eligible_now=(stype in policy.s1_types and not flags), exclusions=tuple(excl), raw=dict(row),
    )


def classify_rows(rows: list[dict], policy: UniversePolicy | None = None) -> list[ListingRecord]:
    """중복 코드는 오류(같은 스냅숏에 두 번 나오면 원천 이상)."""
    policy = policy or UniversePolicy()
    out = [classify_row(r, policy) for r in rows]
    seen: set[str] = set()
    for rec in out:
        if rec.code in seen:
            raise ValueError(f"종목 목록에 같은 코드가 두 번 있음: {rec.code}")
        seen.add(rec.code)
    return out


def summarize(records: list[ListingRecord]) -> dict:
    stocks = [r for r in records if r.market is not None]
    by_type: dict[str, int] = {}
    for r in records:
        by_type[r.security_type] = by_type.get(r.security_type, 0) + 1
    collect = [r for r in records if r.collect]
    flagged = [r for r in collect if r.risk_flags]
    flag_counts: dict[str, int] = {}
    for r in collect:
        for f in r.risk_flags:
            flag_counts[f] = flag_counts.get(f, 0) + 1
    return {
        "rows": len(records),
        "stock_rows": len(stocks),
        "by_type": dict(sorted(by_type.items())),
        "collect": len(collect),
        "collect_risk_flagged": len(flagged),
        "eligible_now": sum(1 for r in records if r.eligible_now),
        "risk_flag_counts_in_collect": dict(sorted(flag_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        # 참고: state에는 관리종목인데 auditInfo는 관리종목이 아닌 행 (세 필드 불일치 확인용).
        # 전체 목록 기준(ETF·리츠 포함)과 주식 기준을 따로 셈 — 2026-09-30 실측 84 / 82
        "state_admin_not_in_audit_all_rows": sum(1 for r in records if "STATE:관리종목" in r.risk_flags
                                                 and r.audit_info != "관리종목"),
        "state_admin_not_in_audit_stock_rows": sum(1 for r in stocks if "STATE:관리종목" in r.risk_flags
                                                   and r.audit_info != "관리종목"),
    }
