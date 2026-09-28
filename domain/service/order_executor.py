from __future__ import annotations

"""주문 실행기 — 단타 레포 `TradingService`의 주문 실행·추적·복구 부분만 추출.

2026-09-28 (스윙 분리 2라운드). 출처: kiwoom-auto-trader `bdde6c2`
`domain/service/trading_service.py`.

| 이 파일 | 원본 메서드 (trading_service.py) |
|---|---|
| `restore_order_recovery_blocks()` | `_restore_order_recovery_blocks` |
| `_begin_order_intent` / `_clear_resolved_order_intent` | 동일 이름 |
| `_create_tracked_order_journal_entry` / `_maintain_tracked_order_journal` | 동일 이름 |
| `_select_order_status_query_target` / `_reconcile_tracked_order_status` / `_safe_record_order_status_observation` | 동일 이름 |
| `_process_pending_ack_error_commands` / `_process_pending_ack_orphan_commands` | 동일 이름 |
| `sync_with_balance()` | `_sync_position_state_machine_shadow` |
| `submit_buy()` | `_try_buy`의 주문 의도 기록 이후 부분 |
| `submit_sell()` | `_try_sell` + `_try_sell_unchecked` |
| `has_unresolved_orders()` | `_has_unresolved_orders` |
| 첫 체결 / 완전 청산 훅 | `_apply_first_fill_buy_side_effects` / `_apply_deferred_sell_side_effects`의 **호출 시점**만 |

**원칙: 판정 로직은 원본과 같게, 매매 판단은 넣지 않는다.**

원본과 다른 점 (의도된 변경):

1. 진입 게이트(14:50 차단, 쿨다운, 진입횟수, 리스크 한도, 시세 신선도 등)는
   호출부(전략·리스크 계층) 책임이라 여기 없습니다. 여기 있는 차단은
   주문 안전 게이트뿐입니다(복구 불가 / PSM 차단 / 계좌 내 미해결 주문 /
   주문 의도 기록 실패).
2. 강제 매도 여부는 `forced=True` 인자로만 받습니다. 원본은 매도 사유
   문자열에 "손절"/"강제청산" 등이 들어있으면 강제로 간주했는데(문구 변경에
   취약), 스윙에서는 호출부가 명시합니다.
3. 첫 체결·완전 청산 시점의 부작용(진입시각·손실 카운트·알림 등)은 내용이
   단타 규칙이라 가져오지 않았고, `on_first_fill_buy` / `on_sell_closed`
   훅으로 호출 시점만 제공합니다. 훅 예외는 CRITICAL 로그 후 삼킵니다
   (원본에는 훅이 없었으므로 새로 정한 규칙 — 외부 코드 예외가 PSM 대조
   루프를 끊지 않게).
4. 잔고 캐시 무효화, `_sold_today` 등 단타 루프 전용 상태는 호출부 책임입니다.
   중복 매도는 PSM의 SELL_PENDING HARD block이 막습니다.
5. 조회 대상 목록은 `sync_with_balance(balance, watch_symbols=...)`로 받습니다
   (원본은 `self.targets`).
6. `commands/` 폴더 경로를 생성자 인자로 받습니다(기본값 "commands" — 원본과 동일).
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from domain.models import (
    AccountBalance, BrokerOrderStatus, OrderRequest, OrderResult, OrderSide,
    OrderStatusEvidence, RuntimeState,
)
from domain.position.lifecycle import (
    PositionLifecycle, PositionStateMachine, is_trackable_order_id,
)
from infra.broker.kiwoom_order_status import find_all_matching, normalize_order_id
from infra.storage.order_status_observation_store import (
    OrderStatusObservation, OrderStatusObservationRecorder, build_entry_evidence, resolve_env,
)
from infra.storage.tracked_order_journal import TrackedOrderJournalStore, TrackedOrderRecord
from utils.time_utils import now_kst

# 원본 _try_buy()의 "영구적 매수 실패" 키워드 (2026-07 실측 기반) — 호출부가
# 같은 종목 재시도를 막을지 판단할 때 쓰도록 공개합니다.
PERMANENT_BUY_REJECT_KEYWORDS = (
    "매매제한", "RC4007",       # 매매제한 종목
    "거래정지", "관리종목",       # 거래정지/관리종목
    "상장폐지",                  # 상장폐지
)


def is_permanent_buy_reject(message: str | None) -> bool:
    msg = message or ""
    return any(kw in msg for kw in PERMANENT_BUY_REJECT_KEYWORDS)


@dataclass(frozen=True)
class OrderSubmission:
    """submit_buy()/submit_sell()의 결과.

    block_code가 비어 있으면 브로커에 주문을 보냈다는 뜻입니다(접수 여부는
    result.accepted로 확인). block_code가 있으면 result는 None일 수도 있고
    (전송 전 차단), 있을 수도 있습니다(ORDER_PLACEMENT_AMBIGUOUS).
    """

    block_code: str
    result: OrderResult | None = None

    @property
    def sent(self) -> bool:
        return self.result is not None

    @property
    def accepted(self) -> bool:
        return bool(self.result is not None and self.result.accepted and not self.block_code)


FirstFillHook = Callable[[str, int, dict], None]
SellClosedHook = Callable[[str, dict], None]


class OrderExecutor:
    # 원본 TradingService와 동일한 값 (2026-08-19 1P0.8-D.1/D.1.1)
    # - ORDER_STATUS_QUERY_MIN_PENDING_AGE_SEC: 방금 시작된 BUY_PENDING/
    #   SELL_PENDING은 잔고 폴링만으로 대부분 해소되므로 조회하지 않음
    # - ORDER_STATUS_QUERY_MIN_INTERVAL_SEC: 같은 종목 재조회 최소 간격
    ORDER_STATUS_QUERY_MIN_PENDING_AGE_SEC = 30
    ORDER_STATUS_QUERY_MIN_INTERVAL_SEC = 30

    def __init__(
        self,
        *,
        settings,
        broker,
        state: RuntimeState,
        highest_price: dict[str, int],
        state_store,
        app_logger,
        trade_logger,
        position_lifecycle_logger=None,
        tracked_order_journal: TrackedOrderJournalStore | None = None,
        order_status_observation_recorder: OrderStatusObservationRecorder | None = None,
        on_first_fill_buy: FirstFillHook | None = None,
        on_sell_closed: SellClosedHook | None = None,
        commands_dir: str | Path = "commands",
    ) -> None:
        """
        state / highest_price는 호출부가 state_store.load()로 읽은 객체를 그대로
        넘깁니다(같은 객체를 공유 — 원본 TradingService에서 한 객체가 둘 다
        들고 있던 것과 같은 효과). 이 클래스는 state.unresolved_order_intents와
        state.last_order_id_by_symbol만 씁니다.
        """
        self.settings = settings
        self.broker = broker
        self.state = state
        self._highest_price = highest_price
        self.state_store = state_store
        self.app_logger = app_logger
        self.trade_logger = trade_logger
        self._commands_dir = Path(commands_dir)
        self._on_first_fill_buy = on_first_fill_buy
        self._on_sell_closed = on_sell_closed

        self._tracked_order_journal = tracked_order_journal or TrackedOrderJournalStore(
            settings.storage.tracked_order_journal_file
        )

        # 원본과 동일한 fail-open 생성 규칙: account_scope_id가 없거나 기록기
        # 생성이 실패하면 관측만 끄고 주문 기능은 그대로 동작.
        self._order_status_observation_recorder = order_status_observation_recorder
        if self._order_status_observation_recorder is None:
            if getattr(settings.broker, "observation_enabled", False):
                try:
                    self._order_status_observation_recorder = OrderStatusObservationRecorder(
                        settings.storage.order_status_observation_log_file,
                        app_logger=self.app_logger,
                    )
                    self._order_status_observation_recorder.start()
                except Exception as exc:
                    self.app_logger.warning(
                        f"[ORDER_STATUS_OBS] 기록기 생성 실패 — 이번 실행에서는 관측을 비활성화합니다"
                        f"(BUY/SELL/리스크 판정에는 영향 없음): {type(exc).__name__}: {exc}"
                    )
                    self._order_status_observation_recorder = None
            else:
                self.app_logger.info(
                    "[ORDER_STATUS_OBS] account_scope_id 미설정 — 이번 실행은 관측 기능을 "
                    "비활성화합니다(매매 프로그램 기동에는 영향 없음, 커버리지는 "
                    "'계측 비활성'으로 표시됨)"
                )

        self._position_state_machine = PositionStateMachine(logger=position_lifecycle_logger)
        self._position_state_machine_initialized = False
        self._journal_recovery_failed = False
        self._pending_sell_side_effects: dict[str, dict] = {}
        self._pending_buy_side_effects: dict[str, dict] = {}
        self._last_order_status_query_at: dict[str, datetime] = {}
        self._last_order_attempt_by_symbol: dict[str, OrderResult] = {}
        self._forced_sell_failures: dict[str, int] = {}
        self.restore_order_recovery_blocks()

    # ══════════════════════════════════════════════════════════════
    # 조회용 (호출부가 게이트 판단에 사용)
    # ══════════════════════════════════════════════════════════════

    @property
    def position_state_machine(self) -> PositionStateMachine:
        return self._position_state_machine

    @property
    def recovery_failed(self) -> bool:
        return self._journal_recovery_failed

    def has_unresolved_orders(self) -> bool:
        """원본 `_has_unresolved_orders`와 동일."""
        return bool(self.state.unresolved_order_intents) or any(
            state.lifecycle in (PositionLifecycle.BUY_PENDING, PositionLifecycle.SELL_PENDING)
            or state.orphan_order_id
            for state in self._position_state_machine._states.values()
        )

    def last_order_attempt(self, symbol: str) -> OrderResult | None:
        return self._last_order_attempt_by_symbol.get(symbol)

    def forced_sell_failure_count(self, symbol: str) -> int:
        return self._forced_sell_failures.get(symbol, 0)

    def save_state(self) -> None:
        self.state_store.save(self.state, self._highest_price)

    def shutdown(self) -> dict | None:
        """관측 기록기 종료 (원본 app/main.py `_shutdown_order_status_observation_recorder`
        에 해당). 기록기가 없으면 None."""
        recorder = self._order_status_observation_recorder
        if recorder is None:
            return None
        try:
            return recorder.shutdown()
        except Exception as exc:
            self.app_logger.warning(
                f"[ORDER_STATUS_OBS] 종료 처리 실패(매매 결과에는 영향 없음): {type(exc).__name__}: {exc}"
            )
            return None

    # ══════════════════════════════════════════════════════════════
    # 재시작 복구 / 주문 의도 기록 (원본 그대로)
    # ══════════════════════════════════════════════════════════════

    def restore_order_recovery_blocks(self) -> None:
        """Restore uncertainty, never infer a fill from a startup balance.

        Existing ack_error commands may clear these blocks only after the
        operator checks both broker orders and holdings. Automatic recovery
        of partial/cancelled orders remains unsupported.
        """
        facts = dict(self.state.unresolved_order_intents)
        try:
            for symbol, record in self._tracked_order_journal.load_all().items():
                facts.setdefault(symbol, {}).update(side=record.side, order_id=record.order_id)
        except Exception as exc:
            self._journal_recovery_failed = True
            self.app_logger.critical(f"[STARTUP_ORDER_RECOVERY] journal 확인 실패 — 자동주문 차단: {exc}")
        for symbol, fact in facts.items():
            state = self._position_state_machine.get(symbol)
            state.pending_order_id = fact.get("order_id") or "UNKNOWN_ORDER_ID"
            self._position_state_machine.on_placement_ambiguous(
                symbol, fact.get("side", "UNKNOWN"), "unresolved order from previous process")
            self.app_logger.critical(
                f"[STARTUP_ORDER_RECOVERY] {symbol} | 미확인 주문 복원 — 주문·잔고 대조 필요"
            )

    def _begin_order_intent(self, symbol: str, side: str, quantity: int) -> bool:
        """Write before sending: a crash/timeout must not erase a submission."""
        self.state.unresolved_order_intents[symbol] = {
            "side": side, "quantity": quantity, "order_id": "",
            "created_at": now_kst().isoformat(),
        }
        try:
            self.state_store.save(self.state, self._highest_price)
            return True
        except Exception as exc:
            self._journal_recovery_failed = True
            self.app_logger.critical(f"[ORDER_INTENT_WRITE_FAILED] {symbol} | 주문 미전송: {exc}")
            return False

    def _clear_resolved_order_intent(self, symbol: str) -> None:
        state = self._position_state_machine.get(symbol)
        if (state.lifecycle in (PositionLifecycle.OPEN, PositionLifecycle.FLAT)
                and not state.pending_order_id and not state.orphan_order_id):
            self.state.unresolved_order_intents.pop(symbol, None)

    def _create_tracked_order_journal_entry(self, symbol: str, side: str) -> None:
        """accepted + 실제 order_id 확정 직후 호출. 추적 불가능한 order_id
        (빈 값/"pending"/"UNKNOWN_ORDER_ID")면 아무 것도 쓰지 않습니다."""
        state = self._position_state_machine.get(symbol)
        order_id = state.pending_order_id
        if not is_trackable_order_id(order_id):
            return
        try:
            record = TrackedOrderRecord(
                symbol=symbol,
                side=side,
                order_id=str(order_id).strip(),
                base_quantity_before_order=state.base_quantity_before_order,
                target_quantity_after_order=(
                    state.expected_final_quantity if side == "BUY" else 0
                ),
                accepted_at=datetime.now(),
                lifecycle_kind="BUY_PENDING" if side == "BUY" else "SELL_PENDING",
            )
            self._tracked_order_journal.upsert(record)
        except Exception as exc:
            self.app_logger.critical(
                f"[TRACKED_ORDER_JOURNAL_ERROR] {symbol} | journal 기록 실패"
                f"(매매 로직에는 영향 없음, 재시작 후 이 주문의 흔적이 없을 "
                f"수 있음) | side={side} | {type(exc).__name__}: {exc}"
            )

    def _maintain_tracked_order_journal(self, symbol: str) -> None:
        """갱신과 "안전할 때만" 삭제. 삭제 조건: lifecycle이 OPEN/FLAT이고,
        추적 중인 pending_order_id도 orphan도 없음. ERROR는 사람이 해소하기
        전까지 절대 삭제되지 않습니다."""
        try:
            record = self._tracked_order_journal.get(symbol)
            if record is None:
                return
            state = self._position_state_machine.get(symbol)
            changed = False
            if state.orphan_order_id and record.orphaned_at is None:
                record.orphaned_at = state.orphan_since or datetime.now()
                changed = True
            if state.partial_fill_since and record.first_fill_at is None:
                record.first_fill_at = state.partial_fill_since
                changed = True
            if (
                state.lifecycle in (PositionLifecycle.OPEN, PositionLifecycle.FLAT)
                and not is_trackable_order_id(state.pending_order_id)
                and not state.orphan_order_id
            ):
                self._tracked_order_journal.remove(symbol)
                return
            if changed:
                self._tracked_order_journal.upsert(record)
        except Exception as exc:
            self.app_logger.critical(
                f"[TRACKED_ORDER_JOURNAL_ERROR] {symbol} | journal 유지 실패"
                f"(매매 로직에는 영향 없음): {type(exc).__name__}: {exc}"
            )

    # ══════════════════════════════════════════════════════════════
    # 주문 전송
    # ══════════════════════════════════════════════════════════════

    def _write_trade_log(
        self, symbol: str, side: str, quantity: int, accepted: bool,
        message: str, order_id: str, price: int = 0, context: dict | None = None,
    ) -> None:
        """원본 `_write_trade_log`와 동일 (TradeCsvLogger가 모르는 키는 무시됨)."""
        row = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "accepted": accepted,
            "message": message,
            "order_id": order_id,
        }
        if context:
            row.update(context)
        self.trade_logger.append(row)

    def _buy_safety_block(self, symbol: str) -> str:
        """원본 _try_buy() 앞부분 중 주문 안전 게이트만."""
        if self._journal_recovery_failed:
            return "ORDER_RECOVERY_UNAVAILABLE"
        _buy_block = self._position_state_machine.would_block_buy_detail(symbol)
        if _buy_block:
            _code, _detail = _buy_block
            self.app_logger.warning(
                f"[LIFECYCLE_BLOCK] {symbol} BUY 차단 | {_code} | {_detail}"
            )
            return _code
        # 계좌 안 어느 종목이든 미해결 주문이 있으면 신규 매수 금지
        if any(
            self._position_state_machine.would_block_buy_detail(sym)
            for sym in self._position_state_machine._states
        ):
            return "ACCOUNT_ORDER_UNRESOLVED"
        return ""

    def submit_buy(
        self,
        symbol: str,
        quantity: int,
        reference_price: int,
        *,
        context: dict | None = None,
    ) -> OrderSubmission:
        """시장가 매수. 원본 _try_buy()의 주문 의도 기록 이후 흐름과 동일.

        reference_price: 주문 직전 시세 (ORDER_PRICE_BASED_ESTIMATE — 실제
            체결가 아님). trades.csv price 컬럼과 첫 체결 훅 ctx에 그대로 들어감.
        context: trades.csv 컨텍스트 컬럼 + 첫 체결 훅 ctx에 함께 실림.
        """
        block = self._buy_safety_block(symbol)
        if block:
            return OrderSubmission(block)
        if quantity <= 0:
            return OrderSubmission("INVALID_QUANTITY")

        current_price = reference_price
        context = dict(context or {})
        order = OrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            price=current_price,
        )

        if not self._begin_order_intent(symbol, "BUY", order.quantity):
            return OrderSubmission("ORDER_INTENT_WRITE_FAILED")
        self._position_state_machine.on_buy_requested(symbol, order.quantity, "pending")
        try:
            result = self.broker.place_order(order)
        except Exception:
            self._position_state_machine.on_placement_ambiguous(symbol, "BUY", "unexpected placement exception")
            raise

        # 응답 자체를 못 받은 경우(타임아웃 등)는 접수 여부를 알 수 없으므로
        # 롤백하지 않고 ERROR로 — 사람이 계좌를 확인할 때까지 BUY/SELL 모두 차단.
        if result.is_ambiguous:
            self._position_state_machine.on_placement_ambiguous(symbol, "BUY", result.message)
            self.app_logger.critical(
                f"[ORDER_PLACEMENT_AMBIGUOUS] {symbol} | side=BUY | "
                f"주문 접수 여부 불명(응답 없음) — 사람 확인 후 "
                f"acknowledge_error() 필요: {result.message}"
            )
            self._last_order_attempt_by_symbol[symbol] = result
            self._write_trade_log(
                order.symbol, order.side.value, quantity, False,
                f"AMBIGUOUS: {result.message}", result.order_id,
                price=current_price, context=context,
            )
            return OrderSubmission("ORDER_PLACEMENT_AMBIGUOUS", result)

        self._position_state_machine.on_buy_result(symbol, result.accepted)
        if not result.accepted:
            self._clear_resolved_order_intent(symbol)
            self.state_store.save(self.state, self._highest_price)
        if result.accepted:
            self._position_state_machine.confirm_pending_order_id(symbol, result.order_id)
            if not str(result.order_id or "").strip():
                self.app_logger.critical(
                    f"[ORDER_ID_MISSING] {symbol} | "
                    f"주문 accepted=True지만 order_id가 비어 있음 | "
                    f"side=BUY"
                )
            self._create_tracked_order_journal_entry(symbol, "BUY")
        self._last_order_attempt_by_symbol[symbol] = result

        self._write_trade_log(
            order.symbol, order.side.value, quantity, result.accepted,
            result.message, result.order_id, price=current_price, context=context,
        )

        if result.accepted:
            # accepted != 실제 진입. 첫 실체결 확인 전까지는 컨텍스트만 저장.
            self.state.last_order_id_by_symbol[symbol] = result.order_id
            self._pending_buy_side_effects[symbol] = {
                **context,
                "order_id": result.order_id,
                "current_price": current_price,
                "quantity": quantity,
            }
            self.app_logger.info(
                f"[ORDER] {symbol} | 매수 주문 접수 완료(미확정) | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매수 주문 실패 | 사유: {result.message}"
            )
        return OrderSubmission("", result)

    def submit_sell(
        self,
        symbol: str,
        quantity: int,
        reference_price: int = 0,
        *,
        exit_reason: str = "",
        avg_buy_price: int = 0,
        forced: bool = False,
        context: dict | None = None,
    ) -> OrderSubmission:
        """시장가 매도. 원본 _try_sell() + _try_sell_unchecked()와 같은 흐름.

        forced: 손절 등 강제 매도. PSM의 SOFT block·재시도 백오프를 넘을 수
            있지만 HARD block(BUY_PENDING/SELL_PENDING/orphan/ERROR)은 못 넘음.
        """
        if self._journal_recovery_failed:
            self.app_logger.critical(f"[ORDER_RECOVERY_UNAVAILABLE] {symbol} SELL — journal 수동 확인 필요")
            return OrderSubmission("ORDER_RECOVERY_UNAVAILABLE")
        if quantity <= 0:
            return OrderSubmission("INVALID_QUANTITY")

        decision = self._position_state_machine.decide_sell(symbol, forced=forced)
        if decision.decision.value == "RECONCILIATION_REQUIRED":
            self.app_logger.critical(
                f"[RECONCILIATION_REQUIRED] {symbol} SELL 차단(장시간) | "
                f"{decision.code} | {decision.detail} | "
                f"요청수량={quantity} 사유={exit_reason} — 브로커에서 직접 확인 필요"
            )
            return OrderSubmission(decision.code or "RECONCILIATION_REQUIRED")
        if decision.decision.value == "BLOCKED":
            self.app_logger.warning(
                f"[LIFECYCLE_BLOCK] {symbol} SELL 차단 | {decision.code} | "
                f"{decision.detail} | 요청수량={quantity} 사유={exit_reason}"
            )
            return OrderSubmission(decision.code or "BLOCKED")
        if decision.decision.value == "THROTTLED":
            self.app_logger.warning(
                f"[LIFECYCLE_FORCE_THROTTLE] {symbol} 강제매도 최소간격 미달 | "
                f"{decision.code} | 사유={exit_reason}"
            )
            return OrderSubmission(decision.code or "THROTTLED")
        if decision.decision.value == "ALLOW_FORCED":
            self.app_logger.warning(
                f"[LIFECYCLE_FORCE] {symbol} 강제 매도 경로 | 사유={exit_reason}"
            )
        return self._submit_sell_unchecked(
            symbol, quantity, reference_price, exit_reason, avg_buy_price, forced,
            dict(context or {}),
        )

    def _submit_sell_unchecked(
        self, symbol: str, quantity: int, current_price: int, exit_reason: str,
        avg_buy_price: int, forced: bool, context: dict,
    ) -> OrderSubmission:
        """실제 매도 주문 발행. 반드시 submit_sell()을 통해 호출."""
        order = OrderRequest(symbol=symbol, side=OrderSide.SELL, quantity=quantity)
        if not self._begin_order_intent(symbol, "SELL", quantity):
            return OrderSubmission("ORDER_INTENT_WRITE_FAILED")
        self._position_state_machine.on_sell_requested(symbol, quantity, "pending")
        try:
            result = self.broker.place_order(order)
        except Exception:
            self._position_state_machine.on_placement_ambiguous(symbol, "SELL", "unexpected placement exception")
            raise

        log_context = {**context, "exit_reason": exit_reason, "avg_buy_price": avg_buy_price}

        if result.is_ambiguous:
            self._position_state_machine.on_placement_ambiguous(symbol, "SELL", result.message)
            if forced:
                self._forced_sell_failures[symbol] = self._forced_sell_failures.get(symbol, 0) + 1
            self.app_logger.critical(
                f"[ORDER_PLACEMENT_AMBIGUOUS] {symbol} | side=SELL | "
                f"주문 접수 여부 불명(응답 없음) — 사람 확인 후 "
                f"acknowledge_error() 필요: {result.message} | 사유={exit_reason}"
            )
            self._last_order_attempt_by_symbol[symbol] = result
            self._write_trade_log(
                order.symbol, order.side.value, quantity, False,
                f"AMBIGUOUS: {result.message}", result.order_id,
                price=current_price, context=log_context,
            )
            return OrderSubmission("ORDER_PLACEMENT_AMBIGUOUS", result)

        if not result.accepted:
            self._position_state_machine.on_sell_result(
                symbol, accepted=False, broker_quantity=0,
                reject_reason=str(getattr(result, "message", "") or "")[:80],
            )
            self._clear_resolved_order_intent(symbol)
            self.state_store.save(self.state, self._highest_price)
            if forced:
                self._forced_sell_failures[symbol] = self._forced_sell_failures.get(symbol, 0) + 1
                self.app_logger.critical(
                    f"[FORCED_SELL_FAILED] {symbol} 강제 매도 거부 "
                    f"(연속 {self._forced_sell_failures[symbol]}회) | "
                    f"사유={exit_reason} | 브로커={getattr(result, 'message', '')}"
                )
        else:
            self._forced_sell_failures.pop(symbol, None)
            self._position_state_machine.confirm_pending_order_id(symbol, result.order_id)
            if not str(result.order_id or "").strip():
                self.app_logger.critical(
                    f"[ORDER_ID_MISSING] {symbol} | "
                    f"주문 accepted=True지만 order_id가 비어 있음 | "
                    f"side=SELL"
                )
            self._create_tracked_order_journal_entry(symbol, "SELL")
        self._last_order_attempt_by_symbol[symbol] = result

        self._write_trade_log(
            order.symbol, order.side.value, quantity, result.accepted,
            result.message, result.order_id, price=current_price, context=log_context,
        )

        if result.accepted:
            self._pending_sell_side_effects[symbol] = {
                **context,
                "exit_reason": exit_reason,
                "avg_buy_price": avg_buy_price,
                "current_price": current_price,  # ORDER_PRICE_BASED_ESTIMATE — 실제 체결가 아님
                "quantity": quantity,
                "forced": forced,
                "order_id": result.order_id,
                "recorded_at": datetime.now(),
            }
            self.app_logger.info(
                f"[ORDER] {symbol} | 매도 주문 접수 완료 | 수량 {quantity}주 | 주문번호 {result.order_id}"
            )
        else:
            self.app_logger.warning(
                f"[FAIL ] {symbol} | 매도 주문 실패 | 사유: {result.message}"
            )
        return OrderSubmission("", result)

    # ══════════════════════════════════════════════════════════════
    # 잔고 대조 (원본 _sync_position_state_machine_shadow)
    # ══════════════════════════════════════════════════════════════

    def sync_with_balance(self, balance: AccountBalance, watch_symbols=()) -> None:
        """Reconcile holdings and tracked orders through lifecycle methods.

        Lifecycle states gate real orders; ERROR and unresolved orders are
        never treated as flat merely because a balance snapshot reports zero.
        매 폴링마다(주문 판단 전에) 호출하십시오.
        """
        psm = self._position_state_machine

        self._process_pending_ack_error_commands()
        self._process_pending_ack_orphan_commands()

        # 첫 호출: 잔고를 그대로 상태머신에 반영만 함 (원본과 동일)
        if not self._position_state_machine_initialized:
            for pos in balance.positions:
                psm.sync_from_broker(pos.symbol, pos.quantity)
            self._position_state_machine_initialized = True
            return

        broker_qty_by_symbol = {p.symbol: p.quantity for p in balance.positions}

        symbols_to_check = set(watch_symbols) | set(psm._states.keys()) | set(broker_qty_by_symbol.keys())

        # 폴링당 order-status 조회 최대 1건 (1P0.8-D.1.1 global budget)
        _order_status_query_target = self._select_order_status_query_target(symbols_to_check)

        for symbol in symbols_to_check:
            broker_qty = broker_qty_by_symbol.get(symbol, 0)
            state = psm.get(symbol)

            _esc = psm.check_block_escalation(symbol)
            if _esc:
                self.app_logger.critical(f"[LIFECYCLE_STUCK] {symbol} {_esc}")
            _orphan = psm.observe_for_orphan(symbol, broker_qty)
            if _orphan:
                self.app_logger.warning(f"[LIFECYCLE_ORPHAN] {symbol} 해소 | {_orphan}")
            violation = psm.check_invariant(symbol, broker_qty)
            if violation:
                self.app_logger.critical(f"[POSITION_STATE_MISMATCH][SHADOW] {violation}")

            _lifecycle_before_sync = state.lifecycle

            if symbol == _order_status_query_target:
                self._reconcile_tracked_order_status(symbol, broker_qty)

            if state.lifecycle == PositionLifecycle.SELL_PENDING:
                psm.on_sell_result(symbol, accepted=True, broker_quantity=broker_qty)
                _resolved = psm.resolve_stale_pending(symbol, broker_qty)
                if _resolved:
                    self.app_logger.warning(f"[LIFECYCLE_TIMEOUT] {symbol} {_resolved}")
            elif state.lifecycle == PositionLifecycle.BUY_PENDING:
                psm.confirm_buy_from_broker(symbol, broker_qty)
                _resolved = psm.resolve_stale_pending(symbol, broker_qty)
                if _resolved:
                    self.app_logger.warning(f"[LIFECYCLE_TIMEOUT] {symbol} {_resolved}")
            else:
                psm.sync_from_broker(symbol, broker_qty)

            # 완전 청산 확정(FLAT 전환) 시점에만 매도 부작용 실행
            if (psm.get(symbol).lifecycle == PositionLifecycle.FLAT
                    and _lifecycle_before_sync != PositionLifecycle.FLAT
                    and symbol in self._pending_sell_side_effects):
                self._apply_sell_closed(symbol)

            # 첫 실체결(수량이 주문 전보다 늘어남) 시점에만 매수 부작용 실행
            _psm_state_now = psm.get(symbol)
            if (symbol in self._pending_buy_side_effects
                    and _psm_state_now.known_quantity > _psm_state_now.base_quantity_before_order):
                self._apply_first_fill_buy(
                    symbol,
                    filled_quantity=(
                        _psm_state_now.known_quantity
                        - _psm_state_now.base_quantity_before_order
                    ),
                )

            if psm.get(symbol).lifecycle == PositionLifecycle.ERROR:
                self.app_logger.critical(
                    f"[POSITION_STATE_ERROR][SHADOW] {symbol} | "
                    f"{psm.get(symbol).last_error} | broker_qty={broker_qty} — "
                    f"실제 계좌 상태를 확인하세요"
                )

            self._maintain_tracked_order_journal(symbol)
            self._clear_resolved_order_intent(symbol)

    def reconcile_after_market_close(self, balance: AccountBalance, watch_symbols=()) -> None:
        """원본과 동일: 미해결 주문이 있을 때만 대조·저장, 주문은 내지 않음."""
        if self.has_unresolved_orders():
            self.sync_with_balance(balance, watch_symbols)
            self.state_store.save(self.state, self._highest_price)

    def _apply_first_fill_buy(self, symbol: str, filled_quantity: int) -> None:
        ctx = self._pending_buy_side_effects.pop(symbol, None)
        if ctx is None:
            return
        self.app_logger.info(
            f"[ORDER] {symbol} | 매수 첫 체결 확인 | 보유 {filled_quantity}주 / "
            f"요청 {ctx.get('quantity')}주 | 주문번호 {ctx.get('order_id')}"
        )
        if self._on_first_fill_buy is None:
            return
        try:
            self._on_first_fill_buy(symbol, filled_quantity, ctx)
        except Exception as exc:
            self.app_logger.critical(
                f"[FIRST_FILL_HOOK_FAILED] {symbol} | 첫 체결 훅 예외(주문 상태 대조는 계속): "
                f"{type(exc).__name__}: {exc}"
            )

    def _apply_sell_closed(self, symbol: str) -> None:
        ctx = self._pending_sell_side_effects.pop(symbol, None)
        if ctx is None:
            return
        self.app_logger.info(
            f"[ORDER] {symbol} | 매도 완전 청산 확인 | 주문번호 {ctx.get('order_id')}"
        )
        if self._on_sell_closed is None:
            return
        try:
            self._on_sell_closed(symbol, ctx)
        except Exception as exc:
            self.app_logger.critical(
                f"[SELL_CLOSED_HOOK_FAILED] {symbol} | 청산 훅 예외(주문 상태 대조는 계속): "
                f"{type(exc).__name__}: {exc}"
            )

    # ══════════════════════════════════════════════════════════════
    # 체결 조회 (원본 그대로)
    # ══════════════════════════════════════════════════════════════

    def _select_order_status_query_target(self, symbols) -> str | None:
        """폴링당 get_order_status() 논리 호출 최대 1건. 조회 자격이 있는 후보 중
        가장 오래 대기한 것 하나만 고릅니다(pending_since / orphan_since 기준).
        자격 판정은 _reconcile_tracked_order_status()와 같은 규칙."""
        psm = self._position_state_machine
        now = datetime.now()
        best_symbol: str | None = None
        best_since: datetime | None = None
        for symbol in symbols:
            state = psm.get(symbol)
            lifecycle = state.lifecycle
            order_id: str | None = None
            since: datetime | None = None
            if lifecycle in (PositionLifecycle.BUY_PENDING, PositionLifecycle.SELL_PENDING):
                order_id = state.pending_order_id
                age_sec = (
                    (now - state.pending_since).total_seconds()
                    if state.pending_since else 0.0
                )
                if age_sec < self.ORDER_STATUS_QUERY_MIN_PENDING_AGE_SEC:
                    continue
                since = state.pending_since or now
            elif psm.has_orphan_order(symbol):
                order_id = state.orphan_order_id
                since = state.orphan_since or now
            else:
                continue
            if not is_trackable_order_id(order_id):
                continue
            last_at = self._last_order_status_query_at.get(symbol)
            if last_at is not None and (
                (now - last_at).total_seconds() < self.ORDER_STATUS_QUERY_MIN_INTERVAL_SEC
            ):
                continue
            if best_since is None or since < best_since:
                best_since = since
                best_symbol = symbol
        return best_symbol

    def _safe_record_order_status_observation(
        self,
        *,
        symbol: str,
        order_id: str,
        kind: str,
        query_id: str,
        started_at_iso: str,
        pending_age_sec: float,
        outcome: str,
        failure_stage: str | None,
        error_repr: str | None,
        evidence: OrderStatusEvidence | None,
        partial_oso_entries: list | None = None,
        partial_cntr_entries: list | None = None,
    ) -> None:
        """조회 결과(성공/실패 모두)를 관측 저장소에 기록. 전체가 try/except라
        어떤 예외도 PSM 판정 흐름에 영향을 주지 않습니다."""
        recorder = self._order_status_observation_recorder
        if recorder is None:
            return
        try:
            account_scope_id = str(getattr(self.settings.broker, "account_scope_id", "") or "")
            env = resolve_env(
                use_mock=bool(getattr(self.settings.broker, "use_mock", False)),
                is_paper_trading=bool(getattr(self.settings.broker, "is_paper_trading", False)),
            )
            cntr_matches: list = []
            oso_matches: list = []
            psm_broker_status: str | None = None
            filled_price_parsed: int | None = None
            if evidence is not None:
                cntr_matches = [build_entry_evidence(e) for e in evidence.matched_cntr_entries]
                oso_matches = [build_entry_evidence(e) for e in evidence.matched_oso_entries]
                psm_broker_status = str(evidence.broker_order.status.value) if evidence.broker_order else None
                filled_price_parsed = evidence.broker_order.filled_price if evidence.broker_order else None
                if evidence.evidence_error and outcome == "success":
                    outcome = "partial"
                    failure_stage = failure_stage or "evidence_build"
                    error_repr = error_repr or evidence.evidence_error
            elif partial_oso_entries or partial_cntr_entries:
                try:
                    target = normalize_order_id(order_id)
                    if partial_oso_entries:
                        oso_matches = [
                            build_entry_evidence(e) for e in find_all_matching(partial_oso_entries, target)
                        ]
                    if partial_cntr_entries:
                        cntr_matches = [
                            build_entry_evidence(e) for e in find_all_matching(partial_cntr_entries, target)
                        ]
                except Exception as exc:
                    self.app_logger.warning(
                        f"[ORDER_STATUS_OBS] {symbol} 부분 조회 결과 매칭 실패(관측만 영향): "
                        f"{type(exc).__name__}: {exc}"
                    )
                outcome = "partial"
            base_qty: int | None = None
            target_qty: int | None = None
            journal_linked = False
            order_accepted_at: str | None = None
            try:
                record = self._tracked_order_journal.get(symbol)
                if record is not None and str(record.order_id) == str(order_id):
                    base_qty = record.base_quantity_before_order
                    target_qty = record.target_quantity_after_order
                    journal_linked = True
                    if record.accepted_at is not None:
                        order_accepted_at = record.accepted_at.isoformat()
            except Exception as exc:
                self.app_logger.warning(
                    f"[ORDER_STATUS_OBS] {symbol} journal 스냅샷 조회 실패(관측만 영향, "
                    f"매매 로직 무관): {type(exc).__name__}: {exc}"
                )
            observation = OrderStatusObservation(
                query_id=query_id,
                started_at=started_at_iso,
                finished_at=datetime.now().isoformat(),
                account_scope_id=account_scope_id,
                env=env,
                symbol=symbol,
                requested_order_id=str(order_id),
                side_context=kind,
                query_kind=kind,
                pending_age_sec_at_query=pending_age_sec,
                outcome=outcome,
                failure_stage=failure_stage,
                error_repr=error_repr,
                psm_broker_status=psm_broker_status,
                filled_price_parsed=filled_price_parsed,
                cntr_matches=cntr_matches,
                oso_matches=oso_matches,
                cntr_match_count=len(cntr_matches),
                oso_match_count=len(oso_matches),
                base_quantity_before_order=base_qty,
                target_quantity_after_order=target_qty,
                journal_linked=journal_linked,
                order_accepted_at=order_accepted_at,
                write_id=query_id,
            )
            recorder.record(observation)
        except Exception as exc:
            self.app_logger.critical(
                f"[ORDER_STATUS_OBS_RECORD_FAILED] {symbol} | query_id={query_id} — "
                f"관측 기록 실패(매매/리스크 판정에는 영향 없음): {type(exc).__name__}: {exc}"
            )

    def _reconcile_tracked_order_status(self, symbol: str, broker_qty: int) -> None:
        """1P0.8-D.1: Tracked Order Reconciliation (read-only).

        FILLED + 잔고 일치일 때만 PSM 전환을 앞당깁니다. OPEN/UNKNOWN/조회 실패는
        아무것도 바꾸지 않습니다. 주문·취소·재주문은 하지 않습니다.
        """
        state = self._position_state_machine.get(symbol)
        lifecycle = state.lifecycle
        kind: str | None = None
        order_id: str | None = None
        observation_pending_age_sec = 0.0
        if lifecycle == PositionLifecycle.BUY_PENDING:
            kind = "BUY_PENDING"
            order_id = state.pending_order_id
            age_sec = (
                (datetime.now() - state.pending_since).total_seconds()
                if state.pending_since else 0.0
            )
            observation_pending_age_sec = age_sec
            if age_sec < self.ORDER_STATUS_QUERY_MIN_PENDING_AGE_SEC:
                return
        elif lifecycle == PositionLifecycle.SELL_PENDING:
            kind = "SELL_PENDING"
            order_id = state.pending_order_id
            age_sec = (
                (datetime.now() - state.pending_since).total_seconds()
                if state.pending_since else 0.0
            )
            observation_pending_age_sec = age_sec
            if age_sec < self.ORDER_STATUS_QUERY_MIN_PENDING_AGE_SEC:
                return
        elif self._position_state_machine.has_orphan_order(symbol):
            kind = "ORPHAN"
            order_id = state.orphan_order_id
            orphan_since = getattr(state, "orphan_since", None)
            observation_pending_age_sec = (
                (datetime.now() - orphan_since).total_seconds() if orphan_since else 0.0
            )
        else:
            return  # BUY_PENDING/SELL_PENDING/orphan 아님 — 추적 대상 아님

        if not is_trackable_order_id(order_id):
            return  # 실제 브로커 주문번호가 아님 — 조회하지 않음(429 절약)

        now = datetime.now()
        last_at = self._last_order_status_query_at.get(symbol)
        if last_at is not None and (
            (now - last_at).total_seconds() < self.ORDER_STATUS_QUERY_MIN_INTERVAL_SEC
        ):
            return
        self._last_order_status_query_at[symbol] = now

        query_id = uuid.uuid4().hex
        query_started_at_iso = datetime.now().isoformat()
        try:
            evidence = self.broker.get_order_status_evidence(order_id, symbol)
        except Exception as exc:
            self.app_logger.warning(
                f"[ORDER_STATUS_QUERY_FAILED] {symbol} | kind={kind} | "
                f"order_id={order_id} | {type(exc).__name__}: {exc} — "
                f"기존 lifecycle 그대로 유지, 아무것도 해제하지 않음"
            )
            failure_stage = getattr(exc, "failure_stage", None) or "get_order_status_evidence"
            partial_oso = getattr(exc, "oso_entries", None)
            partial_cntr = getattr(exc, "cntr_entries", None)
            self._safe_record_order_status_observation(
                symbol=symbol, order_id=order_id, kind=kind,
                query_id=query_id, started_at_iso=query_started_at_iso,
                pending_age_sec=observation_pending_age_sec,
                outcome="api_error", failure_stage=failure_stage,
                error_repr=f"{type(exc).__name__}: {exc}", evidence=None,
                partial_oso_entries=partial_oso, partial_cntr_entries=partial_cntr,
            )
            return

        broker_order = evidence.broker_order
        self._safe_record_order_status_observation(
            symbol=symbol, order_id=order_id, kind=kind,
            query_id=query_id, started_at_iso=query_started_at_iso,
            pending_age_sec=observation_pending_age_sec,
            outcome="success", failure_stage=None, error_repr=None,
            evidence=evidence,
        )

        if broker_order.status == BrokerOrderStatus.OPEN:
            return  # 살아있다는 증거일 뿐 — 상태 변경 없음
        if broker_order.status == BrokerOrderStatus.UNKNOWN:
            return  # 아무것도 확정할 수 없음 — 상태 변경 없음
        if broker_order.status != BrokerOrderStatus.FILLED:
            self.app_logger.warning(
                f"[ORDER_STATUS_UNSUPPORTED] {symbol} | kind={kind} | "
                f"order_id={order_id} | status={broker_order.status} — "
                f"미지원 주문 상태이므로 lifecycle 유지"
            )
            return

        if kind == "BUY_PENDING":
            expected = state.expected_final_quantity
            if broker_qty == expected:
                self._position_state_machine.confirm_buy_from_broker(symbol, broker_qty)
            else:
                self.app_logger.warning(
                    f"[ORDER_STATUS_BALANCE_MISMATCH] {symbol} | kind=BUY_PENDING | "
                    f"order_id={order_id} | order_status=FILLED | "
                    f"broker_qty={broker_qty} | expected={expected} — "
                    f"잔고 API 반영 지연일 수 있음, 상태 유지"
                )
        elif kind == "SELL_PENDING":
            if broker_qty == 0:
                self._position_state_machine.on_sell_result(
                    symbol, accepted=True, broker_quantity=0)
            else:
                self.app_logger.warning(
                    f"[ORDER_STATUS_BALANCE_MISMATCH] {symbol} | kind=SELL_PENDING | "
                    f"order_id={order_id} | order_status=FILLED | "
                    f"broker_qty={broker_qty} | expected=0 — "
                    f"잔고 API 반영 지연일 수 있음, 상태 유지"
                )
        elif kind == "ORPHAN":
            target = (0 if state.orphan_expected_delta < 0
                      else state.expected_final_quantity)
            matched = (
                (state.orphan_expected_delta < 0 and broker_qty == 0)
                or (state.orphan_expected_delta > 0
                    and broker_qty == state.expected_final_quantity)
            )
            if matched:
                note = self._position_state_machine.observe_for_orphan(symbol, broker_qty)
                if note:
                    self.app_logger.warning(
                        f"[LIFECYCLE_ORPHAN][ORDER_STATUS_CONFIRMED] {symbol} | {note}"
                    )
            else:
                self.app_logger.warning(
                    f"[ORDER_STATUS_BALANCE_MISMATCH] {symbol} | kind=ORPHAN | "
                    f"order_id={order_id} | order_status=FILLED | "
                    f"broker_qty={broker_qty} | target={target} — "
                    f"잔고 API 반영 지연일 수 있음, orphan 유지"
                )

    # ══════════════════════════════════════════════════════════════
    # 사람 확인 파일 명령 (폴더 경로 인자화 + 8-A: BOM 허용·입력 검증·처리 파일 보관)
    # ══════════════════════════════════════════════════════════════

    def _process_pending_ack_error_commands(self) -> None:
        """commands/ack_error_{symbol}.json 파일로 ERROR 상태를 정정합니다.

        사용법 (PowerShell):
            $body = @{ broker_quantity = 100; note = "HTS 직접 확인" } | ConvertTo-Json
            $body | Out-File -Encoding utf8 commands\\ack_error_475150.json
        처리 후 파일은 삭제하지 않고 commands/processed/ 또는 commands/failed/로
        옮깁니다(실패 시 사유는 같은 이름의 .error.txt). BOM 있는 UTF-8도 읽습니다.
        """
        commands_dir = self._commands_dir
        if not commands_dir.is_dir():
            return
        for cmd_file in sorted(commands_dir.glob("ack_error_*.json")):
            symbol = cmd_file.stem[len("ack_error_"):]
            try:
                payload = _read_command_json(cmd_file)
                broker_quantity = payload.get("broker_quantity")
                if type(broker_quantity) is not int or broker_quantity < 0:
                    raise ValueError(f"broker_quantity는 0 이상 정수여야 합니다: {broker_quantity!r}")
                raw_note = payload.get("note")
                if not isinstance(raw_note, str) or not raw_note.strip():
                    raise ValueError("note는 비어 있지 않은 문자열이어야 합니다")
                note = raw_note.strip()
                self._position_state_machine.acknowledge_error(symbol, broker_quantity, note)
                self.app_logger.warning(
                    f"[ACK_ERROR_COMMAND] {symbol} | 파일 명령으로 ERROR 해제 — "
                    f"broker_quantity={broker_quantity}, note={note!r}"
                )
            except Exception as exc:
                dest = _archive_command(cmd_file, ok=False, error=f"{type(exc).__name__}: {exc}")
                self.app_logger.error(
                    f"[ACK_ERROR_COMMAND] {symbol} | 명령 파일 처리 실패, 상태 변경 없음: {exc} — "
                    f"원본은 {dest}로 옮김. 파일을 고쳐 commands/에 다시 넣으세요"
                )
            else:
                _archive_command(cmd_file, ok=True)

    def _process_pending_ack_orphan_commands(self) -> None:
        """commands/ack_orphan_{symbol}.json 파일로 orphan을 해제합니다.

        payload: {"note": "HTS에서 미체결 없음 확인"} (비어 있지 않은 문자열 필수)
        해제 시 이 종목의 보류 중인 매수/매도 부작용 컨텍스트도 함께 버립니다
        (원본과 동일 — 훅은 호출되지 않음).
        """
        commands_dir = self._commands_dir
        if not commands_dir.is_dir():
            return
        for cmd_file in sorted(commands_dir.glob("ack_orphan_*.json")):
            symbol = cmd_file.stem[len("ack_orphan_"):]
            try:
                payload = _read_command_json(cmd_file)
                raw_note = payload.get("note")
                if not isinstance(raw_note, str) or not raw_note.strip():
                    raise ValueError("note는 비어 있지 않은 문자열이어야 합니다")
                note = raw_note.strip()
                if not self._position_state_machine.has_orphan_order(symbol):
                    raise ValueError(
                        f"{symbol}: 현재 orphan 주문이 없어 ack_orphan을 적용할 수 없습니다"
                    )
                self._position_state_machine.acknowledge_orphan(symbol, note)
                had_buy_ctx = symbol in self._pending_buy_side_effects
                had_sell_ctx = symbol in self._pending_sell_side_effects
                self._pending_buy_side_effects.pop(symbol, None)
                self._pending_sell_side_effects.pop(symbol, None)
                self.app_logger.warning(
                    f"[ACK_ORPHAN_COMMAND] {symbol} | 파일 명령으로 orphan 해제 — "
                    f"note={note!r}, buy_ctx_cleared={had_buy_ctx}, "
                    f"sell_ctx_cleared={had_sell_ctx}"
                )
            except Exception as exc:
                dest = _archive_command(cmd_file, ok=False, error=f"{type(exc).__name__}: {exc}")
                self.app_logger.error(
                    f"[ACK_ORPHAN_COMMAND] {symbol} | 명령 파일 처리 실패, 상태 변경 없음: {exc} — "
                    f"원본은 {dest}로 옮김. 파일을 고쳐 commands/에 다시 넣으세요"
                )
            else:
                _archive_command(cmd_file, ok=True)


# ── 사람 확인 파일 명령 보관 (8-A, F7) ──────────────────────────────
def _read_command_json(path: Path) -> dict:
    """PowerShell 5.1 `Out-File -Encoding utf8`은 BOM을 붙이므로 utf-8-sig로 읽습니다."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 객체가 아님: {type(payload).__name__}")
    return payload


def _archive_command(path: Path, *, ok: bool, error: str = "") -> Path:
    """명령 파일을 processed/ 또는 failed/로 옮깁니다(삭제하지 않음 — 재현·감사용).

    옮기기 자체가 실패하면 같은 명령이 매 폴링마다 반복 처리되지 않도록 마지막
    수단으로 삭제합니다.
    """
    sub = path.parent / ("processed" if ok else "failed")
    stamp = now_kst().strftime("%Y%m%d_%H%M%S_%f")
    dest = sub / f"{path.stem}.{stamp}{path.suffix}"
    try:
        sub.mkdir(parents=True, exist_ok=True)
        path.replace(dest)
        if not ok:
            dest.with_suffix(".error.txt").write_text(error + "\n", encoding="utf-8")
        return dest
    except OSError:
        path.unlink(missing_ok=True)
        return Path("(삭제됨 — 보관 실패)")
