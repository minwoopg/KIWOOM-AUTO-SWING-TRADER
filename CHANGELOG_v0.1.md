# CHANGELOG v0.1 — 스윙 자동매매 분리

## 2026-09-28 — 1라운드: 단타 레포에서 매매 로직 제외 기반 코드 추출

### 배경
- 단타 레포(`minwoopg/kiwoom-auto-trader` `bdde6c2`)와 별도로 스윙 자동매매를 새 레포에서 시작.
- 확정 사항: 별도 계좌 / 새 레포 / 장 시작 전 기동·장 마감 후 종료 / 주문·매매 로직은 이후 스윙식으로 전환.
- 1단계 조사 보고서(`swing-auto-trader/2026-09-28-stage1-reuse-inventory.md`)의 분류를 따름.
- 이번 라운드는 "매매 로직 제외 재사용 기반"만. `TradingService`의 주문 실행부 추출은 다음 라운드.

### 변경 내용
| 구분 | 파일 |
|---|---|
| 원본 그대로 (코드·설정 24개 + 테스트 5개) | 브로커 6종, `indicator/__init__.py`, `position/__init__.py`, `lifecycle.py`, `tracked_order_journal.py`, `order_status_observation_store.py`, `state_store.py`, `run_baseline.py`, `process_lock.py`, `models.py`, `cost_model.py`, `pnl_calculator.py`, `exit_calc.py`, `indicators.py`, `time_utils.py`, `trade_outcome.py`, `requirements.txt`, `.gitignore`, `run_regression_tests.py` |
| 수정 | `config/settings.py`(App/Broker/Storage/Kakao만), `config/settings.yaml`(스윙 계좌 라벨 `acct-swing`, `use_mock: true`), `infra/storage/logger.py`(app/trades/position_lifecycle 로거만, `TRADE_FIELDS` 분봉 컬럼 제거), `infra/notify/kakao_notifier.py`(시작 알림 제목·감시줄 지정 옵션 — 기본값은 원본 문구 그대로) |
| 새로 작성 | `app/main.py`(기반 점검 모드), `testing_helpers.py`, `test_app_startup.py`, `test_extraction_boundary.py`, `provenance.json`, `README.md`, `.env.example` |
| 가져오지 않음 | `TradingService`, 전략 5종, `market_regime/*`, `risk_manager`, `state_reconciler`, `daily_reporter`, `minute_bar_saver`, `websocket/*`, `export_daily_bundle.py`, shadow 로거·`skip_reason`·`shadow_signature` |

### 테스트 및 검증
- `run_regression_tests.py`: 12개 파일 중 11개 통과.
  실패 1개 `test_broker_order_status.py`는 실측 fixture(`tests/fixtures/order_reconciliation/`)가 git에 없어서이며 단타 레포에서도 동일하게 실패함.
- 원본 테스트 이관: 브로커 4종(224건) 원본 그대로 통과, `test_run_baseline`(29건), `test_kakao_*`(27+21건), `test_cost_model`(42건), `test_tracked_order_journal_store`(원본 1~5절 30건 — 원본 레포의 같은 구간 PASS 30건과 일치).
- `test_extraction_boundary.py`: 단타 매매 모듈 import 없음, `unchanged` 29개 파일 해시가 원본과 일치, 설정에 단타 섹션 없음.
- `python -m app.main`(MockBroker) 실행 → 정상 종료(exit 0), 같은 락을 잡은 상태에서 두 번째 실행은 중복 실행 차단(exit 1).

### 변경하지 않은 것
- `domain/models.py`의 `RuntimeState`(단타 당일 기준 필드) — `state_store.py`와 주문 의도 기록(`unresolved_order_intents`)이 의존하므로 스윙 상태 모델 설계 시 함께 교체.
- `pnl_calculator.py`의 "종목당 이월 매도 1회" fail-close — 스윙 분할청산과 충돌하지만 손익 원장 설계 라운드에서 다룸.
- `time_utils.py` 휴장일 미반영, `kiwoom_broker.py` 토큰 갱신·취소 주문 없음 — 운영 방식(매일 기동·시장가)상 당장 필요 없음.
- `run_baseline.py` 첫 실행 시 `[RUN_BASELINE] 헤더 확인 실패` 경고 1회 — 원본과 동일한 기존 동작(파일 없을 때 마이그레이션 건너뜀), 기록 자체는 정상.

### 다음 작업
1. `TradingService`에서 주문 실행부 추출 → `OrderExecutor` (주문 의도 기록 → PSM → 전송 → 저널 → 체결 조회 → 첫 체결/완전 청산 부작용 훅).
2. `test_partial_fill_lifecycle.py`·`test_order_status_reconciliation.py`·`test_tracked_order_journal.py` 6~12절을 `OrderExecutor` 기준으로 재작성.
3. 카카오 notifier 인스턴스 공유 검증(원본 `TestSharedNotifierBetweenTradingServiceAndStartup`)을 새 기동 경로 기준으로 재추가.

### 전달 파일
- `swing-auto-trader.zip` (레포 전체, `.git` 제외)

---

## 2026-09-28 — 2라운드: 주문 실행부 추출 (`OrderExecutor`)

### 배경
- 1라운드 다음 작업 1·2번. 단타 `TradingService`(6,283줄) 안에 묶여 있던 주문 실행·추적·복구 로직을 매매 판단 없이 떼어냄.
- 카카오 알림은 당분간 사용하지 않음(.env의 `KAKAO_*`를 비워두면 자동 비활성 — 코드 변경 없음).

### 변경 내용
- `domain/service/order_executor.py` 신설 (원본 `trading_service.py` 해당 메서드를 판정 로직 그대로 옮김)

| 새 API | 원본 |
|---|---|
| `submit_buy(symbol, qty, reference_price, context=)` | `_try_buy`의 주문 의도 기록 이후 |
| `submit_sell(symbol, qty, reference_price, exit_reason=, avg_buy_price=, forced=, context=)` | `_try_sell` + `_try_sell_unchecked` |
| `sync_with_balance(balance, watch_symbols)` | `_sync_position_state_machine_shadow` |
| `reconcile_after_market_close(balance)` | `reconcile_after_market_close` |
| `restore_order_recovery_blocks()` (생성 시 자동) | `_restore_order_recovery_blocks` |
| `has_unresolved_orders()` | `_has_unresolved_orders` |
| `on_first_fill_buy` / `on_sell_closed` 훅 | `_apply_first_fill_buy_side_effects` / `_apply_deferred_sell_side_effects`의 호출 시점 |
| 그대로 이전 | 주문 의도 기록, 저널 생성·유지, 체결 조회(폴링당 1건·나이 게이트·30초 간격), 관측 기록, `commands/ack_error_*`·`ack_orphan_*` |

- 원본과 의도적으로 다른 점
  1. 진입 게이트(14:50·쿨다운·진입횟수·리스크·시세 신선도)는 넣지 않음 — 호출부 책임. 남긴 차단은 주문 안전 게이트 4종(복구 불가 / PSM 차단 / 계좌 내 미해결 주문 / 의도 기록 실패)뿐.
  2. 강제 매도는 `forced=True` 인자로만 판단(원본은 사유 문자열의 "손절"/"강제청산" 키워드).
  3. 첫 체결·청산 부작용 내용(진입시각·손실카운트·쿨다운·알림)은 단타 규칙이라 제외, 훅으로 시점만 제공. 훅 예외는 CRITICAL 후 삼킴(신규 규칙).
  4. 잔고 캐시 무효화·`_sold_today`는 호출부 책임. 중복 매도는 PSM SELL_PENDING HARD block이 막음.
  5. `commands/` 경로를 생성자 인자로 받음(기본값 동일).
  6. 매도 결과도 `last_order_attempt()`로 조회 가능(원본은 매수만 기록 — 판정에 쓰이지 않는 조회용).
  7. 영구 거부 키워드(`RC4007` 등)는 `is_permanent_buy_reject()`로 공개 — 재시도 차단 여부는 호출부가 결정.
- `test_order_executor.py` (98건): 원본 TradingService 통합 테스트의 사건 시나리오(047040 부분체결, 006360/017900 BUY_PENDING 중 SELL, 319400 ambiguous, 재시작 복원, 저널 손상, 체결 조회 게이트, ack 명령, 훅 예외, 저널 I/O 실패 등)를 OrderExecutor 기준으로 재작성.
- `test_position_lifecycle.py` (179건): 원본 `test_partial_fill_lifecycle.py`에서 PSM만으로 검증되는 문장을 자동 추출(원본 336건 중 179건, 남은 문장은 원본과 동일 — 원본 PASS 목록의 부분집합임을 대조 확인). 제외 위치에는 `[스윙 분리: 원본 Lxx 제외]` 주석.
- `tools/equivalence/` : 단타 `TradingService`와 `OrderExecutor`에 같은 시나리오를 흘려 매 단계 상태를 비교하는 도구. `python tools/equivalence/compare.py --orig ..\KIWOOM-AUTO-TRADER`
- `test_extraction_boundary.py`: `tools/equivalence/`를 import 검사에서 제외(단타 레포 안에서 원본을 실행하는 도구이므로).
- `provenance.json`: 신규·파생 파일 등록.

### 테스트 및 검증
- `run_regression_tests.py`: 14개 파일 중 13개 통과 (실패 1개는 1라운드와 동일 — `test_broker_order_status.py` 실측 fixture 없음).
- 동등성 비교: 18개 시나리오 **18/18 동일** (단타 레포 `bdde6c2` 대비). 비교 항목: PSM 종목별 상태, 주문 의도, 저널, 보류 컨텍스트, 브로커 호출 순서(주문·체결 조회), 훅 호출, 복구 실패 플래그.
  - 도구 자체 검증: `_select_order_status_query_target`의 선택 기준을 일부러 뒤집으면 `status_budget_two_symbols`가 DIFF로 잡힘을 확인.
- 1라운드 `unchanged` 29개 파일 해시 그대로 유지.

### 변경하지 않은 것
- `app/main.py`는 기반 점검 모드 그대로 — `OrderExecutor` 연결은 스윙 매매 루프 라운드에서.
- `domain/position/lifecycle.py` 등 원본 그대로 가져온 파일 전부.
- 원본 `test_order_status_evidence_observation.py`의 저장소 단위 부분(기록기 꼬리 손상 복구·커버리지 집계)은 `export_daily_bundle` 결합이 깊어 이관하지 않음 — `order_status_observation_store.py`는 원본과 바이트 동일.

### 다음 작업
1. 스윙 상태 모델(포지션별 진입일·손절가·전략 ID) 설계 → `RuntimeState` 교체.
2. 여러 날 보유 손익 원장 (`pnl_calculator`의 이월 매도 1회 제한 해소).
3. 거래일 캘린더, 일봉 수집·완성 봉 판정.

### 전달 파일
- 패치 0001 (코드 + 테스트 + 도구), 0002 (CHANGELOG/README)

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
