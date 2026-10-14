from __future__ import annotations

"""설정 파일을 읽어서 파이썬 객체로 변환하는 모듈.

2026-09-28 (스윙 분리 1라운드): 단타 레포 `config/settings.py`(bdde6c2)에서
매매 로직과 무관한 설정(App/Broker/Storage/Kakao)만 남겼습니다.
Trading/Strategy/Risk/MarketRegime/EntryWatch/WebSocket/Experimental은
단타 전용이라 제외했고, 스윙 설정은 스윙 로직 라운드에서 새로 추가합니다.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from domain.service.trade_costs import PAPER_COMMISSION_RATE, PAPER_SELL_TAX_RATE

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True)
class AppConfig:
    name: str
    env: str
    log_level: str


@dataclass(frozen=True)
class BrokerConfig:
    provider: str
    use_mock: bool
    base_url: str
    app_key: str
    secret_key: str
    account_number: str
    is_paper_trading: bool
    # 2026-09-18 (우선순위1 1차: 체결조회 증거 독립 저장): 계좌를
    # 구분하는 **비민감** 식별자(예: "acct-a") — 실계좌번호(account_number)
    # 자체는 아니며, 관측 저장소의 dedup·커버리지 집계 키에만 쓰입니다.
    # 기본값 ""(빈 문자열)은 "라벨 미설정" 상태이며, 이 경우 관측
    # 기능만 비활성화됩니다(기동 자체를 막지 않음 — 아래
    # `observation_enabled` 참고). settings.yaml에 이 키가 없어도
    # 하위호환으로 정상 로드됩니다(기존 설정 파일 변경 불필요).
    account_scope_id: str = ""

    @property
    def observation_enabled(self) -> bool:
        """`account_scope_id`가 비어있지 않을 때만 체결조회 증거
        관측 기능이 활성화됩니다. 이 값이 False라고 해서 프로그램
        기동 자체를 막지 않습니다 — 매매 로직과 무관한 부가 계측
        기능이므로, 라벨이 없으면 그 계측만 꺼지고 커버리지는
        "0%"가 아니라 "계측 비활성"으로 별도 표시되어야 합니다."""

        return bool(self.account_scope_id.strip())


@dataclass
class KakaoConfig:
    access_token:  str = ""   # 카카오 액세스 토큰
    refresh_token: str = ""   # 리프레시 토큰 (자동 갱신용)
    rest_api_key:  str = ""   # REST API 키 (토큰 갱신용)
    # 2026-09-11: 카카오 앱의 "Client Secret 사용함"이 켜진 경우에만
    # 필요. 기존처럼 꺼져 있으면 빈 문자열로 두면 됨(하위호환).
    client_secret: str = ""
    # 2026-09-11: refresh로 새로 발급된 토큰을 재시작 후에도 이어 쓸 수
    # 있게 저장하는 경로. .env는 코드가 직접 고치지 않고, 이 파일(이미
    # .gitignore의 data/ 아래)에 원자적으로 저장·우선 로드함.
    token_state_file: str = "data/kakao_token_state.json"


@dataclass(frozen=True)
class StorageConfig:
    """파일 경로 설정.

    단타 레포의 StorageConfig에서 주문 추적·관측·기준선 관련 경로만 남겼습니다.
    shadow/entry_watch/분봉 저장 경로는 제외했습니다.
    """

    state_file: str
    trade_log_file: str
    app_log_file: str
    # 포지션 상태머신(PSM) 전이 로그
    position_lifecycle_log_file: str = "logs/position_lifecycle.csv"
    # 체결 확정 전 주문 사실의 원자적 보존(재시작 복구용)
    tracked_order_journal_file: str = "data/tracked_order_journal.json"
    # 실행 기준선(run_id/git_sha/설정 해시) 기록
    run_baseline_log_file: str = "logs/run_baseline.csv"
    # 체결조회 증거(JSON Lines, append-only)
    order_status_observation_log_file: str = "logs/order_status_observations.jsonl"
    # 체결 원장(JSON Lines, append-only) — 보유 수량·매입단가·진입일의 단일 원천 (4라운드)
    fill_ledger_file: str = "data/fill_ledger.jsonl"
    # 일일 리포트 폴더 (7라운드)
    reports_dir: str = "reports"
    # 사람 확인 복구 명령 폴더 (8-G — 테스트는 임시 폴더를 씀)
    commands_dir: str = "commands"


@dataclass(frozen=True)
class MarketDataConfig:
    """일봉 수집 설정 (5라운드).

    실측(2026-09-28): 0.5초 간격 5번째 호출에서 HTTP 429 → 기본 간격 1초.
    한 페이지 600행(약 2.4년) → backfill_pages=3이면 약 7년.
    """

    daily_bars_dir: str = "data/daily_bars"
    min_call_interval_sec: float = 1.0
    retry_backoff_sec: tuple = (2.0, 5.0, 10.0, 20.0)
    backfill_pages: int = 3

    def __post_init__(self) -> None:
        if self.min_call_interval_sec < 0.5:
            raise ValueError("min_call_interval_sec는 0.5 이상 (실측상 0.5초 간격에서도 429 발생)")
        if self.backfill_pages < 1:
            raise ValueError("backfill_pages는 1 이상")
        object.__setattr__(self, "retry_backoff_sec", tuple(float(x) for x in self.retry_backoff_sec))


@dataclass(frozen=True)
class SessionSettings:
    """하루 수명주기 (6라운드) — app/session_runner.SessionConfig로 변환."""

    poll_interval_sec: float = 60.0
    close_reconcile_until: str = "15:45"
    watch_symbols: tuple = ()
    update_daily_bars_after_close: bool = False
    # M3 A2: 지정가 진입 마감(HH:MM). 이후 신규 지정가 매수 거부 + 이 실행이 낸 미체결 지정가 매수 취소.
    # ""면 끔. 시장가 주문·재시작 전 주문은 건드리지 않음.
    entry_cancel_at: str = "09:30"


@dataclass(frozen=True)
class GuardSettings:
    """계좌 안전 한도 (6라운드) — domain/risk/account_guard.GuardConfig로 변환."""

    max_positions: int = 5
    max_order_amount: int = 2_000_000
    max_total_exposure: int = 10_000_000
    min_cash_buffer: int = 100_000
    new_orders_start: str = "09:05"
    new_orders_end: str = "15:15"
    allowed_symbols: tuple = ()
    buy_price_buffer_pct: float = 1.0    # 시장가 매수 체결가 변동 여유 (8-B)


STRATEGY_MODES = ("off", "shadow", "paper")


@dataclass(frozen=True)
class StrategySettings:
    """자동 전략 연결 (M3 B, settings.yaml `swing_strategy:`). 기본 off — NullStrategy(주문 의도 없음).

    mode
      off    : NullStrategy
      shadow : S1 후보를 읽어 주문 의도를 만들고 안전 한도 판정까지 한 뒤 **전송하지 않고 기록**(data/shadow_intents.jsonl)
      paper  : 실제 모의 전송(C3) — 모의 서버(mockapi)·use_mock false·허용 종목 1개 이상일 때만 기동
    watch_dir: 관리자 DB 폴더(daemon.sqlite3·watch_s1.sqlite3·watch_open.sqlite3) — 읽기 전용(mode=ro)으로만 엶
    min_after_close_min: 승인할 스캔의 완성 기준 하한(분). 18:10(160분) 스캔은 잠정 관찰로만 남기고 승인하지 않음
    quantity: 매수 수량(최소판 1주 고정 — 1이 아니면 기동 거부)
    entry_start: 신규 진입 시작(진입 마감은 session.entry_cancel_at)
    allowed_symbols: 신규 매수 허용 종목(C3). 비어 있으면 shadow는 제한 없음, paper는 기동 거부
    daily_loss_limit: 일일 손실 한도(원, 양수). 그날 거래 손익(비용 후) ≤ −한도 → 신규 매수 중단(commands/halt.json — 사람이 재개)
    max_failed_orders: 그날 미접수·응답 불명 주문이 이 수에 닿으면 신규 매수 중단(같은 방식)
    commission_rate / sell_tax_rate: 체결 근거가 없을 때의 비용 추정(모의 실측 10/13: 0.35%·0.20%)
    """

    mode: str = "off"
    id: str = "s1"
    watch_dir: str = "data/watch"
    min_after_close_min: int = 300
    quantity: int = 1
    entry_start: str = "09:05"
    allowed_symbols: tuple[str, ...] = ()
    daily_loss_limit: int = 10_000
    max_failed_orders: int = 1
    commission_rate: float = PAPER_COMMISSION_RATE
    sell_tax_rate: float = PAPER_SELL_TAX_RATE

    def __post_init__(self) -> None:
        syms = tuple(str(x).strip() for x in (self.allowed_symbols or ()))
        if any(not (len(x) == 6 and x.isdigit()) for x in syms):
            raise ValueError(f"swing_strategy.allowed_symbols는 6자리 종목코드 목록 — {self.allowed_symbols!r}")
        object.__setattr__(self, "allowed_symbols", syms)
        if type(self.daily_loss_limit) is not int or self.daily_loss_limit <= 0:
            raise ValueError("swing_strategy.daily_loss_limit는 양의 정수(원)")
        if type(self.max_failed_orders) is not int or self.max_failed_orders < 1:
            raise ValueError("swing_strategy.max_failed_orders는 1 이상 정수")
        for name in ("commission_rate", "sell_tax_rate"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v < 0.05):
                raise ValueError(f"swing_strategy.{name}는 0 이상 0.05 미만")
        if self.mode not in STRATEGY_MODES:
            raise ValueError(f"swing_strategy.mode={self.mode!r} (허용: {STRATEGY_MODES})")
        if self.id != "s1":
            raise ValueError(f"swing_strategy.id={self.id!r} — 지금은 s1만")
        if type(self.quantity) is not int or self.quantity != 1:
            raise ValueError("swing_strategy.quantity는 최소판에서 1 고정")


@dataclass(frozen=True)
class Settings:
    """프로그램 전체 설정을 한 번에 담는 최상위 객체입니다."""

    app: AppConfig
    broker: BrokerConfig
    storage: StorageConfig
    kakao: KakaoConfig = None
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)
    session: SessionSettings = field(default_factory=SessionSettings)
    guard: GuardSettings = field(default_factory=GuardSettings)
    # yaml 키 swing_strategy — 단타 설정의 strategy 섹션과 섞이지 않게 다른 이름(추출 경계 시험 3-1)
    swing_strategy: StrategySettings = field(default_factory=StrategySettings)


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")
        return ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(item) for item in value]
    return value


def load_settings(path: str | Path = "config/settings.yaml") -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    raw = _substitute_env(raw)

    kakao_raw = raw.get("kakao", {})
    return Settings(
        app=AppConfig(**raw["app"]),
        broker=BrokerConfig(**raw["broker"]),
        storage=StorageConfig(**raw["storage"]),
        kakao=KakaoConfig(**kakao_raw) if kakao_raw else KakaoConfig(),
        market_data=MarketDataConfig(**(raw.get("market_data") or {})),
        session=SessionSettings(**{k: (tuple(v) if isinstance(v, list) else v)
                                   for k, v in (raw.get("session") or {}).items()}),
        guard=GuardSettings(**{k: (tuple(v) if isinstance(v, list) else v)
                               for k, v in (raw.get("guard") or {}).items()}),
        swing_strategy=StrategySettings(**(raw.get("swing_strategy") or {})),
    )
