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

from datetime import datetime

from config.settings import AppConfig, BrokerConfig, KakaoConfig, Settings, StorageConfig
from domain.models import (
    AccountBalance, BrokerOrder, BrokerOrderStatus, MarketPrice, OrderResult,
    OrderStatusEvidence, Position,
)
from infra.broker.base import Broker


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
            fill_ledger_file=f"{tmpdir}/fill_ledger.jsonl",
        ),
        kakao=KakaoConfig(token_state_file=f"{tmpdir}/kakao_token_state.json"),
    )


class ScriptedBroker(Broker):
    """주문 결과·체결 조회 결과를 테스트가 직접 지정하는 브로커.

    잔고는 positions를 테스트가 직접 바꿔야만 바뀝니다(주문해도 자동 체결 없음).
    """

    def __init__(self):
        self.positions: dict[str, int] = {}
        self.next_result: list[str] = []
        self.status: dict[str, object] = {}
        self.place_calls: list[tuple] = []
        self.status_calls: list[tuple] = []
        self.seq = 0

    def authenticate(self):
        pass

    def get_market_price(self, symbol):
        return MarketPrice(symbol, 10000, 10000, 10000, datetime.now())

    def get_account_balance(self):
        return AccountBalance(
            100_000_000, 100_000_000,
            [Position(s, q, 10000) for s, q in self.positions.items() if q > 0],
        )

    def place_order(self, order):
        self.place_calls.append((order.symbol, order.side.value, order.quantity))
        kind = self.next_result.pop(0) if self.next_result else "accept"
        self.seq += 1
        oid = f"{self.seq:07d}"
        if kind == "raise":
            raise RuntimeError("unexpected")
        if kind == "ambiguous":
            return OrderResult("", order.symbol, order.side, order.quantity, False, "timeout",
                               datetime.now(), is_ambiguous=True)
        if kind == "reject":
            return OrderResult(oid, order.symbol, order.side, order.quantity, False,
                               "[RC4007] 매매제한 종목", datetime.now())
        if kind == "accept_noid":
            return OrderResult("", order.symbol, order.side, order.quantity, True, "ok", datetime.now())
        return OrderResult(oid, order.symbol, order.side, order.quantity, True, "ok", datetime.now())

    def get_order_status(self, order_id, symbol):
        self.status_calls.append((symbol, order_id))
        st = self.status.get(order_id, BrokerOrderStatus.UNKNOWN)
        if isinstance(st, Exception):
            raise st
        return BrokerOrder(order_id, symbol, st)

    def get_order_status_evidence(self, order_id, symbol):
        return OrderStatusEvidence(broker_order=self.get_order_status(order_id, symbol))

    def get_open_orders(self, symbol):
        return []

    def get_daily_prices(self, symbol, days):
        return []

    def get_weekly_prices(self, symbol, weeks):
        return []

    def get_minute_bars(self, symbol, tick_scope=3, count=40):
        return []
