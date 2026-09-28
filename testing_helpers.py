# -*- coding: utf-8 -*-
"""테스트 공용 헬퍼 (2026-09-28, 스윙 분리 1라운드).

단타 레포의 `test_run_once_integration.build_minimal_settings()`를 대체합니다.
그 함수는 Trading/Strategy/MarketRegime 등 단타 설정까지 채웠지만, 이
레포의 `Settings`는 App/Broker/Storage/Kakao만 가지므로 그에 맞춰 새로
작성했습니다.

파일명이 `test_`로 시작하지 않으므로 `run_regression_tests.py`가 테스트로
수집하지 않습니다.

모든 경로는 반드시 tmpdir 기준으로 둡니다 — 단타 레포에서 기본값
상대경로("logs/...", "data/...")가 그대로 쓰여 테스트가 프로젝트 루트의
실제 logs/·data/에 파일을 새게 만든 사고(CHANGELOG_v1.6 0.5단계)가
있었습니다.
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from config.settings import AppConfig, BrokerConfig, KakaoConfig, Settings, StorageConfig


def build_minimal_settings(tmpdir: str) -> Settings:
    """MockBroker와 함께 바로 쓸 수 있는 최소 Settings를 만듭니다."""
    return Settings(
        app=AppConfig(name="test", env="local", log_level="INFO"),
        broker=BrokerConfig(
            provider="kiwoom", use_mock=True, base_url="", app_key="",
            secret_key="", account_number="", is_paper_trading=True,
        ),
        storage=StorageConfig(
            state_file=f"{tmpdir}/state.json",
            trade_log_file=f"{tmpdir}/trades.csv",
            app_log_file=f"{tmpdir}/app.log",
            position_lifecycle_log_file=f"{tmpdir}/position_lifecycle.csv",
            tracked_order_journal_file=f"{tmpdir}/tracked_order_journal.json",
            run_baseline_log_file=f"{tmpdir}/run_baseline.csv",
            order_status_observation_log_file=f"{tmpdir}/order_status_observations.jsonl",
        ),
        kakao=KakaoConfig(token_state_file=f"{tmpdir}/kakao_token_state.json"),
    )
