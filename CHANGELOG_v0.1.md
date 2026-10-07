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

## 2026-09-29 — 8-E: GPT 재검토 잔여 4건 (R1 주문 종료 증거, R2 마감 보고서, R3 상태 저장 실패, R4 명령 보관 실패)

### 배경
GPT 재검토(`08c6eab` 기준, F1·F3·F4·F6·F8 해결 확인)에서 잔여 4건 재현:
- R1 (P1): 30주 분할매도 후 잔고 70·주문 조회 OPEN/UNKNOWN/오류여도 일반 잔고 대조가 목표 잔고 도달만으로
  주문 종료를 확정 → 저널·차단 해제, 추가 70주 매도가 브로커까지 전달. timeout orphan도 목표 잔고로 해제.
- R2: 마감 후 잔고 실패(NEEDS_REVIEW)인데 보고서가 마지막 성공 잔고로 다시 대조해 "일치" 표시.
- R3: 상태 파일 교체 실패 시에도 VERIFIED·종료 코드 0, 이전 실행 파일이 남을 수 있음.
- R4: 보관 폴더 이동 실패 시 명령 원문 삭제(fallback), 사유 파일도 없음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/position/lifecycle.py` | `on_sell_result(order_filled=)`·`observe_for_orphan(order_filled=)`: 분할청산(목표 > 0)은 FILLED 증거가 있어야 종료/해제, 목표 도달만이면 `TARGET_REACHED_AWAITING_FILL_EVIDENCE`로 대기. 분할청산 timeout은 잔고와 무관하게 orphan. **전량매도(목표 0)는 원본과 동일**. 시계 주입(`clock=`) |
| `domain/service/order_executor.py` | 주문 조회 FILLED+목표 잔고일 때만 `order_filled=True` 전달(일반·timeout·orphan 경로 같은 기준). 시계 주입. 명령 보관: 삭제 fallback 제거 → `.hold`로 보존·사유 파일, 이름 변경도 실패하면 원문 유지 + 같은 내용 재실행 금지, 실행 결과와 보관 결과를 구분해 CRITICAL |
| `app/main.py` | 세션 clock을 executor에 전달(운영은 None → datetime.now). 마감 보고서는 최종 대조만(`final_only`), 없으면 사유·마지막 잔고 시각 표시. `run_id`, 상태 파일 저장 실패 → `STATUS_WRITE_FAILED`·이전 파일 제거·임시 파일 정리. REPORT_FAILED는 판정과 무관하게 누적. 종료 코드 2 = NEEDS_REVIEW **또는 기록된 문제가 있음** |
| `app/reports.py`, `infra/reporting/daily_report.py` | `final_only`·`reconcile_missing_reason`·마감 검증 표시 |
| `app/session_runner.py` | `run_id`, `generated_at`, `needs_attention`, `last_balance_at` |
| 테스트 | `test_order_executor` 123→133(19절 기대값 변경, 20·21절), `test_session_runner` 55→62(15절, 13절은 FILLED 증거로 종료) |

### 의도적 변경 (기존 기대값)
- 19-2·19-9: "목표 잔고 도달 → 해제"를 성공으로 보던 기대값을 "FILLED 증거 전에는 유지, FILLED 후 해제"로 변경.
- 세션 테스트의 SimBroker가 주문 완전 체결 시 주문 조회 FILLED를 돌려주도록 변경.

### 테스트 및 검증
- R1: OPEN·UNKNOWN·조회 오류 각각 추가 SELL 전송 0회·저널 유지(20절), FILLED+90은 대조 계속(20-4), 목표 도달 후 timeout → orphan 유지(20-5), orphan은 FILLED로만 해제(19-9b).
- R2: 15:29 성공·마감 실패 → 보고서에 "일치" 없음, "마감 최종 대조 미확보"·15:29:00·"마감 검증에 사용 불가"(15-1).
- R3: 상태 파일 교체 실패 → STATUS_WRITE_FAILED·이전 파일 제거·tmp 없음(15-4~6), 종료 코드 규칙(14-13).
- R4: failed/·processed/ 경로 충돌·이동/이름 변경 모두 실패 각각 원문 보존·재실행 없음(21절).
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과. 단타 원본 동등성 18/18(전량매도 경로 동일).

### 변경하지 않은 것
- 주문 조회가 취소·거부 등 FILLED 외 종결 상태를 돌려주면 여전히 "미지원 상태, 유지"(차단 유지 → HTS 확인 후 `ack_orphan`). 체결 수량 대조 규칙은 후속.
- 매수(BUY_PENDING)의 목표 잔고 확정 규칙은 원본 그대로.
- F4 후속: 계좌/환경 혼용 방지 기동 검증, F1 후속: 같은 종목 진행 중 주문의 수량 범위 검증, FillRecorder 메모리 전용 — 모두 별도 과제.

### 다음 작업
- 9: 전략 없는 다일 장애 통합 검증(이번 시계 주입으로 PSM 타임아웃·주문 조회 경과도 가짜 시계로 검증 가능).

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

## 2026-09-29 — 8-F: 복구 명령 재시작 안전 (GPT 재검토 `4ae3e0a` R4 잔여, P1)

### 배경
- GPT 재검토: R1~R3 해결, R4는 같은 프로세스 내 재실행 방지만 해결. 보관·`.hold` 이름 변경이 모두 실패하면 처리한
  명령을 메모리에만 기록 → 재시작 후 남아 있던 **옛 `ack_error` 명령이 새 주문의 복원된 ERROR를 해제**, 저널 정리·추가 매수 허용 재현.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/service/order_executor.py` | **실행 전 확보**: 명령을 `commands/processing/`으로 원자적 이동한 뒤에만 실행(이동 실패 시 실행 안 함, CRITICAL 1회). 실행 후 `processed/`·`failed/`로 이동, 실패하면 `processing/`에 보존(실행 대상 아님 → 재시작해도 재실행 없음). `.hold`·메모리 목록 방식 제거. **복구 사건 ID**: ERROR·orphan마다 프로세스별 새 `recovery_id` 발급(재시작 복원 시에도 새 ID), `[RECOVERY_REQUIRED]` CRITICAL + `commands/recovery_required.json`(명령 템플릿). 명령의 `recovery_id`가 현재 사건과 다르면 거부 |
| `tools/equivalence/runner.py` | 새 구현 쪽 명령에 현재 `recovery_id` 포함(원본 쪽은 그대로) |
| `infra/reporting/daily_report.py`, `README.md` | 명령 작성법 안내 |
| `test_order_executor.py` | 기존 명령 테스트에 `recovery_id`, 21절 재작성 133→140(재시작 재현 21-4, 옛 ID 거부 21-5, 목록 파일 21-6, 현재 ID 적용 21-7, ID 없음 거부 21-8) |

### 테스트 및 검증
- 완료 기준(21-4): 옛 ERROR 해제 → 보관 실패 → 새 매수 미체결 → 재시작 → ERROR·저널 유지, 추가 주문 0회.
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과. 단타 원본 동등성 18/18.

### 운영 변경
- 복구 명령에 `recovery_id` 필수. 재시작하면 ID가 바뀌므로 `commands/recovery_required.json`에서 현재 ID를 확인해 작성.

### 변경하지 않은 것
- ERROR·orphan 해제 조건 자체, 매매 로직.

### 다음 작업
- 9: 전략 없는 다일 장애 통합 검증.

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

## 2026-09-30 — 8-G: 명령 확보 재시도, 테스트의 운영 폴더 격리 (GPT 재검토 `c9cbfa9` P2 2건)

### 배경
- GPT 재검토: 8-F의 P1(재시작 후 옛 명령) 해결 확인, Actions 4환경 성공. P2 2건:
  1. `_claim_failed`가 로그 억제뿐 아니라 **실행 재시도까지** 막아, 일시적 잠금이 풀려도 파일을 고치거나 재시작하기 전까지 ERROR 유지.
  2. `run_session()`이 `commands_dir`를 넘기지 않아 기본 `commands/` 사용 → 회귀 테스트가 저장소의 `commands/recovery_required.json`을 수정.
     실행 산출물인 이 파일이 git에 커밋돼 있었음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/service/order_executor.py` | `_claim_failed`를 시그니처 → 마지막 실패 시각으로 바꿔 **로그 1회 + 30초(`CLAIM_RETRY_SEC`) 간격 재확보**. 재확보 성공 시 WARNING 후 실행(확보 뒤 `recovery_id` 검사는 그대로). 기동 시 복구 대상이 없어도 목록 파일을 빈 목록으로 갱신 |
| `config/settings.py/.yaml` | `storage.commands_dir: commands` |
| `app/main.py` | 실행기에 `settings.storage.commands_dir` 전달 |
| `testing_helpers.py` | 테스트 설정의 `commands_dir`를 임시 폴더로 |
| `.gitignore`, `commands/recovery_required.json` | `commands/` 제외, 커밋된 목록 파일 삭제(기동 시 재생성) |
| 테스트 | `test_order_executor` 140→144(21-3b·c 재시도, 21-9 확보 실패+재시작 한 시나리오, 21-10 빈 목록), `test_session_runner` 62→64(16절: 임시 commands 사용, 저장소 `commands/` 전후 동일) |

### 테스트 및 검증
- 완료 기준 1: 일시 확보 실패 → 권한 복구 → 파일 수정·재시작 없이 정확히 1회 처리(21-3c).
- 완료 기준 2: 회귀 전후 `git status` 동일, 저장소 `commands/`에 파일 생성·변경 없음(16-2 + 수동 확인).
- `run_regression_tests.py --skip test_broker_order_status.py`: 24개 전부 통과. 단타 원본 동등성 18/18.

### 운영 참고
- 패치 적용 시 추적 중이던 `commands/recovery_required.json`이 삭제되지만, 다음 기동 때 현재 상태로 다시 만들어집니다.

### 다음 작업
- 9: 전략 없는 다일 장애 통합 검증.

### 전달 파일
- 패치 0001 (fix), 0002 (CHANGELOG/README)

## 2026-09-30 — 9단계: 전략 없는 다일 장애 통합 검증

### 배경
- GPT 재검토(`99649f1`): 8-G 해결, 추가 결함 없음 → 9단계 진행 권고.
  완료 기준: 중복 주문·체결 기록 0건, 원장·잔고 정합, 불확실한 주문 차단 유지, 보고서·상태 파일·종료코드 일관.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `test_multiday_integration.py` (신규, 20건) | 같은 저장 경로·같은 가짜 브로커·가짜 시계로 4거래일 연속 실행 (아래 시나리오) |
| `provenance.json`, `README.md` | 새 테스트 등록·안내 |

코드 변경 없음 — 기존 구현으로 시나리오 전체가 기준을 만족했습니다.

### 시나리오와 결과
| 거래일 | 사건 | 확인 |
|---|---|---|
| D1 9/28 | 매수 20주 | 원장 20 = 잔고 20, VERIFIED·종료코드 0, 보고서·상태 파일 일치 |
| D2 9/29 | 보유만, 상태 파일 교체 실패 주입 | 보유 거래일 1, STATUS_WRITE_FAILED·종료코드 2, 상태 파일 없음 |
| D3 9/30 | 6주 분할매도 → 4주 매도 접수 직후 장중 중지 → 중지 중 체결 → 재시작 → 복구 명령(확보 1회 실패 후 재확보) → API 장애 5분 → 마감 | 분할매도는 FILLED 후 종료·원장 14. 재시작 시 ERROR 복원·새 recovery_id. 사람 확인 전 재매도(ERROR 게이트)·매수(PENDING_AMOUNT_UNKNOWN) 전송 0회. 명령 1회만 적용. 장애 중 주문 없음. 재시작 구간 체결은 자동 기록 안 됨 → 원장 14 vs 잔고 10 → 불일치 종목 매수 차단, 마감 NEEDS_REVIEW(RECONCILE_MISMATCH)·종료코드 2, 보고서·상태 파일 일치 |
| (마감 후) | 사람이 HTS 확인 후 4주 매도 정정 사건 기록 | — |
| D4 10/1 | 보유만 | 원장 10 = 잔고 10, VERIFIED·종료코드 0 |
| 전 기간 | — | 주문 3건(매수 20·매도 6·매도 4)만, event_id 유일, 실현 10·보유 10, D1 보고서 재생성해도 20주, 미해결 주문·저널·복구 목록 없음 |

### 확인된 운영상 한계 (결함 아님, 후속 후보)
1. **재시작 구간 체결은 원장에 자동 기록되지 않습니다**(FillRecorder 메모리 전용). 불일치로 드러나 NEEDS_REVIEW가 되고,
   사람이 원장에 정정 사건을 추가해야 합니다 — 지금은 이를 위한 도구가 없어 테스트는 `FillLedgerStore.append()`를 직접 사용.
   → 후속: 정정 사건 기록 도구(`tools/ledger_correct.py`, 잔고·체결조회로 확인 후 기록).
2. 재시작 직후 미해결 주문이 걸린 종목은 장부 대조에서 수량 차이가 INFO라 **안전 한도(guard)는 재매도를 통과**시키고,
   주문 실행부의 ERROR 게이트가 막습니다. 결과는 안전하지만 차단이 한 겹입니다 → 후속: ERROR·orphan 종목을 guard 차단 목록에도 포함.

### 테스트 및 검증
- `run_regression_tests.py --skip test_broker_order_status.py`: **25개** 전부 통과. 회귀 후 `git status` 깨끗.

### 다음 작업
- 위 한계 2건 보완(정정 도구, guard 이중 차단) 또는 데이터 보강(지수·유니버스) / 백테스트 — 방향 결정 필요.

### 전달 파일
- 패치 0001 (test), 0002 (CHANGELOG/README)

## 2026-09-30 — 9-A·9-B: 다일 검증 보완 (GPT 재검토 `0014b33` P2 2건)

### 배경
- 9-A: D2(상태 파일 저장 실패)에서 세션 판정 NEEDS_REVIEW·종료코드 2인데 **보고서는 `마감 검증: VERIFIED`**.
  보고서를 먼저 만들고 상태 파일 실패를 나중에 판정에 추가하기 때문. D2 테스트는 보고서 판정을 보지 않아 통과했음.
- 9-B: D3의 API 장애 구간 매수 대상(005930)은 이미 불일치로 차단돼 있어, "잔고 실패 시 이전 잔고로 계속" 같은 회귀를
  넣어도 통합 테스트 20건이 모두 통과(GPT 시험으로 확인). 장애 자체의 차단 효과를 구분하지 못함.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `app/main.py` | 상태 파일 저장 실패 시 판정에 `STATUS_WRITE_FAILED`를 더한 뒤 **보고서를 최종 판정으로 다시 생성**. 보고서 생성 실패 → 상태 파일에 REPORT_FAILED 기록하는 기존 동작 유지 |
| `test_multiday_integration.py` | D2-3: 보고서·세션 판정 일치(`consistent(..., status_expected=False)`) + `STATUS_WRITE_FAILED` 표시. D4: 원장·잔고 정상인 **000660** 매수 의도를 잔고 API 장애 구간(10:00~10:05)에 배치 — 장애 중 전략 호출 0회·전송 0회, 복구 후 첫 폴링(10:05)에 1회. 전략 호출 시각·주문 전송 시각 기록. ALL-6: 종료코드를 **운영 `main()`**으로 확인(0, 2, 2, 0). 20→24건 |

### 테스트 및 검증
- 변이 확인: `app/main.py`를 이전 버전으로 되돌리면 D2-3 실패, 세션의 잔고 실패 처리를 "이전 잔고로 계속"으로 바꾸면 D4-1·D4-2 실패 — 두 회귀 모두 잡힘.
- `run_regression_tests.py --skip test_broker_order_status.py`: 25개 전부 통과. 단타 원본 동등성 18/18.

### 다음 작업
- 원장 정정 도구 → ERROR·orphan 종목 guard 이중 차단 → 데이터·백테스트 준비 (GPT 권고 순서).

### 전달 파일
- 패치 0001 (fix+test), 0002 (CHANGELOG)

## 2026-09-30 — A1·A3: 연구 원천 실측 도구, 순수 지표·시장 환경·주봉·S1_BASE 평가기 (주문 없음)

### 배경
- 사용자 결정: 원장 정정 도구·guard 보강보다 **매매 로직(S1)을 주문 없이 먼저 구축해 관찰 데이터를 쌓기**.
- 요청서·보충안을 Claude·GPT가 검토해 합의한 명세를 `docs/research_a_stage.md`로 정리
  (S1_BASE 주 가설, 전체 보통주, 날짜 기준 백필, 백필 자격 조건별 분리, EMA 6N, 사건 ID, 주봉 사용 시각).

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/research/` (신규) | `types`(PASS/FAIL/UNKNOWN, NaN·무한대 차단), `series`(ResearchBar·SeriesView: 미래 봉·세션 차단, 세션 기준 창, 상장 전/중간 공백 구분), `features`(f1), `market`(m1), `weekly`(w1), `s1`(s1_pullback_v0.1) |
| `tools/probe_research_sources.py` (신규) | A1 조회 전용 프로브: 종목 목록(ka10099 후보) 필드·값 분포·코드 형태, 일봉 거래대금 후보 필드·단위 추정·이력 깊이, 지수 일봉(ka20006 후보) 필드·원문 값·소수점 여부. 오류 응답도 기록 |
| `tools/probe_market_data.py` | 허용 TR에 ka10099·ka20006(조사 후보) 추가 — 모의 도메인 제한·주문 TR 금지는 그대로 |
| `docs/research_a_stage.md` (신규), `README.md` | 합의 명세·안내 |
| 테스트 (신규) | `test_research_features`(50), `test_research_s1`(30), `test_probe_research_sources`(14) |

### 테스트 및 검증
- 경계·실패: 상장 전 vs 중간 공백, 기준일 봉 없음, ATR 0·분모 0, 거래대금 하나라도 없으면 UNKNOWN(종가×거래량으로 채우지 않음),
  t 당일 급등이 수축 지표 불변, 252개 미만 52주 UNKNOWN, EMA 6N 경계·공백 뒤 재시작·손계산 일치,
  주봉(수요일 진행 중 제외·금요일 휴장·달력 미확정·일봉 누락 주), SMA30W ≠ SMA150.
- S1: 정상 눌림 회복 PASS, MA60 이탈·RS 부족·과열·전일 고가 미돌파·하락 추세 반등·조정 길이·낮은 종가 없음·빈 조정 구간·고점 동률,
  위험 상태 UNKNOWN/우선주/위험 종목/거래대금 부족·없음, 시장 MIXED 보류·지수 없음 UNKNOWN, 이력 부족·공백, INVALID_STOP,
  **t 이후 봉·지수를 바꿔도 결과 동일**, 설정 해시, 결정적 정렬, 연구 계층의 주문·브로커·원장·네트워크 의존 없음.
- `run_regression_tests.py --skip test_broker_order_status.py`: **28개** 전부 통과. 단타 원본 동등성 18/18.

### 변경하지 않은 것
- 운영 세션·NullStrategy·주문 경로·원장·복구 규칙.
- 실제 원천 필드·단위·지수 배율은 **미확인**(프로브 실행 전). 테스트의 응답 형식은 가짜.

### 다음 작업
- 사용자: 장 마감 후 `python tools/probe_research_sources.py` 실행 → 요약 파일 공유.
- A2: 결과를 반영한 날짜별 종목 목록·지수·거래대금 수집(중단 후 이어서), 과거 달력 보강.

### 전달 파일
- 패치 0001 (feat), 0002 (docs) — 9-A·9-B 패치 위에 적용

## 2026-09-30 — A13-R1~R5: 연구 계산층 경계 보완 (GPT 재검토 `94dca36`)

### 배경
GPT 재검토: 회귀 28/28·신규 94/94·동등성 18/18 통과, 그러나 백필 연결 전 보완 5건 재현.
- R1(P1): 종목·지수·시장 판정의 기준일을 맞춰 보지 않음 — 2025-10-20 신호에 2026-01-02 기준 지수를 넘기자 RS60·최종 판정이 경고 없이 바뀜.
- R2(P1): 수요일까지 잘린 세션 목록을 주봉에 넘기면 수요일을 주 마지막으로 보고 완성 처리. `available_at` 없음.
- R3(P2): 세션 중복·정렬 미검증 — 159개 봉으로 HISTORY 160 통과.
- R4(P2): INVALID_STOP이어도 최종 신호 PASS.
- R5(P2): +무한대 가격 허용 → 주봉에 Infinity·NaN.

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| R1 | `features.rs`, `market.py`, `s1.py` | RS는 두 View 기준일이 같고 수익률 구간 날짜 배열이 같을 때만(아니면 `AS_OF_MISMATCH`/`SESSION_ALIGNMENT_MISMATCH` UNKNOWN). `MarketRegime`에 `as_of`·`index_id`. 평가기는 지수 View·시장 판정 기준일이 종목과 다르면 시장 UNKNOWN. `market=None`이면 같은 지수로 직접 판정 |
| R2 | `weekly.py` (w1→w2) | 주봉은 **주 단위 예정 일정(`WeekSchedule`)**으로만 판정: `CalendarWeekSchedule`(TradingCalendar), `ExplicitWeekSchedule`(known_through까지 모든 평일이 세션·휴장일로 명시돼야 함, 잘린 목록 → `ScheduleCoverageError`). `session_closed_at`·`available_at`(종료+30분 `ASSUMED_DELAY` / 실제 확보 시각 `OBSERVED`)·`availability_basis`. 평가 시각 as_of보다 늦게 사용 가능한 주 제외. 일정 불명 지난 주는 불완전으로 남김 |
| R3 | `series.py` | 세션 목록 date 타입·엄격한 오름차순 검증(오류), 기준일 타입, 창 인자 음수·0 → BAD_WINDOW, 반환 길이 검증 |
| R4 | `s1.py` | `STOP_VALID` 조건·`stop_valid` 필드. `eligible_signal` = 패턴·자격·시장·손절 유효 **모두 PASS**. 패턴 등 개별 결과는 보존. `S1Config` 값 검증(양의 정수·0 이상 유한수·pullback 범위) |
| R5 | `series.py`, `weekly.py` | 가격 `math.isfinite`+양수, volume·trade_value의 bool 거부, 주봉 추세 비유한값 → UNKNOWN(`NON_FINITE`) |
| 문서 | `docs/research_a_stage.md` | available_at 시간대(Asia/Seoul naive)·데이터 확보 시각·백필 해석, 기준일 정합 규칙 |

### 테스트 및 검증
- 새 경계 테스트: `test_research_features` 50→66, `test_research_s1` 30→38.
  R1: 미래 기준일 지수 View·시장 판정, 다른 날짜 시장 판정만 전달, 지수 세션 하나 누락. R2: 잘린 목록 오류, 수요일 진행 중 주 제외,
  금요일 휴장 주 목요일 16:00 사용 가능·14:00/15:40 제외·OBSERVED, 실제 달력 어댑터(2026 추석 주·미지원 연도·특수일). R3: 중복·역순 세션, 159봉+중복 세션.
  R4: INVALID_STOP → 패턴·자격·시장 PASS 보존, 최종 FAIL. R5: ±무한대·NaN·음수·bool 거부, allow_nan=False 직렬화.
- 변이 확인: R1 검사·R4 결합을 되돌리면 새 테스트 6건 실패.
- **정상 S1 기준선 결과 유지**: 기존 정상 시나리오 PASS 그대로(1-1~1-4), 같은 기준일 지수로 직접 판정한 결과 = 기존 결과(8-3).
- `run_regression_tests.py --skip test_broker_order_status.py`: 28개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/` 생성 없음.

### 변경하지 않은 것
- 전략 조건·임계값, 운영 원장·복구·주문 경로. 실제 원천 필드·단위(프로브 결과 대기).

### 다음 작업
- A1 실측 결과 반영 → A2 수집(과거 달력 보강 포함).

### 전달 파일
- 패치 0001 (fix), 0002 (docs)

## 2026-09-30 — A13-Q1~Q3: 지수 원천 일치·주봉 확보 시각·중간 공백 (GPT 재검토 `bfcaaec`)

### 배경
GPT 재검토: R1~R5 주요 수정 확인(회귀 28/28·연구 118/118·동등성 18/18), 잔여 3건 재현.
- Q1(P1): 같은 날짜의 다른 지수(KOSDAQ)로 만든 시장 판정을 넘기면 하락 지수 View인데도 시장·최종 후보 PASS.
- Q2(P1): 주봉 확보 시각을 마지막 세션만 봄 — 월요일 봉을 토요일에 복구해도 금요일 20:00 평가에 그 주가 완성으로 사용됨.
- Q3(P1): 끝난 과거 주의 입력이 준비 안 되면 목록에서 빠지고, 남은 34개로 추세 계산(주 간격 14일인데 UP_PROXY).

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| Q1 | `series.py`, `market.py`, `s1.py` | `SeriesView(..., source_id=)` 원천 식별자. `classify_market`은 생략 시 View 식별자 사용, 다른 식별자 주면 오류. 스캐너 기본 경로는 `market=None`(같은 지수 View로 계산). 외부 `market=`은 View·판정 식별자가 둘 다 있고 같아야 함 → 아니면 `INDEX_SOURCE_MISMATCH` UNKNOWN. `Eligibility.market_index_id`(종목 당시 소속 시장 지수)가 View와 다르면 RS·시장 UNKNOWN(`INDEX_NOT_STOCK_MARKET`) |
| Q2 | `weekly.py` (w2→w3) | `mode` 명시: `ASSUMED_DELAY`(백필, 종료+30분) / `OBSERVED`(**주 전체 봉 확보 시각 중 최댓값**과 종료 시각의 최댓값). 관측 모드에서 확보 시각 하나라도 없으면 `READY_TIME_UNKNOWN`, 세션 종료 전 확보(장중 봉)는 `READY_BEFORE_SESSION_CLOSE` — 가정으로 메우지 않음. 모드 혼용·알 수 없는 모드·음수 지연 거부 |
| Q3 | `weekly.py` | 진행 중 주는 제외, **끝났지만 준비 안 된 주는 `DATA_NOT_READY` 자리로 유지**(가장 최근에 끝난 주 하나만 도착 전으로 잘라냄). 한 주 전체 예정 휴장은 `gap_weeks_before`로 기록. `weekly_trend`는 34주 창의 주 시작 간격 = 7×(1+gap_weeks_before) 검사(`WEEK_SEQUENCE_GAP`) + 불완전 주 UNKNOWN |
| 문서 | `docs/research_a_stage.md` | 주봉 모드·자리 유지 규칙, **A2 수집기가 넘길 봉별 `ready_at`**(완성 봉으로 처음 들어온 응답 수신 시각, 장중 봉 제외, 복구 시각, 덮어쓰기 금지, run_type별 모드), 지수 `source_id` 명명·`market=None` 기본 경로 |

### 테스트 및 검증
- `test_research_s1` 38→44 (9절): KOSDAQ 시장 판정 혼입 → 최종 PASS 안 됨, 식별자 없는 외부 판정 UNKNOWN, 같은 원천 외부 판정 = 기본 경로 결과, 소속 시장 불일치, 올바른 소속 시장 통과, 다른 식별자 재표기 오류.
- `test_research_features` 66→78 (8-6 관측 모드로 갱신, 10절 12건): 주중 봉 늦은 확보(금 20:00 미사용 → 토요일 이후 최종 확보 시각으로 OBSERVED), 주중 확보 시각 누락, 장중 봉, 중간 주 DATA_NOT_READY → UNKNOWN·준비 후 정상 재개, 목록에서 중간 주 제거 → WEEK_SEQUENCE_GAP, 전체 휴장 주 정상 vs 거래 주 데이터 공백 UNKNOWN, 연속 두 주 미준비, 인자 거부, 정상 주봉 결과 유지.
- 변이 확인 9종(마지막 세션 확보만 사용·누락 가정 채움·미준비 주 제거·연속성 검사 제거·휴장 주 미기록·끝 미준비 전부 제거·장중 봉 허용·외부 판정 검증 제거·소속 시장 검사 제거) — 모두 해당 새 테스트가 실패로 잡음.
- **정상 S1 기준선 유지**(1-1~1-4, 8-3, 9-3), 정상 주봉 결과 유지(10-11).
- `run_regression_tests.py --skip test_broker_order_status.py`: 28개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/` 생성 없음.

### 변경하지 않은 것
- S1 임계값·가격 패턴, 세션 검증·STOP_VALID·유한값 검사. 운영 원장·복구·주문 경로.

### 다음 작업
- A2 수집(날짜별 유니버스 스냅숏, 봉별 ready_at·run_type 저장, 지수 봉, 재개 가능한 백필, 과거 휴장일 후보) — 지수 배율·투자주의·외국기업 결정 대기.

### 전달 파일
- 패치 0001 (fix), 0002 (docs)

## 2026-09-30 — A2: 연구 데이터 수집 (목록 스냅숏·재개 가능한 백필·매일 갱신, 조회 전용)

### 배경
- 사용자 결정(GPT 권고 동의): 지수 OHLC ÷100(공식 명세), 투자주의·투자주의환기종목 초기 S1 제외, 외국기업 초기 S1 제외.
- GPT A2 보완 여섯 가지: ① 거래량 0 봉 표시·계산 정책 ② 현재 위험 종목도 과거 수집(수집 대상·신호 자격 분리)
  ③ orderWarning 숫자 번역 금지 ④ 수정주가 기준 저장·혼합 금지 ⑤ 페이지 수가 아닌 날짜로 종료·종목별 부족 사유
  ⑥ 장중 스냅숏과 완성 데이터 구분(observed_at), 거래대금 반올림 오차 단정 삭제.
- A1 원시 응답 재계산: 주식 2,740 → 수집 대상 2,544 / 현재 위험 257 / 현재 자격 2,287, state만 관리종목 전체 84(주식 82).

### 변경 내용
| 구분 | 파일 | 내용 |
|---|---|---|
| ① | `domain/research/series.py`, `features.py`(f1→f2), `weekly.py` | `ResearchBar.no_trades`(거래량 0). 기준일이면 NO_TRADES_AT_T, 창 안이면 UNKNOWN(NO_TRADES:날짜), EMA 연속 구간도 끊김. 주봉은 합산하고 `no_trade_days` 표시 |
| ②③ | `domain/research/universe.py` (u1) | 증권 유형(코드 끝 우선주 추정·스팩·외국기업·ETF/ETN/리츠 등), 위험 표시 합집합(auditInfo·state 토큰·orderWarning 원래 숫자), 필드 없으면 *_MISSING. collect(보통주 전체)와 eligible_now 분리, 정책 버전·해시 |
| 원천 | `infra/research/kiwoom_readonly.py` | 모의 도메인 전용·허용 TR 3개(ka10099·ka10081·ka20006) 조회 클라이언트. 1초 간격, 429·전송 실패 재시도, 401 재인증 1회 |
| ①⑥ | `infra/research/kiwoom_rows.py` | 행 해석: 부호 제거, 지수 ÷100, 거래대금 백만원→원, NO_TRADES / INVALID:사유(원래 행 보존), 날짜 불명 행은 RowError |
| ④ | `infra/research/store.py` (SQLite, `data/research/`) | 날짜별 스냅숏(observed_at·장 단계·정책·원문 gzip), 시계열 조정 기준·revision·verified_base_dt, 겹침 구간이 다르면 통째 교체+bar_history 보존, run_type(BACKFILL/FORWARD)·ready_at, 백필 작업·항목 |
| ④⑤⑥ | `infra/research/collector.py` | 필요 시작일(2017-01-02) 도달로 종료, LISTED_AFTER_START / HISTORY_END / PAGE_CAP, 작업별 base_dt 고정·종목 단위 트랜잭션·재개, 완성 봉 기준(정규장 종료+160분), 매일 갱신 겹침 비교·재수집, 누락 복구 봉 ready_at = 복구 시각, 열린 백필 종목 건너뜀, 새 상장 INIT |
| 달력 | `domain/research/holiday_candidates.py` | 지수 날짜 → 과거 휴장일 후보·추정 이름·지수 간 날짜 차이(사람 확인용 초안, 달력 파일은 안 고침) |
| 도구 | `tools/research_collect.py` | `universe`(프로브 파일 오프라인 확인 포함) · `backfill` · `update` · `status` · `holidays` |
| 문서 | `docs/research_a_stage.md`, `README.md` | A2 절: 결정·단위·분리 원칙·거래 없는 봉·완성 기준·종료 조건·수정주가(수정가격 ≠ 과거 실제 체결가)·ready_at·실행 순서 |

### 테스트 및 검증
- 신규 `test_research_collect.py` 66건(실측 파일 지정 시 67건): 삼성전자 2018-04-30 실측 행 NO_TRADES, 지수 ÷100, 거래대금 원 환산,
  INVALID 사유, 유형·위험 합집합·orderWarning 원래 값·필드 누락, 위험 종목 수집 포함, 도메인·TR 차단, 재시도·재인증,
  날짜 기준 종료(원천에 더 있어도 5페이지에서 멈춤), 페이지마다 같은 base_dt, 상장 늦음/이력 짧음/상한 구분,
  장중 12:31 당일 봉 제외·18:10 기준, 중단 후 이틀 뒤 재개(같은 base_dt·DONE 재조회 없음), 트랜잭션 원자성, ERROR 재시도,
  매일 갱신 FORWARD·ready_at, 장중 값 미저장, 분할 재계산 → REBASE(현재 봉 전부 새 revision·이전 값 보존·ready_at 유지),
  날짜 소실·누락 복구, 주봉 OBSERVED 연결(FORWARD 주 완성·BACKFILL 섞인 주 불완전·복구 시각 반영), 휴장일 후보(2026 달력과 일치),
  CLI 흐름, 연구 계층 import 경계, 운영 폴더·달력 파일 불변.
- `RESEARCH_PROBE_JSONL`로 A1 원시 응답을 지정하면 2,740 / 2,544 / 257 / 2,287 / 84 확인(이번 검증에서 통과).
- `test_research_features` 78→86(거래 없는 봉 8건, 3-8 갱신), `test_research_s1` 44→47(거래 없는 봉 3건, 버전 f2).
- 변이 확인 12종(날짜 종료 제거·완성 기준 제거·겹침 확인 제거·REBASE 제거·위험 종목 수집 제외·재개 base_dt 변경·
  거래 없는 봉 정책 제거·지수 배율 제거·orderWarning 번역·재수집 ready_at 초기화·외국기업 보통주 처리·복구 봉 FORWARD 제거) — 모두 새 테스트가 잡음.
- `run_regression_tests.py --skip test_broker_order_status.py`: 29개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/`·`data/` 생성 없음.

### 변경하지 않은 것
- S1 임계값·패턴, 운영 브로커·주문 실행부·원장·복구 경로, `config/krx_calendar.yaml`(과거 연도는 사람 확인 후 추가).
- 운영용 `tools/update_daily_bars.py`·`infra/market_data/`(주문 계좌 설정 기반)는 그대로 — 연구 수집은 별도 경로.

### 다음 작업
- 실측 순서: `universe` → `backfill --limit 20` 확인 → `backfill` 전부(약 3시간, 재개 가능) → `holidays` 후보 확인·달력 추가.
- 장 마감 후 시각별(15:40·16:10·18:10) 당일 봉 비교로 완성 기준 160분 조정 여부 확인.
- A4 스캔·저장·보고: 스냅숏 적용 범위(t일 장 마감 뒤 관측분), 백필 자격(과거 위험 UNKNOWN) 처리, 주봉 지연 값 연결.

### 전달 파일
- 패치 0001 (feat), 0002 (docs)

## 2026-09-30 — A2-R1~R4·Q-R1·Q-R2: 값 revision·사용 가능 시각, 재수집 검증, 연속조회 계약, state 위험 (GPT 재검토 `166585b`)

### 배경
GPT 재검토: A2 주요 기능 확인(회귀 29/29·수집 67/67·지표 86/86·S1 47/47·동등성 18/18), 새 문제 4건 + 기존 2건 재현.
- A2-R1(P1): REBASE한 새 값에 이전 ready_at을 그대로 붙임 → 10/2에 정정한 값을 10/1에 알았던 것처럼 평가 가능.
  여러 페이지의 수신 시각도 첫 페이지 시각으로 덮음.
- A2-R2(P1): 변경 감지 후 재수집이 빈 응답·한 행이면 REFETCH_EXTEND로 처리하고 조정 기준일만 새 날짜로 바꿈.
- A2-R3(P2): cont-yn=Y인데 next-key가 없으면 이력 끝으로 해석, return_code 누락도 성공으로 봄.
- A2-R4(P2): 빈 state와 state에만 있는 투자주의·환기·경고·단기과열이 현재 자격을 통과.
- Q-R1(P1): 같은 지수 ID·같은 날짜의 다른 입력으로 만든 시장 캐시가 통과.
- Q-R2(P1): 최신 종료 주가 다음 주까지 미확보여도 그 주를 지우고 이전 34주로 정상 추세.

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| R1 | `infra/research/store.py` (스키마 r1→r2) | 봉마다 `received_at`(그 페이지 수신 시각)·`available_at`(= max(수신, revision 활성 시각))·`first_ready_at`(기록용). `series_revision`(조정 기준·활성·대체 시각). `research_series(sid, as_of=X)` — X에 활성이던 revision의 available_at ≤ X 봉만. 반환은 `ResearchSeries`(bars·available_at·revision·basis·integrity) |
| R1 | `infra/research/collector.py` | 행마다 페이지 수신 시각 보존(`FetchedBar`), 새 revision 활성 시각 = 마지막 페이지 수신 시각 |
| R2 | `store.py`, `collector.py` | `init_series`(새 시계열, 부족해도 저장)와 `replace_series`(기존 시계열, **후보 검증 후** EXTEND/REBASE) 분리. 검증: 비어 있지 않음·저장 마지막 날짜 포함·필요 시작일(또는 저장 첫 날짜) 포함·변경을 발견한 첫 페이지 값 재현. 실패·재수집 조회 실패 → 값·메타 그대로, integrity=REBASE_REQUIRED·VERIFY_FAILED, append 거부, 다음 갱신 때 바로 재수집. update 집계 `failed`(종료 코드 1) |
| R3 | `infra/research/kiwoom_readonly.py`, `collector.py` | return_code 있고 0, cont-yn Y/N, Y면 next-key 필수. 페이지 안 내림차순·다음 페이지 과거 진행·base_dt 뒤 날짜 없음·키 반복 없음·빈 첫 응답/빈 연속 페이지 금지 → 오류(ERROR, 저장 안 함). 목록 스냅숏도 같은 계약 |
| R4 | `domain/research/universe.py` (u1→u2) | 빈·공백 state → STATE_MISSING, state 토큰도 위험 범주 전체(관리·정지·투자주의·환기·경고·위험·단기과열·정리매매), 모르는 토큰 → STATE_UNRECOGNIZED. collect는 그대로 |
| Q-R1 | `domain/research/s1.py` | 외부 시장 판정은 같은 index View로 재계산한 판정과 모든 값이 같을 때만 사용(아니면 MARKET_INPUT_MISMATCH) |
| Q-R2 | `domain/research/weekly.py` (w3→w4) | 가장 최근 종료 주 미확보는 다음 거래일 0시 전에만 잘라냄(정상 대기). 그 뒤엔 `DATA_NOT_READY:OVERDUE` 자리 → 추세 UNKNOWN. 다음 거래일 모르면 자리 유지 |
| 도구 | `tools/research_collect.py` | `backfill --recheck-shortfall`(HISTORY_END·PAGE_CAP만 재수집·검증), status에 integrity·VERIFY_FAILED·스키마, update 실패 집계 |
| 문서 | `docs/research_a_stage.md` | 시각 세 가지·시점 조회·과거는 ASSUMED(가정 분석), 재수집 검증·REBASE_REQUIRED, 첫 페이지 범위 정합 검사 표현, 연속조회 계약, state u2, 시장 캐시 재계산, 최근 주 OVERDUE, NO_TRADES HISTORY UNKNOWN 표현 정정, A4로 넘길 규칙, 기존 DB 이전 방침 |

### 테스트 및 검증
- `test_research_collect` 66→94건(실측 파일 지정 시 95): 10/1 확보→10/2 정정→10/1 시점 조회 값·S1 결과가 당시와 동일,
  현재 revision으로는 달라짐, 활성 시각 전후 revision 전환, revision 기록, 페이지별 수신 시각(19:05/19:10)과 19:07 평가 미사용,
  빈 응답·한 행·페이지 상한 재수집 → 값·조정 기준일·revision 그대로·REBASE_REQUIRED, 그 상태 append 거부, 다음 갱신에
  정상 재수집 후에만 revision 2, 실패는 failed 집계, 기존 시계열 백필 실패는 ERROR, cont-yn=Y+빈 키·return_code 누락·
  cont-yn 이상·빈 첫/연속 페이지·키 반복·진행 없음·base_dt 뒤 날짜, 계약 위반 종목 ERROR·재실행 정상, 목록 스냅숏 미저장,
  빈/공백 state·state-only 위험·모르는 토큰(수집 대상 유지), r1 DB 자동 이전(시각 보수적 이전·revision 행·재오픈·시점 조회).
  기존 5-8은 "정정 값의 사용 가능 시각 = 새 revision 활성 시각, 최초 확보 시각은 기록으로 보존"으로 수정.
- `test_research_s1` 47→49(Q-R1 재현·같은 View 캐시 통과), `test_research_features` 86→91(Q-R2 재현·정상 대기·월요일 0시·확보 후 회복·일정 불명).
- 실측 원문 집계 u2에서도 2,544 / 257 / 2,287 / 84 / 82 그대로.
- 실제 r1 코드로 만든 DB(열린 백필 작업 포함)를 새 코드로 열어 자동 이전 → 같은 base_dt로 이어서 완료 → 매일 갱신 확인.
- 변이 확인 16종(정정 값에 이전 시각·시점 조회 무시·첫 페이지 시각 덮기·후보 검증 제거·조회 실패 표시 제거·
  재검증 중 append·Y+빈 키·return_code 누락·키 반복·진행 검사·빈 연속 페이지·빈 state·state 위험 축소·시장 재계산 제거·
  최근 주 항상 잘라냄·이전 시 available=fetched) — 모두 새 테스트가 잡음.
- `run_regression_tests.py --skip test_broker_order_status.py`: 29개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/`·`data/` 생성 없음.

### 변경하지 않은 것
- S1 가격 패턴·임계값, NO_TRADES 정책(f2), 완성 기준 160분(잠정), 주문 경로·운영 원장.

### 기존 DB 처리 방침
- 새 코드가 `data/research/research.sqlite3`(r1)를 열 때 자동으로 r2로 이전(한 트랜잭션, 한 번만). 열린 백필 작업은 이어서 진행.
- **이전 전에 옛 버전 수집 프로세스를 끝내야 함**(끝까지 두거나 Ctrl+C). 두 버전이 같은 DB를 동시에 쓰면 옛 프로세스가 오류로 멈춤.
- r1 시절 HISTORY_END·PAGE_CAP 시계열은 `backfill --recheck-shortfall`로 새 계약에서 다시 받아 검증 가능.

### 다음 작업
- 사용자: 전체 백필 완료 → 패치 적용(자동 이전) → `status` → 18:10 이후 `update` → `holidays` 후보 확인.
- 완성 시각 실측(장 마감 후 여러 시각·다음 거래일 대조), A4 스캔(시점 조회·REBASE_REQUIRED·기대 세션 검사·보류 사유 집계).

### 전달 파일
- 패치 0001 (fix), 0002 (docs)

## 2026-10-01 — A2 2차 재검토 #1·#2: 이전된 판의 활성 시각 복원, 시점 조회의 정합성 상태 (GPT 재검토 `e913df1`)

### 배경
GPT 재검토: 회귀 29/29·수집 95/95·지표 91/91·S1 49/49·동등성 18/18 확인, 응답 계약·state·시장 캐시·OVERDUE 해결 확인.
A4 시점 재평가에 영향을 주는 P1 2건.
- #1: r1 → r2 이전에서 보관된 과거 판의 활성 시각을 `MIN(fetched_at)`(첫 페이지 수신 시각)으로, 봉 available_at도 fetched_at으로 둠.
  revision 2 첫 페이지 10/1 19:00 → 저장 19:10인데 `as_of=10/1 19:05`가 revision 2의 새 가격을 반환, 판 구간도 겹침.
- #2: `as_of` 조회가 integrity를 항상 "AS_OF"로 돌려주고, 함께 오는 meta.integrity는 현재 값 — 9/30 정상 → 10/1 실패 →
  10/2 복구 뒤 10/1 재평가에서 당시 보류 상태가 사라짐.

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| #1 | `infra/research/store.py` (스키마 r2→r3) | r1에서 옮겨 온 판의 시각을 **r1이 저장 때마다 남긴 변경 기록**으로 복원: 판 활성 시각 = INIT/REBASE 기록 시각, 대체 시각 = 다음 판 활성 시각, 봉 available_at = max(판 활성, 수신 뒤 첫 저장 기록(INIT·REBASE·EXTEND·APPEND) 시각). 활성 시각이 증가하지 않는(구간 겹침) 판·기록 없는 판은 `time_basis=UNPROVEN` — 시점 조회에서 돌려주지 않고 `time_proof=UNPROVEN` 표시. 봉에 `time_basis`(OBSERVED/MIGRATED/UNPROVEN) |
| #1 | 같은 파일 | **이미 r2로 이전된 DB도** 열 때 같은 규칙으로 보정(r2→r3, 한 트랜잭션·한 번만). r1 DB는 r1→r2→r3로 이어서 이전 |
| #2 | 같은 파일 | 정합성 이력 `series_integrity`(재수집 실패 REBASE_REQUIRED·복구 OK를 시각과 함께). `research_series` 반환을 `query_mode`(CURRENT/AS_OF)·`integrity`(그 시각 상태)·`integrity_detail`·`time_proof`·`revision_info`(조회에 쓴 판의 기록)·`current_meta`(현재 메타)로 분리. 그 시각에 판이 없으면 integrity=NO_REVISION. r2→r3에서 VERIFY_FAILED 기록으로 이력 재구성 |
| 도구 | `tools/research_collect.py` | status에 time_basis·revision 사유별 개수 |
| 문서 | `docs/research_a_stage.md` | 정합성 이력·시점 조회 반환 구분, 기존 DB 이전 규칙(저장 기록 기반 복원·UNPROVEN 보류·r2 DB 보정) |

### 테스트 및 검증
- `test_research_collect` 94→105건(실측 파일 지정 시 106): r1 코드가 남기던 형식 그대로 만든 DB(분할 재계산 2회·FORWARD 추가)를
  열어 r3 이전 — 판 활성 시각 = 저장 기록 시각·구간 겹침 없음, **10/1 19:05(revision 2 첫 페이지 뒤·저장 전) 조회 → revision 1 가격**,
  9/30 19:02(최초 저장 전) → NO_REVISION, 봉 사용 가능 시각(재수집 봉 = 판 활성, 추가 봉 = 저장 기록), 이전 판 봉 같은 규칙,
  재오픈 불변, **이전 버전이 만든 r2 DB 보정**(19:00 → 19:10)·정합성 이력 재구성, 기록 없는 DB → UNPROVEN 보류,
  판 구간 모순 → 두 판 UNPROVEN. 9/30 정상 → 10/1 실패 → 10/2 복구 뒤 **10/1 20:00 조회 = REBASE_REQUIRED**, 실패 전·복구 후 OK,
  시점 revision_info와 current_meta 구분, 정합성 이력 시각.
- 실제 r1(166585b)·r2(e913df1) 코드로 만든 DB에서도 확인: 이전 r2는 revision 1 활성 시각이 첫 페이지 수신(19:06)이었고, 새 코드로 열면 저장 기록(19:14)으로 보정.
- 변이 확인 8종(이전 판 복원 제거·저장 기록 무시·구간 겹침 검사 제거·시점 조회에 현재 정합성·실패/복구 이력 미기록·이력 재구성 제거·UNPROVEN 봉 반환) — 모두 새 테스트가 잡음.
- 240만 봉 r1 DB 이전 약 7초(사용자 DB 약 600만 봉이면 20초 안팎, 처음 한 번).
- `run_regression_tests.py --skip test_broker_order_status.py`: 29개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/`·`data/` 생성 없음.

### 변경하지 않은 것
- 수집·재수집 검증·응답 계약·state·시장·주봉 규칙, S1 패턴·임계값, 주문 경로.

### 다음 작업
- 사용자: 패치 적용 후 `status`로 schema r3·time_basis(MIGRATED/UNPROVEN 개수)·revision 사유 확인 → `update`.
- A4 스캔: `research_series(as_of=스캔 시각)` + integrity·time_proof·기대 세션 검사, 보류 사유 집계.

### 전달 파일
- 패치 0001 (fix), 0002 (docs)

## 2026-10-01 — 백필 재실행 방지 (사용자 실측 status 후속)

### 배경
- 사용자 실측: 첫 전체 백필(9/30, DONE 1,733 / SHORTFALL 813) 뒤 `backfill`을 다시 실행해 두 번째 전체 작업(10/1 08:52, base_dt 20261001)이 생김.
- 원인: 열린 작업이 없으면 `backfill`이 새 전체 작업을 만드는데, 전달 안내에 "끝났다면 할 일 없음"이라고 잘못 적음(안내와 도구 동작 불일치).
- 데이터는 검증 경로로 처리돼 문제없음(같은 값은 EXTEND로 9/30 봉만 추가, 값이 바뀐 1종목만 REBASE). 다만 약 1.1만 호출·3시간을 다시 씀.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `tools/research_collect.py` | 끝난 작업만 있을 때 `backfill`은 아무것도 하지 않고 안내만 출력. 새 전체 작업은 `--new`(또는 `--recheck-shortfall`·`--codes`)일 때만. 처음 한 번(작업 없음)은 그대로 생성 |

### 테스트 및 검증
- `test_research_collect` 7-2(끝난 뒤 재실행 → 새 작업 없음·API 호출 0회), 7-3(`--new`일 때만 새 작업) 추가 — 105→107건.
- 사용자 DB 이력 재현: 실제 r1 코드(166585b)로 첫 백필 → 실제 r2 코드(e913df1)로 두 번째 백필(EXTEND·REBASE 1건) → 새 코드로 열어 r3 —
  판 활성 시각 = r1 INIT 기록, REBASE 판 구간 연결, 9/30 봉 사용 가능 시각 = 두 번째 작업 저장 기록, UNPROVEN 0.
- 회귀 29/29, 단타 동등성 18/18, 테스트 후 `git status` 깨끗.

### 변경하지 않은 것
- 수집·검증·저장 규칙.

### 다음 작업
- 사용자: 패치 적용 → `status`(schema r3) → 18:10 이후 `update` → `holidays`.

### 전달 파일
- 패치 0001 (fix: CLI·테스트·CHANGELOG)

## 2026-10-02 — A4-A: S1 앞으로의 신호 관찰 스캔·저장·일일 보고 (GPT 종합 검토 `3ccb362` 후속)

### 배경
- GPT 종합 검토(`3ccb362`): a2t·a2u 확인, 추가 P1 없음 → A4-A 진행. 작은 보완 2건을 이 패치에 함께.
  * status의 time_basis가 현재 봉(bar)만 집계 → 이전 판(bar_history)도 따로.
  * 조회가 필요 없는 backfill도 먼저 API 설정을 만듦 → 작업 필요가 정해진 뒤에 만들기.
- A4-A 지시: S1_BASE 그대로, update 뒤 스캔, 스캔 시각 입력만(as_of·integrity·time_proof·revision·기대 세션 봉·
  as-of 스냅숏·지수 장애 → 보류), 후보·탈락·보류 함께 저장, 신호 ID에 입력 해시 없음, 확정 기록 덮어쓰지 않음, 일일 보고서.

### 변경 내용
| 구분 | 파일 | 내용 |
|---|---|---|
| 스캐너 | `infra/research/s1_scanner.py` (새) | 신호일(완성 기준 160분, 잠정), `research_series(as_of=scan_at, start=)`, 데이터 보류(NO_SERIES·NO_REVISION·INTEGRITY·UNPROVEN·STALE), 지수 보류(지수 없이 평가 → RS·시장 UNKNOWN), as-of 스냅숏·장 마감 전 스냅숏 보류, 현재 정책으로 재분류, 입력 해시, final·actionable, 같은 run_key 건너뜀·`verify` 재계산 비교, 집계 |
| 저장 | `infra/research/scan_store.py` (새, 스키마 s1, 별도 파일) | scan_run(run_key·COMPLETE 유일, RUNNING→COMPLETE/FAILED/ABORTED, 시작 시 남은 RUNNING 정리), s1_eval(실행별 종목 판정 append-only, 사전 압축), s1_observation(신호 ID 대표 판정 — final 불변, 보류만 더 늦은 실행이 대체·이력), 한 트랜잭션 완료·ABORTED 실행 완료 거부 |
| 보고서 | `infra/research/scan_report.py` (새) | 요약·시장 환경·시장별·보류 사유·탈락 사유·후보 표(md·json), 수익 아님·잠정 완성 기준 고지 |
| 수집 저장소 | `infra/research/store.py` | `research_series(start=)`, 메타·revision·봉을 한 읽기 트랜잭션에서 읽고 revision 번호로 bar·bar_history 함께 조회(조회 중 REBASE에도 섞이지 않음), `snapshot_as_of(as_of)` |
| 수집기 | `infra/research/collector.py` | API 설정 오류(ResearchConfigError)는 종목 ERROR가 아니라 명령 중단 |
| 도구 | `tools/research_collect.py` | `scan [--at] [--verify]`, `update` 뒤 스캔(`--no-scan`), `--scan-db`·`--report-dir`, API 클라이언트 지연 생성(작업을 만들기 전·갱신 전엔 미리 확인), status time_basis current/history |
| 문서 | `docs/research_a_stage.md`, `README.md` | A4-A 절(실행·입력 규칙·저장·보고서·다음 단계) |

### 테스트 및 검증
- 새 `test_research_scan.py` 39건(가짜 데이터·임시 DB): 신호일 경계(18:09/18:10·주말·대체공휴일·달력 밖), PASS 2·FAIL·위험 자격 FAIL,
  데이터 보류 4종, NO_TRADES 보류, 참고 손절가·진입 상한·조건별 결과·증거 필드, 집계, 같은 시각 재실행 건너뜀·재계산 동일·결정적,
  **10/1 정정(REBASE)·새 스냅숏·늦은 봉 뒤에도 9/30 기록 재계산 동일**, 확정 기록 유지·보류 기록 대체(이력·actionable 0)·
  과거 시각 재현이 늦은 기록을 되돌리지 않음, **지수 REBASE_REQUIRED·지수 오래된 봉 → 해당 시장 종목 후보 아님**,
  스캔 뒤 스냅숏 미사용·장 마감 전 스냅숏 보류·스냅숏 없음 FAILED, **커밋 직전 중단 → 아무것도 안 남고 ABORTED·다음 실행 정상**,
  강제 종료 RUNNING 정리, ABORTED 실행 완료 거부, COMPLETE 유일, CLI scan·보고서·verify·update 실패해도 스캔·`--no-scan`,
  status current/history, 설정 없이 끝난 backfill 통과·조회 필요 시 작업 만들기 전 오류, import 경계·운영 폴더 무변경.
- 변이 확인 15종(latest_snapshot 사용·정합성/시각 입증/오래된 봉 검사 제거·지수 보류 무시·현재 값 사용·final 항상 1·
  확정 기록 덮어씀·완료 건너뛰기 제거·완료 표시 상태 검사 제거·스냅숏 검사 제거·과거 재현 대체·중단 표시 제거·
  신호일 장 마감 기준·actionable 항상 1) — 모두 새 테스트가 잡음.
- 규모: 2,500종목(무작위 시세) 스캔 약 7초, 실행당 저장 약 3MB, verify 일치.
- `test_research_collect` 107/107(실측 원문 포함 108), 지표 91, S1 49.
- `run_regression_tests.py --skip test_broker_order_status.py`: 30개 전부 통과. 단타 원본 동등성 18/18. 테스트 후 `git status` 깨끗, `commands/`·`data/`·`reports/` 생성 없음.

### 변경하지 않은 것
- S1 조건·기준값, 수집·검증 규칙, 주문 경로·운영 원장.

### 다음 작업
- 사용자: 패치 적용 → `status`(schema r3, time_basis current/history의 UNPROVEN, integrity) → 18:10 이후 `update`(자동 스캔) → 보고서 확인.
- A4-B 과거 일괄 스캔(과거 달력 확인 후), A5 다음날 확인·이후 움직임.

### 전달 파일
- 패치 0001 (feat), 0002 (docs)

## 2026-10-02 — 저장소 r4: 같은 초 저장 봉의 UNPROVEN 오판정 수정 (사용자 실측 status 후속)

### 배경
- 사용자 실측(10/2 10:10, a4a 적용 뒤 첫 `status`): schema r3, integrity 전부 OK, 그러나 현재 봉 `UNPROVEN 199`.
- 원인(실제 r1 `166585b`·r2 `e913df1` 코드로 재현): r3 시각 복원이 봉 수신 시각(초 올림 — r2 저장 규칙)과 저장 기록 시각(초 내림)을
  그대로 비교. 한 페이지로 끝나는 짧은 시계열(2024년 중반 이후 상장 등)은 두 번째 백필에서 9/30 봉을 받은 같은 초에 저장(EXTEND)돼
  저장 기록(:17)이 수신(:18)보다 앞서 보여 "저장 기록 없음 → UNPROVEN"으로 잘못 남음. 여러 페이지 시계열은 저장까지 몇 초 걸려 영향 없음.
- 영향: time_proof가 시계열(revision) 단위라 이 199종목은 A4-A 스캔에서 매일 데이터 보류(UNPROVEN)로 빠짐.
- 덧붙여 확인: `revision_reasons`의 `BACKFILL:… 2` = REBASE 1건 + 9/30 첫 백필 때 완성 봉이 없던(9/30 상장, 장중 봉만) 1종목의 INIT.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/research/store.py` (스키마 r3→r4) | 이전 판 복원에 같은 초 규칙: '수신 시각 − 1초 이후 첫 저장 기록'을 그 봉의 저장으로 보고 사용 가능 시각 = max(판 활성, 수신, 저장 기록). r3→r4 단계에서 이미 r3인 DB의 MIGRATED·UNPROVEN 봉만 다시 계산(이후 OBSERVED 봉은 그대로). r2→r3 단계도 같은 규칙 |
| `docs/research_a_stage.md` | 기존 DB 이전에 r3→r4 설명 |

### 테스트 및 검증
- `test_research_collect` 13-12(같은 초 수신·저장 → MIGRATED, 사용 가능 시각 = 수신), 13-13(이미 r3인 DB의 UNPROVEN → MIGRATED, r3 뒤 OBSERVED 봉 불변) 추가 — 109건(실측 원문 포함 110).
- 실제 r1·r2 코드로 만든 DB(1페이지 시계열·9/30 상장 종목 포함)를 r3로 연 상태와 r2 상태 둘 다 새 코드로 열어 UNPROVEN 0, 결과 동일.
- 변이 확인 4종(같은 초 허용 제거·r4가 OBSERVED까지 재계산·사용 가능 시각에서 수신 제외·r3→r4 단계 제거) 모두 잡힘.
- `run_regression_tests.py --skip test_broker_order_status.py`: 30개 전부 통과. 단타 원본 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 수집·검증·스캔 규칙, S1, 주문 경로.

### 다음 작업
- 사용자: 패치 적용 → `status`(schema r4, time_basis current UNPROVEN 없음) → 18:10 이후 `update`(자동 스캔).

### 전달 파일
- 패치 0001 (fix: 저장소·테스트·문서·CHANGELOG) — 이후 A4-A 보완 R1~R5 묶음(`swing-a4b`)에 포함해 전달

## 2026-10-02 — A4-A 보완 R1~R5 (GPT 재검토 `f837185` + 사용자 실측 status)

### 배경
GPT가 사용자 `status`와 `f837185`를 함께 검토: 회귀 30/30·스캔 39/39·동등성 18/18 통과, 그러나 테스트가 놓친 경로 5건 재현.
- R1(P1): `_LazyClient.__getattr__`가 `_ensure_client()` 함수 안에 들어가 있음(들여쓰기 실수) → 일반 CLI의 universe·update·조회 backfill이
  `AttributeError: fetch_page`. 테스트는 가짜 클라이언트를 직접 넘겨 래퍼를 우회.
- R2(P1): r3 보정이 수신(초 올림)·저장 기록(초 내림) 시각을 그대로 비교 → 정상 저장 봉이 UNPROVEN. 이미 r3인 DB의 보정 경로 필요,
  증거가 있는 봉만 보정하고 DB 백업 후 진행. (같은 날 앞서 만든 r4 패치를 이 묶음에 포함 — 사용자는 아직 미적용)
- R3(P2): 지표 판정이 UNKNOWN이어도 final=1 → 데이터 정정 뒤 PASS가 대표 기록을 대체하지 못함.
- R4(P2): 보고서 저장 실패 뒤 같은 시각 재실행이 보고서 없이 성공(건너뜀) 처리.
- R5(P2): run_key에는 분류 정책이 있는데 실행 ID에는 없어 정책만 바꾸면 UNIQUE 충돌.

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| R1 | `tools/research_collect.py` | `__getattr__`를 `_LazyClient` 클래스 안으로(밑줄 속성은 넘기지 않음) |
| R2 | `infra/research/store.py` (r4) | 저장 근거 규칙 `find_write_evidence`: **같은 revision**의 저장 기록(또는 revision 표시 없는 APPEND) 중 '수신 − 1초' 이후 첫 기록. 근거 없는 봉은 UNPROVEN 유지. 스키마를 올리기 전 SQLite 백업 API로 자동 백업(`<파일>.bak-<옛 버전>-<시각>`) |
| R2 | `infra/research/store_inspect.py` (새), CLI `inspect-unproven` | 읽기 전용(mode=ro, 이전·백업 없음)으로 UNPROVEN 봉을 종목·revision·수신 시각별로 묶어 저장 기록과 대조, 판정(PROVABLE_SAME_SECOND·PROVABLE·REVISION_UNPROVEN·NO_EVIDENCE)·날짜별 개수 |
| R3 | `infra/research/s1_scanner.py`, `scan_store.py` | final = 입력 모두 정상 **그리고 판정 PASS/FAIL**. UNKNOWN은 이후 정해진 판정이 대체 |
| R4 | `tools/research_collect.py`, `scan_store.load_run` | 건너뛸 때 보고서(md·json)가 없으면 저장된 실행으로 보고서만 다시 만듦(재계산 없음). 보고서가 확보돼야 종료 코드 0 |
| R5 | `infra/research/scan_store.py` | 실행 ID에 run_key 해시(10자리) |
| 문서 | `docs/research_a_stage.md` | 저장 근거 규칙·자동 백업·inspect-unproven, final 정의, 보고서 복구·실행 ID |

### 테스트 및 검증
- `test_research_collect` 109→113(실측 원문 포함 114): 7-4 팩토리만 가짜로 바꾼 **실제 `_LazyClient` 경로**로 universe·backfill 조회,
  13-14 읽기 전용 점검(같은 초 근거 PROVABLE_SAME_SECOND / 기록 없음·다른 revision 기록 NO_EVIDENCE, 점검은 이전·백업 안 함),
  13-13 근거 있는 봉만 MIGRATED·근거 없는 봉 UNPROVEN 유지, 13-15 이전 전 백업(옛 상태 그대로), 13-16 최신이면 백업 안 함.
- `test_research_scan` 39→43: 4-4 NO_TRADES UNKNOWN(final=0) → 정정 뒤 PASS가 대표 기록 대체, 8-6 보고서 저장 실패 → 재실행이 재계산 없이
  보고서만 복구, 8-7 복구 보고서 = 새로 계산한 보고서, 8-8 분류 정책만 다른 실행 둘 다 완료.
- 실제 r1(`166585b`)→r2(`e913df1`)→r3(`f837185`) 코드로 만든 DB: `inspect-unproven` → 1봉 PROVABLE_SAME_SECOND, 새 코드로 열면
  백업 생성 후 r4·UNPROVEN 0.
- 변이 확인 7종(R1 래퍼·R2 revision 무관 근거·백업 제거·근거 없이 일괄 해제·R3 UNKNOWN 확정·R4 복구 제거·R5 해시 제거) 모두 잡힘.
- `run_regression_tests.py --skip test_broker_order_status.py`: 30개 전부 통과. 단타 원본 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- S1 조건·기준값, 수집·검증 규칙, 주문 경로.

### 다음 작업
- 사용자: 패치 적용 → **먼저 `inspect-unproven`**(읽기 전용) 결과 확인 → `status`(자동 백업 후 r4) → 18:10 이후 `update`(자동 스캔) → 보고서.
- 백업 파일은 r4 결과 확인 뒤 지워도 됨.

### 전달 파일
- 패치 0001 (r4), 0002 (fix: R1~R5), 0003 (docs)

## 2026-10-02 — A4-A 잔여 B1·B2: 저장 근거의 판 귀속(r5), 이전 버전 UNKNOWN 확정 기록 보정(s2) (GPT 재검토 `c308ccb`)

### 배경
GPT가 `c308ccb`(R1~R5 반영)를 검토: R1·R4·R5 해결 확인, 회귀 30/30·수집 114/114·스캔 43/43·동등성 18/18, 읽기 전용 점검 전후
DB 해시 동일, 자동 백업 확인. 기존 데이터와 관련된 잔여 2건.
- B1(R2 잔여): `find_write_evidence`가 해당 revision 기록에 **revision 없는 모든 저장 기록(APPEND)** 을 합쳐, 끝난 판(revision 1,
  10/2 19:10 종료)의 봉을 다음 판의 APPEND(10/3 19:00)로 입증 → MIGRATED·available_at 10/3 19:00. "같은 revision 근거만"
  계약 위반. 이미 r4로 보정된 DB의 재점검 경로도 필요.
- B2(R3 잔여): 새 기록은 UNKNOWN이면 final=0이지만, 이전 버전(`f837185`)이 남긴 `UNKNOWN + final=1` 대표 기록은 그대로 →
  데이터 정정 뒤 PASS가 대체하지 못함(실제 `f837185`로 만든 DB로 재현).
- 같은 날 사용자 운영 DB: r4 전환 완료, `inspect-unproven` UNPROVEN 0(199봉 모두 같은 초 근거로 보정).

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| B1 | `infra/research/store.py` (r5) | `revision_timeline`(판 순서·활성 시각) + `attribute_writes`: revision이 적힌 기록은 그 판, 없는 예전 기록은 기록 순서상 바로 앞 INIT·REBASE의 판이고 그 판의 활성 구간 [활성, 다음 판 활성) 안일 때만. 앞 활성 기록 없음·revision 없는 활성 기록 뒤·구간 밖·경계 같은 초는 귀속 불가(근거 아님). `find_write_evidence`는 같은 판으로 귀속된 기록만 |
| B1 | 〃 | 봉 판정 `plan_migrated_bar` 하나로 보정·점검 공용. r3·r4 DB를 열면 백업 후 MIGRATED·UNPROVEN 봉 재계산(r5) — 잘못 입증된 봉 UNPROVEN, 바뀐 봉 수를 출력·meta `recheck_r5`에 |
| B1 | 〃 `append_forward` | 새 APPEND·VERIFIED 기록에 revision 기록 |
| B1 | `infra/research/store_inspect.py` | 읽기 전용 점검에 `recheck_migrated`(이미 MIGRATED인 봉 중 바뀔 것), `unattributed_writes`(판을 정할 수 없는 예전 기록), `scan_db`(관찰 DB final 보정 대상) 추가 |
| B2 | `infra/research/scan_store.py` (s2) | `final_rule` 공용 함수. s1 DB를 열면 백업 후 대표 기록 중 현재 규칙을 만족하지 않는 final=1을 final=0으로 + `obs_audit`(바꾸기 전 run·판정·입력 상태, 사유). PASS·FAIL 확정 기록·`scan_run`·`s1_eval`은 그대로. 읽기 전용 미리 보기 `inspect_final_reset` |
| B2 | `infra/research/s1_scanner.py` | final = `final_rule(...)`, 실행 context에 `final_rule` 기록. `verify`는 저장된 행에 현재 규칙을 적용해 비교, 이전 규칙 차이는 `final_rule_changed`로 따로 |
| CLI | `tools/research_collect.py` | 연구 DB 재점검·관찰 DB 이전 시 백업 경로·집계 출력, `inspect-unproven`이 관찰 DB 미리 보기도 |
| 문서 | `docs/research_a_stage.md`, `README.md` | r5 판 귀속 규칙, s2 이전·obs_audit·final 규칙 |

### 테스트 및 검증
- `test_research_collect` 114→119(실측 원문 포함 120): 5-4b 새 APPEND에 revision, 13-17 판 귀속 규칙(명시·순서·경계 같은 초·앞
  기록 없음·revision 없는 활성 기록 뒤), 13-18 GPT 재현(다음 판 APPEND로 끝난 판 봉 입증 안 함, 그 APPEND가 속한 판 봉은 그대로),
  13-19 r4 DB 읽기 전용 점검이 MIGRATED→UNPROVEN 1봉을 미리 보여 줌(DB 해시 그대로·백업 없음), 13-20 r4 DB 열기 → 백업·r5·집계,
  13-21 다시 열면 재점검·백업 없음.
- `test_research_scan` 43→49: 11-1 s1 DB 읽기 전용 미리 보기(풀 기록 1건·DB 그대로), 11-1b CLI `inspect-unproven`의 관찰 DB 미리 보기,
  11-2 열면 백업·s2·UNKNOWN만 final=0·감사 이력·PASS/FAIL 4건 유지·당시 s1_eval 보존, 11-3 정정 뒤 최신 스캔 PASS가 대체(대체 1회),
  11-4 이전 규칙 실행 재현 검증 identical·final_rule_changed 1, 11-5 다시 열면 보정·백업 없음.
- **실제 이전 버전 코드로 재현**:
  * r1(`166585b`) 백필·APPEND → r2(`e913df1`) 같은 초 EXTEND·REBASE·APPEND → r3(`f837185`) → r4(`c308ccb`) 순서로 만든 DB.
    한 종목은 revision 1의 9/30 APPEND 기록이 없는 상태(GPT 조건): r3는 다음 판 REBASE(10/1 19:10), r4는 다음 판 APPEND
    (10/2 19:00)로 입증. 새 코드 `inspect-unproven`(해시 그대로) → MIGRATED→UNPROVEN 1봉, 열면 백업 후 r5에서 그 봉만 UNPROVEN,
    같은 초 EXTEND 봉(실측 199봉 유형)을 포함한 나머지 13봉은 그대로. r3 DB를 바로 r5로 열어도 같은 봉 판정(UNPROVEN 봉의
    미사용 available_at만 이전 값 유지).
  * `f837185` 스캐너로 관찰 DB(UNKNOWN·final=1) 생성 → `c308ccb`로 열어도 그대로(재현) → 새 코드로 열면 백업·s2·감사 1건,
    정정 뒤 스캔 PASS가 대체(대체 1회), 이전 실행 재현 검증 identical·final_rule_changed 1.
- 변이 확인 10종 모두 잡힘: 판 귀속 무시·활성 구간 검사 제거·새 APPEND revision 제거·r4 재점검 제거·점검의 MIGRATED 재점검 제거,
  s1→s2 보정 제거·PASS/FAIL까지 해제·감사 이력 제거·verify 저장 final 그대로 비교·관찰 DB 백업 제거.
- `run_regression_tests.py --skip test_broker_order_status.py`: 30개 전부 통과. 단타 원본 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- S1 조건·기준값, 수집·검증 규칙, 주문 경로. r3 뒤 새로 저장된 OBSERVED 봉. 이전 실행의 판정 기록(`scan_run`·`s1_eval`·집계).

### 다음 작업
- 사용자: 패치 적용 → `inspect-unproven`(읽기 전용: `recheck_migrated`·`unattributed_writes`·`scan_db` 확인) → `status`(백업 후 r5) →
  18:10 이후 `update`(자동 스캔, 관찰 DB가 있으면 백업 후 s2) → 보고서.
- 백업 파일(`.bak-r4-*`, `.bak-s1-*`)은 결과 확인 뒤 지워도 됨.

### 전달 파일
- 패치 0001 (fix: B1·B2), 0002 (docs)

## 2026-10-02 — A4-A 계산 계약: 계산 조건이 다르면 별도 실행·별도 대표 기록 (관찰 DB s3, GPT 재검토 `016907a`)

### 배경
GPT가 `016907a`(B1·B2 반영)를 검토: B1·B2 해결, 회귀 30/30·수집 120/120·스캔 49/49·동등성 18/18, 운영 DB r5 전환 정상(재점검
5,116,636봉 변경 없음) — 기본 설정으로 관찰 기록을 쌓기 시작해도 됨. 추가 P2 1건:
- `S1Scanner.run_key()`에 전략 설정·분류 정책은 있지만 lookback·지표/시장 계산 버전 등이 빠져 있음. 같은 시각·같은 DB에서
  lookback 300 실행(000100 PASS) 뒤 lookback 100(직접 계산하면 UNKNOWN)으로 저장 실행하면 기존 실행을 돌려주며
  `SKIPPED_ALREADY_COMPLETE`. 기본값 일일 운영에는 장애가 없지만 계산층을 바꾸면 비교 결과가 섞일 수 있음.
- 지시: 계산 계약 해시(전략 설정·분류 정책·지표/시장 계산 버전·lookback·완성 봉 지연·거래일 달력 버전)를 실행 키·context에
  저장, 같은 계약은 건너뛰고 다르면 별도 실행, 대표 기록도 계약을 섞지 않음, 입력 해시는 증거 필드 유지, 기존 기록은 덮어쓰지 않음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/research/s1_scanner.py` | 계산 계약 = 전략·설정 해시·분류 정책·지표/시장 계산 버전·lookback·완성 지연(초)·달력 내용 해시(`calendar_version`)·final 규칙·스캔 입력 규칙 버전(`SCAN_RULES_VERSION="sr1"`)·지수 ID → `contract_hash`(12자리). 실행 키 끝 `|c:<해시>`, context에 `contract_hash`·`contract`. `verify`는 계약이 다르면 비교하지 않고 `contract_match=False`·`contract_diff`, 계약 기록 전 실행은 `contract_match=None`으로 비교 |
| `infra/research/scan_store.py` (s3) | `scan_run`·`s1_observation`에 contract_hash 열. 대표 기록 키 `S1|전략|c:<계약>|종목|신호일`. s2(또는 s1) DB를 열면 백업 후 열 추가 — 기존 행은 NULL(계약을 추정하지 않음)로 별도 묶음 보존. `observations(contract_hash=…)` 필터 |
| `infra/research/scan_report.py` | 보고서 머리말에 계산 계약(해시·lookback·달력·final·스캔 규칙). 계약 기록 전 실행은 그렇다고 표시 |
| `tools/research_collect.py` | 스캔 결과 출력에 contract_hash, 관찰 저장소 이전 시 `[관찰 저장소 이전] {...}` |
| 문서 | `docs/research_a_stage.md` 계산 계약·s3 이전, `README.md` |

### 테스트 및 검증
- `test_research_scan` 49→55: 12-1 GPT 재현(lookback 300 PASS / 100 직접 계산 UNKNOWN), 12-2 lookback 100 저장 실행이 별도 실행으로
  완료(실행 키·context·실행 행에 계약), 12-3 대표 기록이 계약별로 따로(300의 PASS 확정 기록과 100의 UNKNOWN이 섞이지 않음, 신호 ID에
  계약·입력 해시 없음), 12-4 같은 계약 재실행 건너뜀(검증 일치)·다른 계약 스캐너의 검증은 비교 없이 `contract_diff=["lookback_sessions"]`,
  12-5 완성 지연·달력 휴장일·분류 정책·lookback·지표·시장 버전이 바뀌면 해시가 다르고 휴장일 이름만 바뀌면 같음, 12-6 보고서 표시.
- 11절(B2) 갱신: 이전 버전 DB를 열면 s1→s2→s3(백업 1회), 계약 기록 전 실행 1·대표 기록 9는 NULL로 표시, 정정 뒤 새 스캔의 PASS는
  그 계약의 대표 기록으로 들어가고 계약 기록 전 대표 기록(UNKNOWN·final=0)은 섞지 않고 그대로 보존. 계약 기록 전 실행의 재현 검증은
  `contract_match=None`·identical.
- **실제 이전 버전 코드로 재현**: `016907a`에서 lookback 300 → 100 저장 실행이 같은 실행(`SKIPPED_ALREADY_COMPLETE`) 반환을 확인 →
  그 DB(s2)를 새 코드로 열면 백업·s3(계약 기록 전 실행 1·대표 기록 9), lookback 300·100이 각각 별도 실행·별도 대표 기록(000100: PASS/
  UNKNOWN), 계약 기록 전 묶음은 그대로. `f837185`가 만든 s1 DB도 s1→s3 한 번에(final 해제 1·감사 1·계약 NULL).
- 변이 확인 10종 모두 잡힘: 실행 키·대표 기록 키의 계약 제거, 계약에서 lookback·완성 지연·달력 제거, 달력 해시가 휴장일 이름 포함/휴장일
  무시, verify 계약 확인 제거, s3 이전이 기존 기록에 계약을 채움, 관찰 DB 백업 제거.
- `run_regression_tests.py --skip test_broker_order_status.py`: 30개 전부 통과. 수집 120/120(실측 원문 포함). 단타 원본 동등성 18/18.
  `git status` 깨끗.

### 변경하지 않은 것
- S1 조건·기준값, 기본 lookback(300)·완성 지연(160분), 입력 해시 구성, 수집 저장소(r5), 주문 경로. 기존 실행·대표 기록 값.

### 다음 작업
- 사용자: 오늘 18:10 `update` **전에** 적용 권장(그래야 첫 기록부터 계약이 붙음). 이미 이전 버전으로 돌렸다면 적용 뒤 `scan`을 한 번
  실행해 같은 신호일의 계약 기록을 만듦(다음 개장 전이면 actionable=1).
- 첫 보고서 점검 → A5-1(다음 거래일 09:05 가격 기록 — 10/2 신호의 다음 거래일은 10/6, 10/5 개천절 대체공휴일) → A5-2.
- 18:10 완성 기준 실측(같은 날짜 봉을 늦은 시각·다음 거래일과 비교)은 별도로 계속.

### 전달 파일
- 패치 0001 (fix: 계산 계약·s3), 0002 (docs)

## 2026-10-02 — A5-1: 다음 거래일 개장 + 5분 가격 기록 (조회만, GPT 재검토 `71b78e3` 지시)

### 배경
GPT가 `71b78e3`(계산 계약)을 검토: P2 해결·추가 차단 결함 없음(회귀 30/30·수집 120·스캔 55·동등성 18/18). 계약별 대표 기록 분리·
기존 계약 미추정 유지. 최종 목표는 24시간 상시 실행 스윙 프로그램 — 단계: **A5-1**(다음 거래일 가격 기록) → A24-A(조회 전용
상시 실행 관리자) → A5-2 → S1 실행 명세 → A24-B(모의매매). A5-1 지시:
- 대상 계약 하나를 운영 설정에서 명시, 전날 그 계약의 PASS·final=1·개장 전 확보 후보만, 후보 목록 확정(signal_id·run_id·계약),
  계약 변경 시 섞지 않음(같은 종목 중복 후보 금지), 가격 기준이 바뀌면 진입 상한 비교 보류.
- 실행 시각 = 거래일 개장 + 5분(특수 개장일은 달력), 가격 TR·원천 필드 먼저 확인, 늦으면 실제 조회 시각으로(09:05로 간주하거나
  일봉으로 채우지 않음), 정상·실패·지연·상한 초과·거래 불가 구분, 재시작해도 중복 없음, 관찰 가격 ≠ 가정 체결가격(체결 표시 안 함).

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/research/open_check.py` (새) | 후보 확정(`candidate_set`·`candidate`, D마다 하나 — 대상 계약·PASS·final=1·actionable=1·스캔 시각 < D 개장, 신호일 종가·진입 상한·손절가·revision 저장, NO_SCAN/NO_CANDIDATES 구분), 가격 확인(`price_check`, 후보·확인 종류마다 한 행 — ON_TIME/LATE/MISSED, OK/FETCH_FAILED/PARSE_FAILED/NOT_RUN, NOT_TRADABLE > BASIS_CHANGED > ABOVE_CAP > BELOW_STOP > WITHIN_CAP, 가정 체결가격은 상한 이내일 때만·규칙 이름), 실행 기록(`check_run`), 보고서(md·json). 저장 `data/research/a5_checks.sqlite3` |
| `infra/research/research_config.py` (새), `config/research.yaml` (새) | 대상 계약(`current` 또는 12자리 해시)·확인 시각(개장 + 5분)·ON_TIME 허용(120초). 모르는 키·범위 밖 값은 오류 |
| `infra/research/kiwoom_readonly.py` | 시세 조회 `fetch_body`(ka10001만, 목록 조회 TR 3개와 분리) — 요청·수신 시각·시도 횟수. 재시도·재인증 경로를 목록 조회와 공유 |
| `infra/research/s1_scanner.py` | `build_contract()`(스캐너·A5의 'current' 해석이 같은 함수) |
| `tools/research_collect.py` | `open-check`(`--day`·`--no-wait`·`--max-wait-min`·`--a5-db`·`--config`) — 목표 30분 전 안이면 대기, 휴장일 안내. 클라이언트는 조회할 후보가 있을 때만 생성 |
| `tools/probe_price_sources.py` (새), `tools/probe_market_data.py` | 시세 원천 확인 프로브(장중): ka10001 필수 필드·시각 필드 후보, ka10003(체결)·ka10004(호가) 후보 TR. 조회 전용·모의 도메인·토큰 가림 |
| 문서 | `docs/research_a_stage.md` A5-1 절, `README.md` |

### 테스트 및 검증
- `test_research_open_check.py` (새, 30건): 설정 검증·목표 시각(09:05·특수 개장일 10:05·휴장일 거부)·'current' = 스캐너 계약,
  후보 선택(다른 계약·계약 기록 전·FAIL·개장 뒤 확정·final 아님 제외, 종목당 하나)·확정 내용, 판정 8종(상한 이내·초과·손절가 이하·
  기준 변경·거래량 0·상한가·조회 실패·필드 없음), 시각(ON_TIME/LATE·실제 요청 시각·지연 초), 가정 체결가격 구분, 본문 보존·가림,
  재실행 중복 없음, 확정 뒤 관찰 기록이 바뀌어도 목록 유지, 계약 변경 시 기존 D 유지, 목표 전 거부, 중단 후 재개(남은 후보만 LATE),
  다음 날 실행 MISSED(조회 0), 정규장 종료를 넘기는 실행, NO_SCAN/NO_CANDIDATES, 보고서, 클라이언트 TR 분리·429 재시도,
  CLI(대기·--no-wait·휴장일·설정 오류·후보 없으면 .env 없이도 확정·**실제 `_LazyClient` 경로**), 프로브 분석·실행 경로, import 경계.
- 변이 확인 14종 중 13종 단독 탐지: 다른 계약 포함·final/개장 전 조건 제거·기준 변경/거래량 검사 제거·늦은 조회 ON_TIME·
  정규장 뒤 조회·목표 전 실행·기록된 후보 재조회·가정 체결 확대·본문 가림 제거·시세 TR 확대·특수 개장일 무시. '매번 다시 고름'과
  '확정 목록 덮어쓰기'는 각각 단독으로는 다른 쪽이 막아 결과가 같고(이중 방어), 둘을 함께 넣으면 3-5b·3-6이 잡음.
- `run_regression_tests.py --skip test_broker_order_status.py`: 31개 전부 통과(새 파일 포함). 수집 120(실측 원문 포함)·스캔 55.
  단타 원본 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- S1 조건·기준값·계산 계약, 수집·스캔 저장소 스키마(r5·s3), 주문 경로(이번 단계는 주문 연결 없음).

### 다음 작업
- 사용자: 오늘 장중(15:20 전)에 `python tools/probe_price_sources.py` 1회 → 요약 공유(필드·시각 후보 확인). 18:10 이후 `update`.
  10/6(화) 09:00~09:05 사이 `open-check` 실행(목표 09:05까지 대기 후 기록) → 보고서 공유.
- A24-A: 조회 전용 상시 실행 관리자(마감 갱신·스캔·아침 가격 기록 자동, 재시작·중복 실행 방지·휴일 대기).

### 전달 파일
- 패치 0001 (feat: A5-1), 0002 (docs)

## 2026-10-02 — A5-1 보완 R1~R3 + 가격 원천 실측 반영 (GPT 재검토 `14eb8c0`·사용자 프로브)

### 배경
GPT가 `14eb8c0`(A5-1)을 검토: 회귀 31/31·수집 120·A5-1 30/30·동등성 18/18, 기본 구조는 적절. 상시 실행 관리자에 붙이기 전 보완 3건 재현.
- R1: `select_candidates()`가 `scan_at`만 개장과 비교 — scan_at 9/30 19:30·실제 저장 완료 10/1 09:10인 실행의 PASS 8건이 후보(OK)로 들어감.
  "과거 데이터를 같은 기준으로 계산할 수 있음"과 "개장 전에 실제로 확보됨"은 다름. 대표 기록의 run_id도 그 조건을 만족해야 함.
- R2: 마감 검사가 `fetch_body()` 호출 전에만 있어, 15:29:59 진입 → 재시도 요청 15:30:04 → 수신 15:30:05가 LATE·WITHIN_CAP·가정 체결로 기록됨.
  요청 직전마다 검사하고, 마감 뒤 받은 응답은 원문·시각만 보존(유효한 장중 확인·가정 체결 아님). 요청은 제때·응답 확보가 늦은 경우도 구분.
- R3: 실행 ID가 초 단위 시각뿐이라 같은 초 재실행 시 `IntegrityError: UNIQUE constraint failed: check_run.run_id`.
같은 날 사용자 장중 프로브(`tools/probe_price_sources.py`, 11:40, 3종목 × 2회):
- ka10001: cur_prc·base_pric·open/high/low_pric·upl_pric·lst_pric·trde_qty 확인. **가격 시각 필드 없음**. base_pric = 전일 종가
  (pred_pre = 현재가 − 기준가, 275,750 − 276,000 = −250).
- ka10003(체결정보): `cntr_infr` 첫 행이 최근 체결 — tm(HHMMSS)·cur_prc·stex_tp(KRX). 요청 11:40:24.04에 체결 11:40:23.
- ka10004(주식호가): bid_req_base_tm·sel_fpr_bid·buy_fpr_bid·10단계 호가.
- 같은 날 `open-check`(11:41): 대상일 10/2·신호일 10/1에 대상 계약 스캔이 없어 NO_SCAN 0건 확정 — 의도한 동작.

### 변경 내용
| ID | 파일 | 내용 |
|---|---|---|
| R1 | `infra/research/scan_store.py` (관찰 저장소 s4) | `scan_run.committed_at`: 판정·대표 기록·COMPLETE 커밋이 **끝난 뒤** 잰 시각을 초 올림(실제 저장 완료보다 이르지 않음). 커밋 전 중단이면 비어 있음. s3 DB는 백업 후 열 추가(기존 실행 NULL) |
| R1 | `infra/research/open_check.py` | 후보 원천 실행 = COMPLETE·대상 계약·scan_at < 개장·**저장 완료 상한 < 개장**(committed_at, 없으면 finished_at + 1시간 — 저장 트랜잭션 수 초·잠금 대기 30초라 보수적). 대표 기록의 run_id가 그 실행이어야 함. 제외한 실행·근거(`commit_basis`)를 후보 목록 원천에 기록 |
| R2 | `infra/research/kiwoom_readonly.py` | `fetch_body(..., not_after=)`: 인증·호출 간격·재시도 대기 뒤 **요청 직전마다** 마감 검사 → 지났으면 요청하지 않고 `DeadlinePassed`(이미 보낸 요청 수 포함). 시도 횟수는 실제 보낸 요청 수 |
| R2 | `infra/research/open_check.py` | 첫 요청 전 마감 → MISSED, 재시도 중 마감 → FETCH_FAILED·MISSED, 마감 전 요청·마감 뒤 수신 → AFTER_CLOSE(원문·시각 보존, 관찰가·판정·가정 체결 없음, 보조 조회 없음). 시각 ON_TIME / LATE_RESPONSE(요청은 제때·수신 늦음) / LATE, 응답 소요(ms) 기록 |
| R3 | `infra/research/open_check.py` | 실행 ID = 대상일·확인 종류·DB가 같은 트랜잭션에서 발급하는 시도 번호(`a5_20261006_open5m_a2`). 후보·확인의 중복 방지 키는 그대로 |
| 실측 | `infra/research/open_check.py` (A5 저장소 a2), `kiwoom_readonly.py` | ka10001 판정 뒤 보조 조회: ka10003 최근 KRX 체결 시각(`source_time`)·체결가·지연(초), ka10004 호가 기준 시각·최우선 매도/매수호가. 판정·가정 체결가격에는 쓰지 않음, 실패·형식 오류·마감은 `extra_json` 상태만. 시세 조회 허용 TR에 ka10003·ka10004(실측 확인). a1 DB는 백업 후 열 추가. 보고서에 최근 체결 시각·최우선 매도호가 |
| CLI | `tools/research_collect.py` | A5 저장소 이전 시 백업 경로 출력 |
| 문서 | `docs/research_a_stage.md` | 실측 결과, 개장 전 저장 완료 조건, 마감·시각 구분, 실행 ID, 저장소 s4·a2 |

### 테스트 및 검증
- `test_research_open_check` 30→42: 4b-1 R1 재현(개장 뒤 저장 → NO_SCAN·제외 기록), 4b-2 개장 뒤 저장 실행이 바꾼 대표 기록은 제외·개장 전
  실행 후보는 유지, 4b-3 커밋 시각 없는 이전 실행(finished_at + 1시간: 포함/제외), 4b-4 R2 재현(**실제 클라이언트**: 429 → 재시도 직전
  마감 → 요청 안 함·가정 체결 없음), 4b-5 마감 뒤 수신 → AFTER_CLOSE(원문 보존), 4b-6 LATE_RESPONSE, 4b-7 보조 조회 마감 시 요청 안 함,
  4b-8 R3 같은 초 3회 실행(시도 번호 ID·추가 조회 0), 4-3b CLI 같은 초 재실행으로 보고서 복구(추가 조회 0), 3-2b 원천 시각·호가,
  4-1b a1 → a2, 6-3 **10/2 실측 응답 그대로** 파싱. `test_research_scan` 55→56: 7-6 committed_at = 커밋 뒤 초 올림, 커밋 전 중단이면 없음.
- **실제 이전 버전(`14eb8c0`) 코드로 재현** → 새 코드: R1 `OK 8` → `NO_SCAN 0`, R2 재시도 요청 15:30:03 전송·LATE 기록 → 재시도 안 함·
  FETCH_FAILED·MISSED, R3 IntegrityError → `…_a1`·`…_a2`. 이전 코드가 만든 관찰 DB(s3)를 새 코드로 열면 백업·s4, 이전 실행은
  finished_at + 1시간으로 판정(10/1 09:10 저장 → 제외, 9/30 19:31 저장 → 포함).
- 변이 확인: R1~R3 10종(저장 완료 검사·run_id 검사 제거, 이전 실행 여유 0, 커밋 시각을 트랜잭션 안에서, 마감 미전달, 첫 요청 전에만 검사,
  수신 시각 검사 제거, 응답 지연 구분 제거, 보조 조회 마감 무시, 초 단위 실행 ID) + 실측 반영 7종 모두 잡힘. 기존 A5-1 변이 결과 동일.
- 회귀 31개 파일·수집 120(실측 원문 포함)·스캔 56·A5 42·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 판정 규칙·가정 체결가격 규칙(관찰가 기준)·후보 확정·계산 계약. 매수 가정 체결가를 최우선 매도호가로 바꿀지는 GPT 결정 대기.

### 다음 작업
- 사용자: 패치 적용 → 18:10 이후 `update`(관찰 저장소 백업 후 s4) → 10/6(화) 09:00~09:05 `open-check`(A5 저장소 백업 후 a2).
- A24-A: 조회 전용 상시 실행 관리자(작업 종류·대상 거래일·계약별 완료 상태, 긴 수집 분리, open-check 중복 실행 잠금, 운영 상태에
  NO_SCAN·NO_CANDIDATES·조회 실패·MISSED 구분 표시, 놓친 확인은 누락·지연으로).

### 전달 파일
- 패치 0001 (feat: 원천 시각·호가), 0002 (fix: R1~R3), 0003 (docs)

## 2026-10-07 — A5-1 잔여 2건: 커밋 시각 미입증 실행 제외, 기본 가격 선저장 (GPT 방향 전환 지시 7-1단계)

### 배경
GPT가 개발 방향을 "사용자 지정 종목의 상시 감시·분석·알림"으로 바꾸면서, 관련 기록의 정확성을 위해 A5 잔여 2건을 먼저 보완하도록 지시.
- 완료 시각(committed_at)이 없는 스캔을 `finished_at + 1시간`으로 입증된 완료처럼 처리하지 않기.
- 기본 가격(ka10001)을 먼저 저장하고, 보조 체결·호가 조회 중 중단돼도 보존하기.
- 체결 조회의 시각을 별도 기본정보 조회 가격의 원천 시각으로 간주하지 않기.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/research/open_check.py` | `commit_bound`: committed_at이 없으면 입증 불가(UNPROVEN)로 후보 원천에서 제외 — 1시간 여유 규칙 삭제. 기본 가격 행을 먼저 한 트랜잭션으로 저장(`extra_json` = 대기)한 뒤 보조 조회 결과를 덧붙임. 재시작 때 대기 상태 행은 다시 조회하지 않고 보조 조회만 INTERRUPTED로 마감. ka10003 값 열 이름을 `trade_time`·`trade_price`·`trade_exchange`·`trade_lag_sec`로(ka10001 원천 시각 `source_time`과 분리, 아직 비어 있음) |
| 문서 | `docs/research_a_stage.md` |

### 테스트 및 검증
- `test_research_open_check` 42→43: 4b-3 커밋 시각 없는 실행 제외(근거 UNPROVEN), 4b-9 보조 조회 중 중단 → 기본 가격·판정·요청 시각 보존,
  재시작 때 재조회 없이 INTERRUPTED. 변이 확인 2종(1시간 규칙 복구·보조 조회 뒤 저장) 모두 잡힘.
- 회귀 31개 파일·수집 120(실측 원문 포함)·스캔 56·A5 43·단타 동등성 18/18. `git status` 깨끗.

### 다음 작업
- 방향 전환 2단계: 사용자 지정 종목 설정·검증(관심/보유 구분, 수동 보유 정보, 설정 이력·마지막 정상 설정 유지).

### 전달 파일
- 패치 0001 (fix: 잔여 2건), 0002 (docs) — 기준 `53e56e5`

## 2026-10-07 — A5 기록 저장소 a3 이전: 옛 a2 DB 호환 (GPT 재검토 `0e494dd` P1)

### 배경
GPT가 `53e56e5` 코드로 만든 실제 a2 DB를 `0e494dd` 코드로 열자, 스키마 표시는 a2 그대로라 이전이 일어나지 않고
가격 기록 저장에서 `OperationalError: table price_check has no column named trade_time`이 발생.
옛 a2는 ka10003 최근 체결을 `source_*` 열에, 새 코드는 `trade_*` 열에 저장하는데 테스트가 새 DB와 a1 이전만 다뤘음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/research/open_check.py` | 스키마 a3. a1·a2 DB는 열 때 백업 후 한 트랜잭션으로 이전: `trade_*`·`trade_basis`·`legacy_json` 열 추가. 옛 `source_*` 값은 같은 행 `extra_json`의 ka10003 응답 첫 행(tm·cur_prc)과 일치할 때만 `trade_*`로 옮김(LEGACY_KA10003), 아니면 값 보존·UNKNOWN. `source_time`은 모두 비움(ka10001 원천 시각으로 오해 방지). 새 기록은 `trade_basis=KA10003`. 보고서는 근거 불명 체결 값을 `UNKNOWN(근거 불명)`으로 표시. 이전 집계 `upgrade_summary`·`meta.a3_upgrade` |
| `tools/research_collect.py` | `open-check`가 A5 저장소 이전 집계 출력 |
| `test_research_open_check.py` | 옛 버전 표 정의(9fc5637 a1·53e56e5 a2·0e494dd a2)로 만든 DB의 이전 검사 4-1b·4-1c·4-1d |
| 문서 | `docs/research_a_stage.md` A5-1 저장 항목 |

### 테스트 및 검증
- 실제 `53e56e5` 코드로 a2 DB 생성(후보 8 · 5종목 기록 뒤 중단):
  - `0e494dd`로 열기: 재현 — `OperationalError: ... no column named trade_time`.
  - 수정 후 열기: 백업 `bak-a2-*`, 5행 중 4행 LEGACY_KA10003로 옮김·1행(체결 조회 실패) 그대로. 기존 5행의 관찰가·판정·시각·
    가정 체결 동일, 재실행은 남은 3종목만 조회해 새 기록(KA10003), 다시 열면 이전 없음.
- `test_research_open_check` 43→45: a1 → a3, a2(53e56e5) → a3(일치·불일치·근거 없음·체결 값 없음 + 이전 뒤 새 기록·다시 열기),
  a2(0e494dd) → a3. 변이 확인 3종(근거 검사 생략·source_time 유지·a2 이전 생략) 모두 잡힘.
- 회귀 31개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 후보 확정·가격 판정·가정 체결 규칙, 수집·관찰 DB. 옛 `source_price` 등 열은 지우지 않음. DB 삭제·재수집 없음.

### 다음 작업
- 방향 전환 2단계: 지정 종목 설정·검증 — 시장·거래소·통화 포함(국내·미국), 관심/보유 분리, 수동 보유 정보,
  최초 오류 시 감시 시작 안 함·운영 중 오류 시 마지막 정상 설정 유지(버전 표시)·오류 상태 신규 매수 차단,
  관심 감시/보유 감시 중단 구분, 신규 종목은 등록 시 필요한 일봉 즉시 수집(확보·검증 전 UNKNOWN), 설정 이력과 적용 버전 연결.
- 해외: 미국 시세 API 후보 조사·조회만 실측(브로커 미정 — 공통 인터페이스 + 시장별 어댑터로 국내 작업과 병행).

### 전달 파일
- 패치 0001 (fix: a3 이전), 0002 (docs) — 기준 `0e494dd`

## 2026-10-07 — 지정 종목 설정·검증·데이터 준비 (방향 전환 2단계, 국내)

### 배경
최종 목표를 "사용자 지정 종목의 24시간 운영·자동매매"로 정하고, 첫 단계로 대상 선택(수동)과 데이터 준비를 만듦.
GPT 지시: 관심/수동 보유 분리, 최초 오류 시 감시 시작 안 함·운영 중 오류 시 마지막 정상 설정 유지·신규 매수 차단,
관심 감시 중단과 보유 감시 중단 구분, 신규 종목 즉시 수집·확보 전 UNKNOWN, 설정 이력과 적용 버전 연결,
수집 대상은 등록 종목 + 국내 지수. 미국 주식은 후속 단계로 미룸.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/watchlist/config.py` (신규) | 설정 스키마 w1 해석·검증(순수 함수). 문자열 코드 강제, 관심(켜기·S1·가격대)·수동 보유(수량·평균가·손절·목표, source=MANUAL), 종목 목록 대조, 종목·필드별 오류/경고 전부 수집, 따옴표 유지 YAML 쓰기 |
| `infra/watch/store.py` (신규) | `data/watch/watch.sqlite3`(wa1): 설정 시도 이력(APPLIED/REJECTED·원문·오류·목록 스냅숏), 준비 상태·변경 이력, 준비 실행 |
| `infra/watch/manager.py` (신규) | 적용(`sync_config`)·마지막 정상 설정 복원·`entry_blocked`, 등록 종목 + 지수만 수집(`prepare_data`, 연구 수집기 재사용), 준비 상태 READY/UNKNOWN, `entry_gate` |
| `tools/watchlist.py` (신규) | init·add·set·enable·disable·holding-close·remove·validate·apply·prepare·status·history. 편집은 검증 통과 때만 파일 기록, add는 즉시 과거 일봉 수집 |
| `config/watchlist.example.yaml` (신규) | 비활성 예시. 실제 `config/watchlist.yaml`은 `.gitignore` |
| `test_watchlist.py` (신규) | 25건 |
| 문서 | `docs/watchlist.md` |

### 테스트 및 검증
- `test_watchlist` 25건: 검증 6(따옴표 없는 코드·8진수 코드, 오류 전부 수집, 관심+보유 한 항목, 목록 대조, 목록 없음 fail-closed,
  따옴표 유지 쓰기) · 적용 3(최초 오류 시작 안 함·조회 0, 운영 중 오류 마지막 정상 유지·차단·중복 기록 없음, 파일 없음·복구·이력)
  · CLI 5(편집·거부 시 파일 그대로, 관심 꺼도 보유 감시 유지·보유 끄기/삭제 거부, 청산 뒤 삭제, add 즉시 수집)
  · 준비 9(등록 종목+지수만 조회, READY/이력 부족/위험/보유만, 버전 연결, 운영 중 오류 시 진행·차단, 한 종목 실패 격리,
  삭제해도 다른 기록·일봉 보존, status 표, 장중 기준일, 그날 첫 목록 갱신) · 경계 2(주문·브로커 import 없음, 레포 산출물 없음).
- 변이 확인 9종(오류 시 차단 제거·최근 시도를 사용 설정으로·최초 오류에도 준비·비활성 종목 수집·이력 부족 무시·보유 항목 삭제·
  코드 따옴표 제거·숫자 코드 허용·편집 미기록) 모두 잡힘.
- 회귀 32개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 전체 시장 연구 도구(`tools/research_collect.py`)·수집·관찰·A5 DB와 그 동작. BUY/HOLD/SELL·주문 경로. 연구 DB 삭제·재수집 없음.

### 다음 작업
- 3단계 조회 전용 상시 실행 관리자: 재시작 후 이어서, 중복 실행 잠금, 수집 중에도 보유 감시 계속, 기존 작업 스케줄러와 중복 정리.

### 전달 파일
- 패치 0001 (feat: 지정 종목 설정·검증·준비), 0002 (docs) — 기준 `5edb518`

## 2026-10-07 — 지정 종목 보완 W1-R1~R4 (GPT 재검토 `0fcfa15`)

### 배경
GPT가 3단계 상시 관리자 연결 전에 4건을 재현.
- R1: YAML을 직접 고쳐 보유를 지우면 APPLIED — 보유 감시가 사라짐.
- R2: 새 목록의 투자경고가 진입 관찰에 반영되지 않음(준비 기록의 옛 위험 값), 경고·목록 변화가 이력에서 생략.
- R3: 기준일 봉 거래량 0도 READY(S1은 NO_TRADES_AT_T로 보류).
- R4: 인코딩·권한 오류가 예외로 터져 마지막 정상 설정으로 복구되지 않음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/watch/manager.py` | `holding_guard`: 마지막 정상 설정의 수동 보유가 사라지면 같은 값의 청산 기록이 있을 때만 적용, 아니면 REJECTED. `refresh_risk`: 설정을 읽을 때마다 최신 목록으로 위험 자격 갱신. 준비 상태를 가격 데이터(status)와 S1 분석(analysis_status — `SeriesView.window` 계약)으로 분리. `entry_gate`에 위험 자격·목록 스냅숏·분석 준비 반영. `read_config_text`: UnicodeError·OSError를 설정 오류로 |
| `infra/watch/store.py` | 스키마 wa2(wa1은 백업 후 이전): `holding_close`(OPEN/USED/VOID), `symbol_risk`·`symbol_risk_log`, `config_check`(목록이 바뀐 재검증), readiness·log에 분석 준비 열. 설정 기록 중복 판단에 경고 포함 |
| `tools/watchlist.py` | 공통 파일 읽기, `holding-close`가 청산 기록 후 적용, status에 가격 데이터·S1 분석·위험 자격(목록 스냅숏 기준) 열 |
| `test_watchlist.py` | 25 → 33건 |
| 문서 | `docs/watchlist.md` |

### 테스트 및 검증
- 수정 전 코드(`fe04b30`)에서 4건 모두 재현: R1 보유 삭제 APPLIED·감시 대상에서 빠짐, R2 투자경고 뒤 진입 관찰 가능, R3 거래량 0 기준일 READY·가능,
  R4 `UnicodeDecodeError`. 수정 후: R1 REJECTED·보유 유지, R2 RISK_FLAGS 차단, R3 S1 분석 HOLD(NO_TRADES_AT_T), R4 REJECTED·마지막 정상 유지.
- 새 검사 8건: 6-1~6-3 보유 보호·청산 기록 USED/VOID, 6-4~6-5 prepare 없이 위험 차단·위험 이력·재검증 기록·RISK_STALE,
  6-6 NO_TRADES_AT_T·창 안 NO_TRADES·DATA_GAP(보유 가격 감시는 유지), 6-7 최초·운영 중 인코딩·읽기 오류, 6-8 wa1 → wa2 이전.
- 변이 확인 8종(보유 보호·청산 무효화·위험 갱신·위험 차단·경고 변화·분석 창·인코딩·OSError) 모두 잡힘.
- 회귀 32개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·지정 종목 33·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 연구 수집·S1 스캔·A5, 주문 경로. 160봉 기본값(S1_BASE). 연구 DB 삭제·재수집 없음.

### 다음 작업
- 3단계 조회 전용 상시 실행 관리자(재시작 후 이어서, 중복 실행 잠금, 수집 중 보유 가격 감시 지속, 기존 스케줄러 정리).
- 연초·긴 지표 대비 이전 연도 거래일 달력 확보.

### 전달 파일
- 패치 0001 (fix: W1-R1~R4), 0002 (docs) — 기준 `0fcfa15`

## 2026-10-07 — 지정 종목 보완 W1b-R1·R2 (GPT 재검토 `6dc8e11`)

### 배경
- R1: `history_sessions=60`이면 60봉만으로 분석 READY·진입 관찰 가능. 160봉 READY 뒤 300으로 올리고 apply만 해도 이전 READY 사용.
- R2: YAML에서 보유를 지운 상태로 CLI 편집(add) → 보유 보호가 REJECTED로 막았지만 파일은 바뀌고 종료 코드 0.
  YAML에서 이미 보유를 지웠으면 안내된 `holding-close`가 "수동 보유 정보가 없음"으로 거부.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `domain/watchlist/config.py` | `monitor.history_sessions` 하한 = `S1Config.min_history`(160), 기본값도 같은 값 |
| `infra/watch/manager.py` | `analysis_basis`(전략·S1 설정 해시·필요 봉 수)를 준비 결과에 저장, `entry_gate`는 지금 기준과 다르면 HOLD(BASIS_CHANGED). `holding_guard(pending=)`: 파일을 바꾸기 전 검사용. 보유 보호 안내에 holding-close(YAML에서 지웠어도)·restore |
| `tools/watchlist.py` | `commit_text`: 형식·목록·보유 보호 → 청산 기록 → 파일 교체 → 적용. 적용 거부 시 원래 파일로 되돌리고 종료 코드 2·청산 기록 VOID. `holding-close`는 사용 중 설정의 보유로 청산(YAML에서 이미 빠져도). `restore [--version N]` 신설. status에 BASIS_CHANGED 표시 |
| `test_watchlist.py` | 33 → 39건 |
| 문서 | `docs/watchlist.md` |

### 테스트 및 검증
- 수정 전 코드(`6dc8e11`)에서 재현: 60봉 설정 APPLIED, 300봉 상향 뒤 진입 관찰 가능, 거부된 CLI add 종료 코드 0·파일 변경, YAML 누락 뒤 holding-close 거부.
  수정 후: 60봉 설정 오류, BASIS_CHANGED 보류(재판정 뒤 해제), add 거부·파일 그대로·종료 코드 2, holding-close 적용.
- 새 검사 6건(7-1~7-6): S1 최소 이력, 기준 변경 보류·재판정, 쓰기 전 보유 보호, restore, YAML 누락 뒤 청산, 쓴 뒤 적용 거부 시 되돌리기.
- 변이 확인 5종 모두 잡힘. 회귀 32개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·지정 종목 39·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 연구 수집·S1 스캔·A5, 주문 경로, 감시 DB 스키마(wa2 그대로).

### 다음 작업
- 3단계 조회 전용 상시 실행 관리자.

### 전달 파일
- 패치 0001 (fix: W1b-R1·R2), 0002 (docs) — 기준 `6dc8e11`

## 2026-10-07 — 지정 종목 보완 W1c-R1 (GPT 재검토 `a60c7df`)

### 배경
`commit_text()`가 청산 기록을 먼저 저장한 뒤 파일을 교체하는데, 교체가 `PermissionError`로 실패하면 정리 코드가 돌지 않아
OPEN 기록이 남음. 이후 YAML에서 보유를 빼고 apply하면 그 기록이 쓰여 APPLIED·보유 감시 종료.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `tools/watchlist.py` | `commit_text`: 청산 기록·파일 읽기·교체·적용을 예외 처리 — 그 명령의 청산 기록 VOID(close_id로), 교체 뒤면 원래 파일 복원, 복원까지 실패하면 REJECTED 시도("복원 실패")로 기록·종료 코드 2. 중단(Ctrl+C)은 같은 정리 뒤 다시 올림 |
| `infra/watch/manager.py` | 청산 기록은 그것을 만든 명령이 넘긴 close_id로만 보유 보호를 통과(`sync_config(closes=)`). 적용 뒤 쓰이지 않은 OPEN 기록은 모두 VOID — 강제 종료로 남은 기록도 이후 apply에 쓰이지 않음 |
| `infra/watch/store.py` | `get_close`·`void_close`·`void_open_closes` |
| `test_watchlist.py` | 39 → 44건 |
| 문서 | `docs/watchlist.md` |

### 테스트 및 검증
- 수정 전 코드(`a60c7df`)에서 재현: holding-close 교체 실패 뒤 OPEN 남음 → 보유 누락 apply가 APPLIED·보유 감시 종료.
  수정 후: 기록 VOID·종료 코드 2, 이후 apply REJECTED·보유 유지.
- 새 검사 5건(8-1~8-5): 교체 실패 → 재시작 → 보유 누락 apply 거부, 강제 종료로 남은 OPEN 기록 무시·정리, 적용 중 예외 시 파일 복원,
  복원 실패 기록·신규 매수 차단, 중단 시 정리 후 재발생.
- 변이 확인 5종 모두 잡힘. 회귀 32개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·지정 종목 44·단타 동등성 18/18. `git status` 깨끗.

### 변경하지 않은 것
- 감시 DB 스키마(wa2), 연구 수집·S1·A5, 주문 경로.

### 다음 작업
- 3단계 조회 전용 상시 실행 관리자.

### 전달 파일
- 패치 0001 (fix: W1c-R1), 0002 (docs) — 기준 `a60c7df`

## 2026-10-07 — 배치 A: 설정 적용의 DB/파일 실패 경계 W1d-R1 (GPT 검토 `90c3d55`)

### 배경
`sync_config`가 시도 기록(APPLIED)을 먼저 커밋한 뒤 청산 정산·OPEN 정리·위험 자격을 따로 커밋해, 그 사이 실패하면
CLI는 YAML만 되돌리고 DB에는 보유가 지워진 새 설정이 사용 중으로 남음(entry_blocked=False). 함께 C1(설정 변경과 자동 읽기
경쟁), C2(청산 근거의 종목·버전 연결), C3(정리 실패를 성공처럼 표시) 검토.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/watch/store.py` | `tx()` 재진입 — 안쪽 메서드가 바깥 트랜잭션에 합류. `record_close(state=, used_version=)` |
| `infra/watch/manager.py` | `sync_config`: 시도 기록·청산 기록(USED)·OPEN 정리·위험 자격을 **한 트랜잭션**(COMMIT = 확정 지점). `close_holdings`+`expect_version`(트랜잭션 안 재확인, 다르면 ConfigConflict). `holding_guard`: 청산 근거의 종목·값 검사, 저장소 OPEN 기록은 근거 아님 |
| `infra/watch/apply.py` (신규) | 설정 적용 잠금(OS 파일 잠금, 재진입, 대기 상한), 적용 저널·재시작 복구, `commit_config_text`(사전 검사→저널→교체→확정→정리, 단계별 성공/실패 보고), `sync_file`, 작업별 고유 임시 파일, 시험용 중단 지점(환경 변수로만) |
| `tools/watchlist.py` | 편집·restore를 잠금 안에서(파일 읽기~적용), add의 과거 일봉 수집은 잠금 밖. apply·status·prepare는 `sync_file`, DB 오류는 종료 코드 2. `--lock-timeout` |
| `.gitignore` | `config/watchlist.yaml.*`(저널·임시 파일) |
| `test_watchlist.py` | 44 → 52건(7-6·8-1~8-5를 새 경계로 고침, 9-1~9-8 추가) |
| 문서 | `docs/watchlist.md` 적용 경계 절 |

### 테스트 및 검증
- 수정 전(`90c3d55`) 재현 — holding-close 중 장애 주입, 새 연결로 확인:

  | 주입 지점 | 수정 전 | 수정 후 |
  |---|---|---|
  | 청산 정산(시도 기록 직후) | YAML 10주 · DB v2 보유 없음 · 차단 안 됨 | v1 10주 · 청산 기록 없음 · 일치 |
  | 남은 OPEN 정리 | 같음(USED) | 같음(v1 일치) |
  | 위험 자격 시작 / 일부 쓰기 뒤 | 같음(위험 자격 v2 일부) | 같음(위험 자격 v1) |

- 강제 종료(실제 별도 프로세스): 수정 전은 적용 시작 전 종료 시 YAML 보유 없음·DB 10주·차단(수동 복구 필요).
  수정 후 7개 지점 모두 재시작한 status가 자동 복구(확정 전 → 원래 파일, 확정 뒤 → 확인)·YAML/DB 일치.
- 새 검사 9-1~9-8: 트랜잭션 경계 4지점 실패, 일반 apply 실패, 강제 종료 7지점 + 재시작, 복원 실패 + 기록 실패(결과 불확정),
  성공 경로(USED·v2·위험 자격 v2), C2 근거 검사·ConfigConflict, C1 두 프로세스 경쟁·잠금 대기 상한.
- 변이 확인 8종(단일 트랜잭션·재진입·저널 복구·근거 종목·기준 버전·잠금·파일 복원·기록 실패 보고) 모두 잡힘.
- 회귀 32개 파일·수집 120(실측 원문 포함)·스캔 56·A5 45·지정 종목 52·단타 동등성 18/18. `git status` 깨끗.
  `test_broker_order_status.py`는 레포에 없는 실측 fixture가 필요해 계속 제외(`--skip`) — 통과로 세지 않음.

### 변경하지 않은 것
- 감시 DB 스키마(wa2 — 열·표 추가 없음), 설정 파일 형식, 연구 수집·S1·A5, 주문 경로.

### 다음 작업
- 배치 B: 조회 전용 상시 실행 관리자(W2/A24-A).

### 전달 파일
- 패치 0001 (fix: W1d-R1), 0002 (docs) — 기준 `90c3d55`

## 2026-10-07 — 배치 B: W2/A24-A 조회 전용 상시 실행 관리자

### 배경
사용자 지정 국내 종목의 조회 전용 자동 운영을 시작해 데이터를 쌓는 단계(GPT 요청서 배치 B). 주문 기능은 켜지 않음.

### 변경 내용
| 파일 | 내용 |
|---|---|
| `infra/watch/daemon.py` (신규) | 관리자: 작업 CLOSE_PREP(마감 뒤 목록→설정 반영→지정 종목·지수 일봉→지정 종목 S1 관찰→보고서)·OPEN_CHECK(개장 + 5분 A5-1 가격 기록), 거래일 달력 기준 실행 시각·기한(MISSED), 작업 키(종류·거래일·대상 범위), 상태·재시도(5·15·30·60분, 실패 4번), 우선순위·양보(개장 확인·중지·호출 예산·시간 상한), 강제 종료 뒤 ABORTED 복구, heartbeat, 일일 보고서, 로그 가림. 저장 `data/watch/daemon.sqlite3`(wd1) |
| `tools/watch_daemon.py` (신규) | run(--until-idle)·status·stop·report·doctor(Windows 작업 스케줄러 읽기만). 관리자 중복 기동 OS 잠금 |
| `infra/research/s1_scanner.py` | 선택 인자 `selection`(대상 코드·선정 방식을 계약에 포함, 그 종목만 계산)·`context_extra`. 없으면 계약 해시 그대로(`170bd3f6d10b`) |
| `infra/research/kiwoom_readonly.py` | 토큰 응답 `expires_dt`로 만료 10분 전 재발급(형식 미실측 — 못 읽으면 401에만 의존), 재인증 10분 안 3번 상한 |
| `infra/watch/manager.py` | `prepare_data(should_stop=)` — 대상마다 양보 확인, YIELDED |
| `test_watch_daemon.py` (신규) | 34건 |
| 문서 | `docs/watch_daemon.md`(운영 명세·Windows 명령·장애별 복구), README |

### 테스트 및 검증
- `test_watch_daemon` 34건(가짜 시계·가짜 키움): 실행 시각(일반·특수 개장·휴장), 정상 다일(10/7 준비 → 10/8 개장 확인 → 10/8 준비 → 10/9 휴장 →
  10/12 개장 확인), 끝난 작업 재실행 없음·재시작, 자정 전환, 놓친 개장 확인 MISSED(조회 0), 늦은 관찰 actionable=0·NO_SCAN,
  CLOSE_PREP 기한 MISSED, 개장 뒤 재관찰 계약 무시, 양보(개장 확인 우선·예산·중지), 설정 추가·오류·없음, 토큰 만료 재발급·401·429·
  재인증 상한·다른 오류 구분, DB 잠김 재시도, 실제 별도 프로세스의 DB 잠금 대기, 보고서 실패·재생성, **실제 별도 프로세스** 강제 종료 →
  재기동 복구·중복 기동 거부·stop, Ctrl+C, status·로그 가림·import 경계·조회 TR만·레포 산출물 없음, doctor.
- 변이 확인 9종(끝난 작업 재실행·개장 확인 우선·남은 RUNNING·CLOSE_PREP 기한·호출 예산·개장 전 저장 관찰·토큰 만료·재인증 상한 등) 모두 잡힘.
- 회귀 33개 파일(지정 종목·관리자 포함)·수집 120(실측 원문 포함)·스캔 56·A5 45·지정 종목 52·관리자 34·단타 동등성 18/18. `git status` 깨끗.
  `test_broker_order_status.py`는 레포에 없는 실측 fixture가 필요해 계속 제외 — 통과로 세지 않음.
- **미실측**: 실제 API로 2거래일 이상 운영한 로그·보고서 없음(가짜 API 시험 출력만), 토큰 expires_dt 형식, 완성 시각 160분.

### 변경하지 않은 것
- 전체 시장 수집·S1 스캔·A5 CLI와 그 DB, 연구 계약 해시, 감시 DB 스키마(wa2), 주문·계좌 경로(연결 없음), 사용자 PC 작업 스케줄러.

### 다음 작업
- 실제 모의 도메인으로 2거래일 이상 조회 전용 운영 → 로그·보고서 확인.
- W3 장중 가격 감시·알림(이 관리자의 우선순위·양보 구조에 연결).

### 전달 파일
- 패치 0003 (feat: W2 관리자), 0004 (docs) — 배치 A 패치 0001·0002 뒤
<!-- 이후 작업은 여기부터 이어서 기록합니다. -->
