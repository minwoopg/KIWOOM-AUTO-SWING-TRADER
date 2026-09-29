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

---

## 2026-09-28 — 3라운드: 거래일 캘린더 + 조회 전용 실측 프로브

### 배경
- 뼈대 작업 1·2번. 매매 로직은 다루지 않음.
- 단타 `time_utils.is_market_open()`은 평일 09:00~15:20만 보고 휴장일을 모름 → "N거래일 보유", "직전 거래일 완성 봉" 계산 불가.
- 일봉 당일 미완성 봉 포함 여부, 체결조회 범위는 지금까지 추정만 있었음.

### 변경 내용
- `config/krx_calendar.yaml`: 2026년 KRX 평일 휴장일 17일 + 특수 운영일(1/2 10시 개장). 출처 주석 포함.
  - 확인된 2026년 특이사항: 7/17 제헌절 재지정 휴장, 6/3 지방선거, 5/25·10/5 대체공휴일 휴장, 9/28은 정상 개장(추석 토요일 겹침은 대체공휴일 아님).
- `utils/trading_calendar.py` (`TradingCalendar`)
  - `is_trading_day` / `next_trading_day` / `previous_trading_day` / `add_trading_days` / `trading_days_between`(보유 거래일수) / `trading_days_in_range`
  - `phase(now)`: CLOSED_DAY / PRE_OPEN / REGULAR / CLOSING_AUCTION(15:20~) / POST_CLOSE
  - `last_completed_session(now)`: 정규장이 끝난 마지막 거래일 — 일봉 판정 기준일
  - `compare_with_bar_dates()`: 실제 일봉 날짜와 캘린더 대조
  - fail-closed: 파일 없음·형식 오류·주말을 휴장일로 적음·다루지 않는 연도 조회 → 예외 (다음 해를 "평일이니 개장"으로 추측하지 않음)
- `tools/probe_market_data.py`: 조회 전용 프로브 (모의투자 도메인만, 주문 API 호출 불가, 계좌·토큰 값 가림)
  - A. 일봉(ka10081) 당일 미완성 봉 포함 여부·페이지 크기·연속조회
  - B. 일봉 날짜 ↔ 캘린더 대조
  - C. 미체결(ka10075)·체결(ka10076)이 이전 거래일 주문을 보여주는지
- `utils/time_utils.py`는 원본 그대로 유지 (provenance `unchanged`).

### 테스트 및 검증
- `test_trading_calendar.py` 42건, `test_probe_market_data.py` 22건 (가짜 세션 — 네트워크 없음, 주문 API 미호출·비밀값 미기록 확인 포함).
- `run_regression_tests.py`: 16개 파일 중 15개 통과 (실패 1개는 기존과 동일 — 실측 fixture 없음).

### 변경하지 않은 것
- 캘린더는 2026년만 다룸. 2027년 목록은 거래소 공지 후 추가. 2026-01-02에 `previous_trading_day`를 부르면 2025년이 없어 예외가 남(의도된 fail-closed).
- 수능일 개장 지연은 공지 후 추가.

### 다음 작업
- 프로브 실행 결과를 보고 일봉 데이터 계층 설계 확정 (완성 봉 판정 규칙, 페이지 수, 저장 형식).
- 4라운드: 스윙 상태 모델 + 여러 날 보유 원장.

### 전달 파일
- 패치 0001 (캘린더·프로브·테스트), 0002 (CHANGELOG/README)

---

## 2026-09-28 — 4라운드: 스윙 상태 모델 + 여러 날 보유 원장

### 배경
- 뼈대 작업 3·4번. 매매 로직(손절가를 어떻게 정하는지 등)은 다루지 않음 — 담을 그릇만 만듦.
- 단타 `RuntimeState`는 당일 기준 필드뿐이라 여러 날 보유를 담지 못함.
- 단타 `pnl_calculator`는 이월 매도를 종목당 1회만 인정 → 스윙 분할 청산 시 계산 거부·신규매수 차단. 입력도 주문가 기준.
- 실측 프로브 1회차(장중) 결과는 프로젝트 문서 `2026-09-28-probe-result-1-intraday.md` — 당일 미완성 봉 포함, 600행/페이지, 연속조회 가능, 0.5초 간격 5회째 429, 2026-01~09 캘린더 일치.

### 변경 내용
- **한 사실은 한 곳에만** 원칙으로 역할 분리

| 사실 | 원천 |
|---|---|
| 보유 수량·매입단가·진입 거래일 | 체결 원장 `data/fill_ledger.jsonl` |
| 주문 진행 상태 | PSM(메모리) + 주문 저널 |
| 재시작 시 "보냈는지 모르는 주문" | `SwingState.unresolved_order_intents` |
| 전략 ID·손절가 등 메타 | `SwingState.positions` (`PositionMeta`) — 수량·가격은 저장하지 않음 |

- `domain/position/swing_state.py`: `SwingState`, `PositionMeta` (origin ORDER/ADOPTED, needs_review, meta dict). 형식 검증 엄격(알 수 없는 필드·잘못된 손절가 거부).
- `infra/storage/swing_state_store.py`: 원자적 저장, schema_version 검사, 손상·단타 형식 파일은 `SwingStateCorruptError` (단타 저장소는 검증 없이 읽었음). `OrderExecutor`와 호출 형태 호환.
- `domain/position/fill_event.py` + `infra/storage/fill_ledger.py`: append-only 체결 원장. 가격 출처 필수(BROKER_FILL / BROKER_AVG / ORDER_ESTIMATE). 같은 사건 재기록은 무시, 같은 id·다른 내용은 예외. 쓰다가 끊긴 마지막 줄만 격리하고 그 외 손상은 예외.
- `domain/service/lot_ledger.py`: 원장 전체를 시간순 FIFO 적용 — 분할 청산 허용, 매도 > 보유면 `LotMatchError`(fail-close 유지), 추정가 포함 여부 표시, 평가손익, 비용 시나리오별 순손익.
- `domain/position/position_book.py`: 원장·잔고·메타 대조 **보고만** (QTY_MISMATCH / UNTRACKED_HOLDING / LEDGER_ONLY는 blocking, 주문 진행 중이면 참고로 강등). 기존 보유분 인수용 `opening_events_from_balance()` 도우미(자동 적용 안 함).
- `app/main.py` 기동 점검: 상태 파일을 `SwingStateStore`로 읽고, 체결 원장 로드 + 장부 대조 결과를 로그·화면에 표시. 실전투자에서 원장 손상·단타 형식 state.json이면 시작 중단.
- `config`: `storage.fill_ledger_file` 추가. `testing_helpers.py`: `ScriptedBroker`를 여러 테스트가 공유하도록 이동.
- `tools/equivalence/runner.py`: 새 구현 쪽 상태 저장소를 `SwingStateStore`로 교체.

### 테스트 및 검증
- 신규: `test_swing_state.py` 27건(OrderExecutor와 함께 재시작 복원 포함), `test_fill_ledger.py` 37건, `test_position_book.py` 14건, `test_app_startup.py` 16→24건.
- `run_regression_tests.py`: 19개 파일 중 18개 통과 (실패 1개는 기존과 동일 — 실측 fixture 없음).
- 동등성 비교: SwingStateStore로 바꾼 뒤에도 18/18 동일.
- `python -m app.main`(MockBroker): 정상 종료, "장부 대조: 일치".

### 변경하지 않은 것
- `domain/models.py`의 `RuntimeState`, `infra/storage/state_store.py`는 원본 그대로 남김(동등성 도구의 원본 쪽·기존 테스트가 사용). 새 코드는 쓰지 않음.
- 원장 자동 기록(체결 확인 → FillEvent 추가)은 아직 연결하지 않음 — 매매 루프 연결 라운드에서 `OrderExecutor` 훅·잔고 변화·체결조회 증거로 기록.
- 원장과 잔고가 어긋나도 자동으로 메우지 않음 (의도).

### 다음 작업
- 5라운드: 일봉 데이터 계층 (완성 봉만 제공, 1초 간격 + 429 백오프, 수정주가 변경 감지 시 재수집).
- 남은 실측: 장 마감 후 당일 봉 확정, 장 시작 전 체결조회 범위.

### 전달 파일
- 패치 0001 (상태·원장·대조·기동 점검·테스트), 0002 (CHANGELOG/README)

---

## 2026-09-28 — 5라운드: 일봉 데이터 계층

### 배경
- 뼈대 작업 5번. 실측 프로브 장중 2회(12:46, 13:00) 결과가 동일하게 재현됨:
  당일 미완성 봉 포함(종가 칸 = 현재가) / 600행/페이지 / 연속조회 동작 / 0.5초 간격 5번째 호출에서 HTTP 429 / 2026-01~09 캘린더 일치.
- 단타 `get_daily_prices()`는 파싱 실패 값을 0으로 채우고, 1페이지·오늘 기준만 조회하며, 미완성 봉을 거르지 않음.

### 변경 내용
- `domain/market_data/daily_bar.py`: `DailyBar`(날짜 `date`, 가격 관계 검증) + 엄격 파서 — 숫자 아님·빈 값·날짜 오류·고가<종가 등은 예외(0으로 채우지 않음). 기존 `PriceBar` 변환 제공.
- `infra/market_data/daily_bar_source.py`
  - `KiwoomDailyBarSource`: ka10081 1페이지 조회(수정주가). 원본 `KiwoomBroker._post()` 재사용. HTTP 429 → 재시도 대상, 전송 실패 → 재시도 대상, 그 외 HTTP·업무 오류 → 즉시 실패.
  - `PacedFetcher`: 모든 조회의 최소 간격(기본 1초) + 재시도 대기(2/5/10/20초, 상한 4회).
- `infra/market_data/daily_bar_store.py`: 종목별 CSV + meta(JSON) 원자적 저장. meta 행 수·날짜 범위와 CSV 불일치, 완성 기준일 이후 봉, 헤더 불일치 → 손상 오류.
- `infra/market_data/daily_bar_repository.py`
  - 완성 봉만: `last_completed_session(now)` 이후 행은 받자마자 버림, 조회(`completed_bars`)도 같은 기준으로 한 번 더 자름.
  - 증분: 로컬이 있으면 1페이지만.
  - 겹치는 구간 값이 하나라도 다르거나 로컬 날짜가 응답에서 빠지면 → 수정주가 재계산으로 보고 전체 재수집(기존 깊이까지).
  - 1페이지가 로컬 마지막 날짜에 닿지 않으면(공백) → 전체 재수집. 로컬 손상 → 전체 재수집.
  - 캘린더상 거래일인데 봉이 없는 날은 `missing_sessions`로 보고만(거래정지 가능). 마지막 봉이 기준일이 아니면 `is_current=False`.
  - 이상한 행이 하나라도 있으면 그 종목은 저장하지 않고 FAILED.
- `config`: `market_data` 설정(간격 1초 — 0.5초 미만 설정 거부, 재시도 대기, backfill_pages=3 ≈ 7년).
- `tools/update_daily_bars.py`: 종목 지정 수집·갱신 CLI (조회 전용, `use_mock`과 무관하게 설정의 base_url로 조회).

### 테스트 및 검증
- `test_daily_bars.py` 46건 (가짜 소스·가짜 시계 — 네트워크·실제 대기 없음).
  - 변형 검증: 겹침 비교를 끄면 5-1·5-2·5-4 실패, 미완성 봉 거르기를 끄면 3-1~3-3 실패 확인.
- `run_regression_tests.py`: 20개 파일 중 19개 통과 (실패 1개는 기존과 동일 — 실측 fixture 없음). 동등성 18/18.

### 변경하지 않은 것
- `KiwoomBroker.get_daily_prices()`는 원본 그대로(쓰지 않음).
- 거래정지일 봉의 실제 응답 형태는 미확인 — 시가·고가·저가가 0으로 오면 현재 파서는 그 종목을 FAILED 처리함(추측으로 보정하지 않음). 실제 사례가 나오면 규칙 추가.
- 요청 `base_dt`는 실측된 형태(오늘 날짜)만 사용.

### 다음 작업
- 6라운드: 실행 수명주기(`app/main.py` 기동 → 대조 → 장중 루프[전략 자리 비움] → 마감 후 대조 → 종료) + 계좌 안전 한도.
- 남은 실측: 장 마감 후 당일 봉 확정(15:30 이후), 장 시작 전 체결조회 범위.

### 전달 파일
- 패치 0001 (일봉 계층·설정·CLI·테스트), 0002 (CHANGELOG/README)

---

## 2026-09-28 — 6라운드: 하루 수명주기 + 계좌 안전 한도 + 체결 원장 자동 기록

### 배경
- 뼈대 작업 6·7번. 전략 자리는 비워둠(NullStrategy) — 매매 판단 없음.
- 5라운드 실사용 확인: `update_daily_bars.py 005930 000660` → 각 1799행(당일 미완성 봉 제외)·9/23까지, 호출 7회 중 1초 간격에서도 429 1회 → 재시도로 복구.

### 변경 내용
- `app/session_runner.py` (`SessionRunner`): 프로세스 1회 = 거래일 하루
  - CLOSED_DAY 즉시 종료 / PRE_OPEN 대기 / 장중 `poll_interval_sec`(기본 60초)마다 tick / 마감 후 미해결 주문이 없어질 때까지(최대 15:45) 대조만 → 요약 → 종료
  - tick 순서: 잔고 → `OrderExecutor.sync_with_balance` → 체결 원장 기록 → 끝난 주문 추적 종료 → 장부 대조(원장·잔고·메타) → 전략 `on_tick` → 안전 한도 → 주문
  - 잔고 조회 실패 폴링은 주문 없음(연속 5회부터 CRITICAL). 전략 예외는 CRITICAL 후 계속. 원장 손상·기록 실패 → 이번 프로세스 신규 주문 중단(대조는 계속, 프로세스는 끝까지 정상 종료).
  - 청산 완료(원장·잔고 보유 없음, 진행 중 주문 없음) 종목의 포지션 메타 자동 정리. 매수 접수 시 메타 생성(전략 ID).
- `domain/strategy/interface.py`: `OrderIntent`(시장가, forced는 매도만), `TickContext`(읽기 전용 스냅샷), `Strategy` 프로토콜, `NullStrategy`.
- `domain/risk/account_guard.py` (`check_intent`, 순수 함수): 신규 주문 시간대(09:05~15:15, 강제 매도 예외) / 장부 불일치·검토 필요 종목 차단(강제 매도 포함) / 1회 주문 금액 / 최대 보유 종목 수 / 총 노출 / 현금 버퍼 / 매도 ≤ 원장 보유 / 허용 종목 목록.
- `domain/service/fill_recorder.py` (`FillRecorder`): 접수된 주문만 추적해 잔고 변화로 BUY/SELL 사건 기록. 매수 원가는 잔고 평균단가 역산(BROKER_AVG), 매도는 주문가 추정(ORDER_ESTIMATE). event_id = 주문번호:누적체결 → 중복 기록 없음. 추적 안 한 수량 변화(HTS 수동 매매)는 기록하지 않고 대조에서 드러남.
- `app/main.py`: 기동 점검 후 하루 수명주기 실행. `--check-only`로 이전 동작(점검만). 마감 후 일봉 갱신은 설정으로 선택(`session.update_daily_bars_after_close`).
- `config`: `session`(폴링 간격 10초 미만 거부, 마감 후 대조 한계, 감시 종목), `guard`(한도 값) 섹션.

### 테스트 및 검증
- `test_account_guard.py` 26건, `test_fill_recorder.py` 14건, `test_session_runner.py` 30건 (가짜 시계로 하루 전체: 휴장일 / 전략 없는 하루 391회 폴링 / 매수→체결 기록→매도→청산·메타 정리 / 다음 날 재기동 대조 일치 / 미체결로 15:45까지 대기 / 잔고 장애 / 전략 예외 / 중지 요청 / HTS 수동 보유 종목 차단 / 원장 손상 시 중단).
  - 테스트 중 발견·수정: 원장이 손상된 상태에서 추적 주문이 체결되면 기록 단계 예외로 러너 전체가 죽음 → 신규 주문 중단으로 처리하도록 수정.
- `run_regression_tests.py`: 23개 파일 중 22개 통과 (실패 1개는 기존과 동일 — 실측 fixture 없음). 동등성 18/18.
- `python -m app.main --check-only` 정상, MockBroker + 가짜 시계 하루 실행 정상(391회 폴링, 대조 일치).

### 변경하지 않은 것
- 매매 판단 없음 — `NullStrategy`는 주문을 내지 않음.
- 매도 체결가는 주문가 추정으로만 기록(체결조회 증거로 실제 체결가를 붙이는 것은 이후 과제).
- 체결 추적 정보는 메모리 전용 — 주문 도중 재시작하면 OrderExecutor가 ERROR로 복원하고 기동 대조에서 불일치로 보고됨(사람 확인 원칙 유지).

### 운영 주의
- `python -m app.main`은 이제 **장 마감(15:30~15:45)까지 실행**됩니다. 점검만 하려면 `--check-only`.
- `broker.use_mock: true`면 MockBroker(즉시 체결 가짜 계좌)로 하루가 돌아갑니다. 스윙 모의계좌로 돌리려면 `false`.

### 다음 작업 (뼈대 마지막)
- 7라운드: 일일 리포트(보유·평가손익·실현손익·미해결 주문)·로그 번들, Windows 작업 스케줄러 등록 스크립트, GitHub Actions 회귀 테스트.

### 전달 파일
- 패치 0001 (수명주기·한도·기록기·main·설정·테스트), 0002 (CHANGELOG/README)

---

## 2026-09-28 — 7라운드: 일일 리포트·번들, 작업 스케줄러, GitHub Actions (뼈대 마지막)

### 배경
- 뼈대 작업 8·9·10번. 매매 로직 없음.

### 변경 내용
- **일일 리포트** `infra/reporting/daily_report.py` (순수 함수) + `app/reports.py`(파일 조립)
  - `reports/daily_report_<날짜>.md`: 요약(보유 수·원가·평가액·평가손익·당일/누적 실현, Base·Stress 비용 차감) / 보유 종목(수량·평균단가·첫 진입일·보유 거래일·종가·평가손익·손절가·전략) / 당일 체결(가격 출처) / 당일 실현 매칭(보유 거래일·추정 여부) / 미해결 주문 / 장부 대조 / 세션 요약
  - 평가는 **완성된 일봉 종가**만 사용(장중 가격 안 씀). 종가가 없으면 제외하고 안내.
  - 하루 수명주기 끝에 자동 생성(휴장일 제외, 실패해도 매매 결과에 영향 없음). `tools/daily_report.py [--date]`로 다시 생성.
- **번들** `tools/export_bundle.py [--date]` → `exports/swing_bundle_<날짜>.zip`
  - 그날 app.log(순환 파일 포함)·trades.csv·position_lifecycle.csv 행, 체결 원장, state.json, 주문 저널, 실행 기준선, 리포트, 체결조회 관측의 그날 줄 + manifest(해시·git SHA).
  - `infra/reporting/masking.py`: 단타 `export_daily_bundle.py`의 마스킹 코드를 그대로 가져옴(자격증명 누출 재현 후 보강된 코드). 텍스트는 정규식, JSON은 키 기준(구조·주문번호 보존).
- **작업 스케줄러** `scripts/register_task.ps1` / `run_swing.ps1` / `unregister_task.ps1`
  - 평일 08:40(변경 가능) 실행, 공휴일은 프로그램이 캘린더로 판단해 바로 종료. 중복 실행 무시, 최대 9시간, 배터리 모드에서도 실행.
  - 실행 출력은 `logs/scheduler/run_<시각>.log`, 종료 코드를 작업 스케줄러에 그대로 전달.
  - PowerShell 5.1이 한글을 읽도록 UTF-8 BOM으로 저장. 파이썬 stderr가 PowerShell 오류로 바뀌어 멈추지 않게 처리.
- **GitHub Actions** `.github/workflows/regression.yml`: main 푸시·PR마다 Windows·Ubuntu × Python 3.11·3.12 회귀 테스트. 실측 fixture 없는 `test_broker_order_status.py`만 명시적으로 건너뜀.
- `run_regression_tests.py`: `--skip FILE` 옵션 추가(provenance `modified`).
- `config`: `storage.reports_dir`. `.gitignore`: `reports/`, `exports/` (손익 등 개인 기록).

### 테스트 및 검증
- `test_daily_report.py` 27건 (리포트 본문 수치·표 행, 지난 날짜 평가, 마스킹, 번들 내용·날짜 필터·가림·manifest), `test_session_runner.py` 30→32건(하루 끝 리포트 생성, 휴장일 미생성).
- `run_regression_tests.py --skip test_broker_order_status.py`: **23개 전부 통과** (UTF-8 모드에서도 동일). 동등성 18/18.

### 확인하지 못한 것
- PowerShell 스크립트는 이 작업 환경에 PowerShell이 없어 실행 검증을 못 함 → 등록 후 `Start-ScheduledTask`로 한 번 확인 필요.
- GitHub Actions의 Windows 러너 결과는 첫 푸시 후 Actions 탭에서 확인 필요(로컬 검증은 Linux).

### 뼈대 완료 — 다음 단계
- 전략 설계(백테스트 엔진 포함)로 넘어갈 준비 완료. 남은 운영 확인: 장 마감 후 당일 봉 확정, 장 시작 전 체결조회 범위(프로브).

### 전달 파일
- 패치 0001 (리포트·번들·스케줄러·CI), 0002 (CHANGELOG/README)

## 2026-09-28 — 8-A: Windows CI 줄바꿈 고정, 수동 복구 명령 BOM·보관 (GPT 기반 검토 F8·F7)

### 배경
- GPT 기반 구조 검토(`5901a05` 기준)의 8건 중 운영 검증 기반 2건을 먼저 처리.
- F8: GitHub Actions Windows 두 환경만 실패 — `test_extraction_boundary`의 원본 바이트 해시 검사.
  `.gitattributes`가 없어 Windows(`core.autocrlf=true`) 체크아웃에서 LF→CRLF로 바뀜.
- F7: PowerShell 5.1 `Out-File -Encoding utf8`은 BOM을 붙이는데 명령 파일을 `utf-8`로 읽어
  파싱 실패 → ERROR 유지된 채 명령 파일이 삭제돼 원인 추적 불가.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `.gitattributes` (신규) | `* text=auto eol=lf` — OS와 무관하게 작업 폴더도 LF. zip/이미지는 binary |
| `domain/service/order_executor.py` | 명령 파일을 `utf-8-sig`로 읽음. JSON 객체 여부, `broker_quantity`(bool·문자열·음수 거부), `note`(공백 거부) 검증. 처리 후 삭제 대신 `commands/processed/` 또는 `commands/failed/`로 이동(시각 접미사), 실패 사유는 `.error.txt`. 이동 자체가 실패하면 반복 처리 방지를 위해 삭제 |
| `test_order_executor.py` | 4-10·15-5 기대값을 이동으로 변경, 18절(BOM+CRLF 정상 처리, 잘못된 입력 6종 → ERROR 유지·원문 보존, 사유 파일, 재처리 없음) 추가 — 98 → 109건 |
| `README.md` | 명령 파일 보관 위치 안내 |

### 테스트 및 검증
- F8 재현·해결: `core.autocrlf=true`로 클론 시 `.gitattributes` 없으면 88개 파일 CRLF → 해시 검사 실패(재현),
  추가 후 전 파일 LF → 통과. 실제 Windows runner 결과는 푸시 후 Actions에서 확인 필요.
- `run_regression_tests.py --skip test_broker_order_status.py`: 23개 전부 통과.
- 단타 원본 동등성 18/18 동일(명령 파일 처리 방식 변경은 상태 전이에 영향 없음).

### 변경하지 않은 것
- 해시 검사 자체는 그대로(제외하거나 해시를 덮어쓰지 않음).
- 명령의 의미(ERROR/orphan 해제 조건), 매매 로직, F1~F6.

### 다음 작업
- 8-B: F1 장부 대조를 종목·주문 단위로, F3 계좌 전체 노출 한도.

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

## 2026-09-29 — 8-B: 장부 대조 종목 단위화, 계좌 전체 노출 한도 (GPT 기반 검토 F1·F3)

### 배경
- F1: `reconcile(orders_in_flight=bool)`이 계좌 전체 하나의 값이라, B종목 주문이 미해결이면
  A종목 수량 불일치도 BLOCK에서 빠짐. 원장 100주·잔고 10주인 A의 100주 매도가 브로커까지 도달.
- F3: 총 노출을 원장 보유만 합산하고, 현재가는 주문 종목 하나만 넘겨 다른 보유는 원가로 평가.
  원장 밖 보유가 노출에서 빠지고, 보유 종목이 오르면 노출을 과소평가.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/position/position_book.py` | `reconcile(in_flight_symbols=...)` — 미해결 주문이 걸린 **그 종목**의 수량 차이만 INFO. `ReconcileReport.blocking_symbols`, `orders_in_flight`는 속성으로 유지 |
| `domain/service/order_executor.py` | `unresolved_symbols()` 추가 (의도 기록 ∪ PENDING ∪ orphan 종목). 기존 동작 변경 없음 |
| `domain/risk/account_guard.py` | 매도: 원장 수량과 **잔고 수량** 모두 확인(`SELL_EXCEEDS_BROKER`). 매수: 계좌 불일치 있으면 전 종목 보류(`ACCOUNT_RECONCILE_BLOCKED`), 금액 모르는 미해결 주문 있으면 보류(`PENDING_AMOUNT_UNKNOWN`), 보유 종목 현재가 누락 시 보류(`PRICE_UNKNOWN`), 노출 = Σ max(원장, 잔고) × 현재가 + 미체결 매수 예약금 + 주문×(1+여유%). `buy_price_buffer_pct`(기본 1%) 추가 |
| `infra/market_data/quote_source.py` (신규) | `KiwoomQuoteSource`(ka10001, 1초 간격, 실패 종목은 빠짐), `StaticQuoteSource`(테스트용) |
| `app/session_runner.py` | 대조를 종목 단위로, 매수 의도 시 보유 종목 현재가를 폴링당 1회 조회, 미체결 매수 예약금 계산, 불일치 종목 메타는 미해결 주문 종목만 정리 보류. `TickContext.in_flight_symbols` 추가 |
| `app/main.py`, `app/reports.py` | 시작·리포트 대조도 종목 단위. KiwoomBroker면 `KiwoomQuoteSource` 연결(모의 브로커는 없음 → 보유 중 매수 보류) |
| `config/settings.py/.yaml` | `guard.buy_price_buffer_pct: 1.0` |
| 테스트 | `test_account_guard` 26→39, `test_position_book` 14→16, `test_session_runner` 32→38, `test_quote_source` 6(신규) |

### 테스트 및 검증
- 재현 고정: 7-1·11-2(F1: B 미체결 중 A 불일치 매도 전송 0회), 7-6·12-3(F3: 원장 밖 보유·현재가 급등 차단), 7-7·12-1(현재가 누락 보류).
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과. 단타 원본 동등성 18/18.

### 변경하지 않은 것
- `OrderExecutor`의 주문 게이트·상태 전이(원본 동등성 유지), 매매 로직.
- 매도에는 금액·종목 수 한도를 적용하지 않음(기존과 같음).
- 대기 중인 매도 주문의 수량 예약(동시 매도 중복)은 `OrderExecutor`의 PENDING 차단에 맡김.

### 다음 작업
- 8-C: F2 분할청산(주문 완료 ≠ 전량 청산), F4 체결 event_id 범위.

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

## 2026-09-29 — 8-C: 분할청산(주문 종료 ≠ 전량 청산), 체결 식별자 범위 (GPT 기반 검토 F2·F4)

### 배경
- F2: 단타 원본은 항상 전량매도라 "매도 주문 종료 = 잔고 0"으로 판정. 100주 중 30주 매도가
  전부 체결돼 잔고 70이 되어도 SELL_PENDING·미해결 주문으로 남고, 저널 SELL 목표는 0 고정.
- F4: 체결 원장 event_id `{주문번호}:{누적수량}`에 거래일·계좌·종목 범위가 없어, 다른 날 같은
  주문번호·누적수량이 나오면 두 번째 체결이 `FillLedgerCorruptError`로 거부됨.
  (키움 주문번호의 유일성 범위는 실측 미확인 — 보수적으로 거래일·계좌 범위를 붙임.)

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/position/lifecycle.py` (**unchanged → modified**) | 매도 요청 시 `base_quantity_before_order`·`requested_quantity`·`expected_final_quantity = 보유 − 요청` 고정. `on_sell_result`: 잔고 = 목표(>0) → OPEN(FILLED_PARTIAL_EXIT), 목표 < 잔고 < 기준 → 대기 유지, 잔고 < 목표 → ERROR(UNEXPECTED_QUANTITY_DECREASE). SELL orphan 해제 기준·타임아웃 orphan 조건도 목표 잔고 기준. **목표 0(전량매도)이면 원본과 같은 판정** |
| `domain/service/order_executor.py` | 저널 SELL 목표 = 주문별 목표 잔고, 체결조회 FILLED 확정도 잔고 = 목표일 때만, 분할청산 종료 시 청산 훅 없이 보류 컨텍스트만 정리 |
| `domain/service/fill_recorder.py` | event_id = `{범위}|{주문 거래일}|{방향}|{종목}|{주문번호}|{누적수량}`, `scope` 필수(빈 값·`|` 거부), `track(order_date=)` |
| `app/main.py`, `app/session_runner.py` | 범위 = `broker.account_scope_id`(비면 "unscoped"), 주문 거래일 전달 |
| `tools/equivalence/compare.py` | 의도적 차이 명시: 매도 요청 후 다음 매수 전까지 PSM 매도 기준 3필드·저널 SELL base 값은 비교 제외 |
| `provenance.json` | lifecycle.py status → modified (note 포함) |
| 테스트 | `test_order_executor` 109→123(19절), `test_fill_recorder` 14→20(8절), `test_session_runner` 38→42(13절) |

### 테스트 및 검증
- F2 완료 기준: 100→70 정상 분할청산 OPEN·미해결 없음(19-2), 100→90 미완료 차단 유지·타임아웃 후 orphan(19-6~9),
  100→0 FLAT(19-10), 재시작 중단점에서 저널 목표 70 보존·ERROR 복원(19-13). 세션 하루(20주 매수 → 6주 분할청산) 대조 일치(13절).
- F4 완료 기준: 같은 거래 재조회 중복 0(8-2), 다른 거래일 같은 번호는 다른 사건(8-1), 같은 id 다른 내용은 차단(8-3), 계좌 범위 구분(8-4).
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과.
- 단타 원본 동등성 18/18 (위 의도적 차이 제외 규칙 적용 후).

### 변경하지 않은 것
- 이미 기록된 이전 형식 event_id — 그대로 유효(새 형식과 겹치지 않음), 이관 불필요.
- FillRecorder 추적은 여전히 메모리 전용. 주문 도중 재시작 시 ERROR 복원 후 사람 확인(자동 복구 아님).
  재시작 후 같은 체결을 다른 시각으로 다시 기록하려 하면 내용이 달라 원장이 거부하는 동작도 그대로.
- 부분 매도 체결가는 여전히 주문 직전 시세 추정(ORDER_ESTIMATE).

### 다음 작업
- 8-D: F5 마감 검증 상태(종료 성공 ≠ 마감 검증 성공), F6 과거 리포트는 기준일까지의 원장만.

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README) — 8-B 패치 위에 적용

## 2026-09-29 — 8-D: 마감 검증 상태 분리, 과거 리포트 기준일 원장 (GPT 기반 검토 F5·F6)

### 배경
- F5: 마감 후 잔고 조회가 실패해도 세션이 `COMPLETED`·종료 코드 0으로 끝났고, 장중 마지막 대조 결과가
  최종 결과처럼 남을 수 있었음. 리포트 파일 생성만으로는 하루가 정상이었는지 판단할 수 없음.
- F6: 리포트의 보유·누적 손익이 원장 전체를 사용 — 9/29 전량 매도 후 9/28 리포트를 다시 만들면 9/28 보유가 사라짐.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `app/session_runner.py` | `SessionSummary.close_check`(VERIFIED / NEEDS_REVIEW / NOT_RUN), `close_issues`, `final_balance_at`, `final_reconcile_at`, `daily_bars`, `report`. 마감 이후 잔고·대조가 없으면 `close_balance_retries`(3)회 × `close_balance_retry_sec`(15초) 재시도. 최종 대조는 **마감 시작 이후 것만** 인정. 판정 항목: FINAL_BALANCE_FAILED, FINAL_RECONCILE_MISSING, RECONCILE_MISMATCH, UNRESOLVED_ORDERS, HALTED, DAILY_BARS_FAILED (+ REPORT_FAILED) |
| `app/main.py` | 리포트 결과 기록, `reports/session_status_<날짜>.json`(원자적 쓰기), 종료 코드 0/1/**2(NEEDS_REVIEW)**, 일봉 갱신 실패 종목을 결과로 반환, 리포트는 최종(마감 후) 대조를 사용 |
| `infra/reporting/daily_report.py` | 기준일까지의 사건만으로 원장 재구성, 이후 사건 제외 건수 안내, `historical=True`면 메타·미해결 주문·장부 대조 미표시 |
| `app/reports.py` | `generate_daily_report(today=)` — 기준일 < 오늘이면 과거 재생성(현재 잔고·메타 미사용). 번들에 상태 파일 포함 |
| `scripts/run_swing.ps1` | 로그에 종료 코드 의미 표시 (UTF-8 BOM 유지) |
| 테스트 | `test_session_runner` 42→55(14절), `test_daily_report` 27→34(6절) |

### 테스트 및 검증
- F5 완료 기준: 마감 후 잔고 실패 → COMPLETED + NEEDS_REVIEW, 이전 대조를 최종으로 쓰지 않음, 3회 재시도(14-2~5),
  일시 실패 회복 시 VERIFIED(14-6), 마감 전 중지 NOT_RUN(14-7), 대조 불일치·일봉 실패·리포트 실패 각각 구분(14-8~11), 종료 코드 0/2/1(14-13).
- F6 완료 기준: 9/28 매수·9/29 매도 후 9/28 재생성 시 보유 10주·누적 0(6-1~2), 이후 매매 추가해도 과거 수치 동일(6-6).
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과. 단타 원본 동등성 18/18.

### 변경하지 않은 것
- 관측 오류가 있어도 장중 감시·대조 루프는 계속 돔(마감 판정만 NEEDS_REVIEW).
- 당일 포지션 메타 스냅샷 저장(과거 리포트에 그날 손절가 표시)은 하지 않음 — 필요 시 후속.
- 같은 날 여러 번 실행하면 리포트·상태 파일은 마지막 실행 기준으로 덮어씀.

### 다음 작업
- 9: 전략 없이 여러 거래일·장애·재시작 통합 검증(정상 3거래일 + 장중 재시작 + 부분체결 + API 장애 + 저장 실패).

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
