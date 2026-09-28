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
class Settings:
    """프로그램 전체 설정을 한 번에 담는 최상위 객체입니다."""

    app: AppConfig
    broker: BrokerConfig
    storage: StorageConfig
    kakao: KakaoConfig = None
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)


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
    )
