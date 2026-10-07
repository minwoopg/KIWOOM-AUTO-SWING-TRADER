from __future__ import annotations

"""사용자 지정 종목 설정 `config/watchlist.yaml` (스키마 w1) — 해석·검증만 하는 순수 함수 (파일·DB·API 없음).

대상 선택은 사용자가 합니다. 이 모듈은 그 선택을 검사해 정규화된 설정(WatchConfig)을 만들거나 오류를 돌려줍니다.
저장·적용 이력·마지막 정상 설정 유지는 `infra/watch/`가 맡습니다.

종목 하나 = 관심(interest)과 수동 보유(holding) 두 부분 — 한 종목이 둘 다일 수 있고, 가격 조회는 함께 씁니다.
- interest : enabled(관심 감시 켜기/끄기), s1_analysis(S1 분석 대상 여부), price_bands(관심 가격대 low~high, 원).
- holding  : **수동 입력** 보유 정보 — quantity·avg_price 필수, stop_price·target_price 선택. 증권사 잔고와 같다고
             보지 않음(표시도 "수동 보유 정보"). 보유가 남아 있는 동안 보유 감시는 끌 수 없음 — 끝내려면 holding을
             지움(청산). 관심을 꺼도 보유 감시는 유지.
- code     : 6자리 문자열(따옴표). YAML이 숫자로 읽은 코드(앞자리 0 손실)는 오류.
- market   : 지금은 KRX만(국내). 해외는 후속 단계.

종목 목록(ka10099 스냅숏) 대조 — listing이 주어질 때
- 목록 스냅숏은 KOSPI·KOSDAQ 주식 행(보통주·우선주·스팩·외국기업)만 담음 — ETF·ETN 등은 지금 지원하지 않음.
- 관심 감시가 켜진 종목이 목록에 없으면 오류. 보유만 있는 종목이 목록에 없으면 경고(보유 감시는 유지, 데이터 UNKNOWN).
- 보통주가 아닌 종목에 s1_analysis: true면 오류(S1은 보통주만).
- 현재 위험 표시가 있으면 경고 — 감시는 유지, 신규 진입 관찰에서 제외(삭제하지 않음).
- 이름(name)이 목록 이름과 다르면 경고.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

import yaml

from domain.research.s1 import S1Config

S1_MIN_HISTORY = S1Config().min_history   # S1 계산 계약의 최소 이력 — 이보다 짧은 준비 기준은 허용 안 함

WATCH_SCHEMA = "w1"
MARKETS = ("KRX",)
MAX_SYMBOLS = 200
MAX_BANDS = 10
_CODE = re.compile(r"^[0-9A-Z]{6}$")
COMMON = "COMMON"


@dataclass(frozen=True)
class Listing:
    """종목 목록 한 행에서 검증에 쓰는 값."""
    code: str
    name: str
    market: str | None
    security_type: str
    risk_flags: tuple[str, ...] = ()
    reg_day: str | None = None


@dataclass(frozen=True)
class PriceBand:
    low: int
    high: int
    label: str = ""


@dataclass(frozen=True)
class Interest:
    enabled: bool
    s1_analysis: bool = False
    price_bands: tuple[PriceBand, ...] = ()


@dataclass(frozen=True)
class ManualHolding:
    quantity: int
    avg_price: int
    stop_price: int | None = None
    target_price: int | None = None
    source: str = "MANUAL"            # 수동 입력 — 증권사 잔고 아님


@dataclass(frozen=True)
class WatchSymbol:
    code: str
    market: str = "KRX"
    name: str | None = None
    memo: str = ""
    interest: Interest | None = None
    holding: ManualHolding | None = None

    @property
    def key(self) -> str:
        return f"{self.market}:{self.code}"

    @property
    def interest_active(self) -> bool:
        return self.interest is not None and self.interest.enabled

    @property
    def holding_active(self) -> bool:
        return self.holding is not None

    @property
    def active(self) -> bool:
        """가격·데이터 감시 대상인지 (관심 켜짐 또는 보유 있음)."""
        return self.interest_active or self.holding_active

    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(m for m, on in (("INTEREST", self.interest_active), ("HOLDING", self.holding_active)) if on)


@dataclass(frozen=True)
class MonitorSettings:
    interval_sec: int = 60            # 장중 가격 확인 간격(다음 단계에서 사용)
    history_sessions: int = S1_MIN_HISTORY   # S1 분석 준비 이력(봉). S1 min_history 이상만(W1b-R1), 거래일 달력 범위 안


@dataclass(frozen=True)
class AlertSettings:
    repeat_limit_per_day: int = 3     # 같은 조건 알림 하루 최대 횟수
    min_repeat_interval_min: int = 30 # 같은 조건 재알림 최소 간격
    rearm_on_clear: bool = True       # 조건이 풀렸다가 다시 참이 되면 새 알림


@dataclass(frozen=True)
class WatchConfig:
    schema: str
    monitor: MonitorSettings
    alerts: AlertSettings
    symbols: tuple[WatchSymbol, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def norm_hash(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def symbol(self, code: str) -> WatchSymbol | None:
        return next((s for s in self.symbols if s.code == code), None)

    @property
    def active_symbols(self) -> tuple[WatchSymbol, ...]:
        return tuple(s for s in self.symbols if s.active)


@dataclass(frozen=True)
class Issue:
    code: str                         # 종목코드 또는 "-"(전체)
    field: str                        # 예: symbols[2].holding.avg_price
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.field}: {self.message}"


@dataclass
class ValidationResult:
    config: WatchConfig | None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.config is not None and not self.errors


class _Errors(Exception):
    pass


class _V:
    """오류를 모으며 해석 — 첫 오류에서 멈추지 않고 종목·필드별로 모두 보여줌."""

    def __init__(self) -> None:
        self.errors: list[Issue] = []
        self.warnings: list[Issue] = []

    def err(self, code: str, where: str, msg: str) -> None:
        self.errors.append(Issue(code, where, msg))

    def warn(self, code: str, where: str, msg: str) -> None:
        self.warnings.append(Issue(code, where, msg))

    def only(self, d, keys: set, code: str, where: str) -> dict | None:
        if not isinstance(d, dict):
            self.err(code, where, "사전(키: 값)이어야 함")
            return None
        extra = set(d) - keys
        if extra:
            self.err(code, where, f"모르는 키 {sorted(map(str, extra))} (허용: {sorted(keys)})")
        return d

    def boolean(self, d: dict, k: str, default, code: str, where: str):
        v = d.get(k, default)
        if v is None or not isinstance(v, bool):
            self.err(code, f"{where}.{k}", f"true/false여야 함 (현재 {v!r})")
            return default
        return v

    def integer(self, d: dict, k: str, lo: int, hi: int, code: str, where: str, *, default=None, required=False):
        if k not in d or d[k] is None:
            if required:
                self.err(code, f"{where}.{k}", "필수")
            return default
        v = d[k]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            self.err(code, f"{where}.{k}", f"{lo:,}~{hi:,} 정수여야 함 (현재 {v!r})")
            return default
        return v


PRICE_MAX = 100_000_000


def parse_text(text: str) -> tuple[object, str | None]:
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"YAML 형식 오류: {exc}".replace("\n", " ")[:400]


def validate(raw, listing: dict[str, Listing] | None, *, check_list: bool = True) -> ValidationResult:
    """raw = yaml.safe_load 결과. listing = 종목 목록(코드 → Listing). check_list=False면 형식만 검사(적용 불가)."""
    v = _V()
    if raw is None:
        raw = {}
    top = v.only(raw, {"schema", "monitor", "alerts", "symbols"}, "-", "watchlist.yaml")
    if top is None:
        return ValidationResult(None, v.errors, v.warnings)
    if top.get("schema") != WATCH_SCHEMA:
        v.err("-", "schema", f"'{WATCH_SCHEMA}'여야 함 (현재 {top.get('schema')!r})")
    mon = v.only(top.get("monitor") or {}, {"interval_sec", "history_sessions"}, "-", "monitor") or {}
    monitor = MonitorSettings(v.integer(mon, "interval_sec", 10, 3600, "-", "monitor", default=60),
                              v.integer(mon, "history_sessions", S1_MIN_HISTORY, 1000, "-", "monitor", default=S1_MIN_HISTORY))
    al = v.only(top.get("alerts") or {}, {"repeat_limit_per_day", "min_repeat_interval_min", "rearm_on_clear"},
                "-", "alerts") or {}
    alerts = AlertSettings(v.integer(al, "repeat_limit_per_day", 1, 50, "-", "alerts", default=3),
                           v.integer(al, "min_repeat_interval_min", 1, 1440, "-", "alerts", default=30),
                           v.boolean(al, "rearm_on_clear", True, "-", "alerts"))
    items = top.get("symbols")
    if items is None:
        items = []
    if not isinstance(items, list):
        v.err("-", "symbols", "목록이어야 함")
        items = []
    if len(items) > MAX_SYMBOLS:
        v.err("-", "symbols", f"최대 {MAX_SYMBOLS}종목 (현재 {len(items)})")
    symbols: list[WatchSymbol] = []
    seen: dict[str, int] = {}
    for i, it in enumerate(items):
        s = _symbol(v, it, i)
        if s is None:
            continue
        if s.key in seen:
            v.err(s.code, f"symbols[{i}].code", f"중복 — symbols[{seen[s.key]}]와 같은 종목(관심·보유는 한 항목에)")
            continue
        seen[s.key] = i
        symbols.append(s)
    if check_list:
        if listing is None:
            v.err("-", "listing", "종목 목록 스냅숏이 없어 코드를 대조할 수 없음 — 목록을 먼저 받으세요(prepare)")
        else:
            for i, s in enumerate(symbols):
                _against_list(v, s, listing.get(s.code), f"symbols[{seen[s.key]}]")
    if v.errors:
        return ValidationResult(None, v.errors, v.warnings)
    return ValidationResult(WatchConfig(WATCH_SCHEMA, monitor, alerts, tuple(symbols)), [], v.warnings)


def _symbol(v: _V, it, i: int) -> WatchSymbol | None:
    where = f"symbols[{i}]"
    raw_code = it.get("code") if isinstance(it, dict) else None
    code = raw_code if isinstance(raw_code, str) else str(raw_code)
    d = v.only(it, {"code", "market", "name", "memo", "interest", "holding"}, code, where)
    if d is None:
        return None
    if not isinstance(raw_code, str):
        v.err(code, f"{where}.code", f"따옴표로 감싼 문자열이어야 함 (예: \"005930\") — 현재 {raw_code!r}"
                                     " (숫자로 읽히면 앞자리 0이 사라짐)")
        return None
    code = raw_code.strip().upper()
    if not _CODE.match(code):
        v.err(code, f"{where}.code", f"6자리 종목코드여야 함 (현재 {raw_code!r})")
        return None
    market = d.get("market", "KRX")
    if market not in MARKETS:
        v.err(code, f"{where}.market", f"지금은 {list(MARKETS)}만 지원 (현재 {market!r}) — 해외는 후속 단계")
    name = d.get("name")
    if name is not None and not isinstance(name, str):
        v.err(code, f"{where}.name", "문자열이어야 함")
        name = None
    memo = d.get("memo", "") or ""
    if not isinstance(memo, str):
        v.err(code, f"{where}.memo", "문자열이어야 함")
        memo = ""
    interest = holding = None
    if d.get("interest") is not None:
        interest = _interest(v, d["interest"], code, f"{where}.interest")
    if d.get("holding") is not None:
        holding = _holding(v, d["holding"], code, f"{where}.holding")
    if d.get("interest") is None and d.get("holding") is None:
        v.err(code, where, "interest(관심) 또는 holding(수동 보유) 중 하나는 있어야 함")
    return WatchSymbol(code, str(market), name, memo, interest, holding)


def _interest(v: _V, d, code: str, where: str) -> Interest | None:
    d = v.only(d, {"enabled", "s1_analysis", "price_bands"}, code, where)
    if d is None:
        return None
    if "enabled" not in d:
        v.err(code, f"{where}.enabled", "필수 (true/false)")
    enabled = v.boolean(d, "enabled", False, code, where)
    s1 = v.boolean(d, "s1_analysis", False, code, where)
    bands = d.get("price_bands") or []
    if not isinstance(bands, list):
        v.err(code, f"{where}.price_bands", "목록이어야 함")
        bands = []
    if len(bands) > MAX_BANDS:
        v.err(code, f"{where}.price_bands", f"최대 {MAX_BANDS}개")
    out = []
    for j, b in enumerate(bands):
        w = f"{where}.price_bands[{j}]"
        b = v.only(b, {"low", "high", "label"}, code, w)
        if b is None:
            continue
        lo = v.integer(b, "low", 1, PRICE_MAX, code, w, required=True)
        hi = v.integer(b, "high", 1, PRICE_MAX, code, w, required=True)
        label = b.get("label", "") or ""
        if not isinstance(label, str):
            v.err(code, f"{w}.label", "문자열이어야 함")
            label = ""
        if lo is not None and hi is not None:
            if lo > hi:
                v.err(code, w, f"low({lo:,}) ≤ high({hi:,})여야 함")
            else:
                out.append(PriceBand(lo, hi, label))
    return Interest(enabled, s1, tuple(out))


def _holding(v: _V, d, code: str, where: str) -> ManualHolding | None:
    d = v.only(d, {"quantity", "avg_price", "stop_price", "target_price"}, code, where)
    if d is None:
        return None
    qty = v.integer(d, "quantity", 1, 100_000_000, code, where, required=True)
    avg = v.integer(d, "avg_price", 1, PRICE_MAX, code, where, required=True)
    stop = v.integer(d, "stop_price", 1, PRICE_MAX, code, where)
    target = v.integer(d, "target_price", 1, PRICE_MAX, code, where)
    if stop is not None and target is not None and stop >= target:
        v.err(code, where, f"stop_price({stop:,}) < target_price({target:,})여야 함")
    if qty is None or avg is None:
        return None
    if stop is None:
        v.warn(code, f"{where}.stop_price", "손절가 없음 — 보유 위험 감시에 가격 기준이 없습니다")
    return ManualHolding(qty, avg, stop, target)


def _against_list(v: _V, s: WatchSymbol, row: Listing | None, where: str) -> None:
    if row is None:
        if s.interest_active:
            v.err(s.code, f"{where}.code", "종목 목록에 없음 — 코드 확인(상장폐지·오타)")
        elif s.holding_active:
            v.warn(s.code, f"{where}.code", "종목 목록에 없음 — 수동 보유 감시는 유지하지만 데이터는 UNKNOWN")
        else:
            v.warn(s.code, f"{where}.code", "종목 목록에 없음(비활성 항목)")
        return
    if s.name and s.name != row.name:
        v.warn(s.code, f"{where}.name", f"목록 이름은 '{row.name}'")
    if s.interest is not None and s.interest.s1_analysis and row.security_type != COMMON:
        v.err(s.code, f"{where}.interest.s1_analysis", f"S1 분석은 보통주만 — 이 종목 유형 {row.security_type}")
    if row.risk_flags:
        v.warn(s.code, where, f"현재 위험 표시 {list(row.risk_flags)} — 감시는 유지, 신규 진입 관찰 제외")


def config_from_dict(d: dict) -> WatchConfig:
    """to_dict()로 저장한 정규화 설정 → WatchConfig (마지막 정상 설정 복원용)."""
    syms = []
    for x in d["symbols"]:
        it = x.get("interest")
        hd = x.get("holding")
        syms.append(WatchSymbol(
            x["code"], x["market"], x.get("name"), x.get("memo", ""),
            None if it is None else Interest(it["enabled"], it["s1_analysis"],
                                             tuple(PriceBand(**b) for b in it["price_bands"])),
            None if hd is None else ManualHolding(**hd)))
    return WatchConfig(d["schema"], MonitorSettings(**d["monitor"]), AlertSettings(**d["alerts"]), tuple(syms))


# ── 편집(CLI) — 원본 사전을 바꿔서 돌려줌. 검증은 호출하는 쪽이 validate()로 ──────────
def empty_document() -> dict:
    return {"schema": WATCH_SCHEMA, "monitor": asdict(MonitorSettings()), "alerts": asdict(AlertSettings()),
            "symbols": []}


def find_item(doc: dict, code: str) -> dict | None:
    for it in doc.get("symbols") or []:
        if isinstance(it, dict) and str(it.get("code", "")).strip().upper() == code:
            return it
    return None


class _Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data: str):
    # 숫자로 보이는 문자열(종목코드)은 항상 따옴표 — 다시 읽을 때 숫자로 바뀌지 않게
    style = '"' if data and (data[0].isdigit() or data.lower() in ("true", "false", "null", "yes", "no")) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_rep)

HEADER = """# 사용자 지정 종목 설정 (스키마 w1) — docs/watchlist.md
# tools/watchlist.py로 바꾸면 이 파일을 다시 씁니다(주석은 이 머리말만 유지). 직접 고쳐도 됩니다.
# 잘못 고치면: 처음이면 감시를 시작하지 않고, 운영 중이면 마지막 정상 설정으로 계속(오류·버전 표시, 신규 매수 차단).
# holding = 수동 입력 보유 정보(증권사 잔고 아님). 보유가 있는 동안 보유 감시는 끌 수 없습니다.
"""


def dump_document(doc: dict) -> str:
    return HEADER + yaml.dump(doc, Dumper=_Dumper, allow_unicode=True, sort_keys=False, default_flow_style=False)
