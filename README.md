# Swing Auto Trader

키움증권 REST API 기반 스윙 자동매매 시스템.
단타 레포 [`kiwoom-auto-trader`](https://github.com/minwoopg/kiwoom-auto-trader)
(`bdde6c2`, 2026-09-28)에서 **매매 로직을 제외한 기반 코드**를 가져와 시작했습니다.

> 현재 상태(4라운드): 매매 루프 없음. 주문 실행부(`OrderExecutor`) 추출 완료, 아직 진입점에는 연결 안 됨.
>  `python -m app.main`은 기동 점검
> (인증 → 잔고 → 미해결 주문 흔적 확인 → 시작 알림)만 하고 종료합니다.

---

## 운영 원칙

- **계좌 분리**: 단타 프로그램과 다른 계좌·앱키를 사용합니다. 단타 프로그램은
  계좌의 모든 보유 종목을 자기 포지션으로 처리하고 15:10 이후 강제청산하므로
  같은 계좌를 쓰면 스윙 보유분이 청산됩니다.
- **매일 기동·종료**: 장 시작 전에 켜고 장 마감 후 끕니다.
  (그래서 토큰 자동 갱신은 넣지 않았습니다 — 키움 토큰은 시작 시 1회 발급)
- **시장가 주문만 사용** (취소 주문 API 미구현)

## 프로젝트 구조

```
swing-auto-trader/
├── app/main.py                     # 진입점 — 1라운드: 기반 점검 모드
├── config/
│   ├── settings.py                 # App/Broker/Storage/Kakao 설정만
│   └── settings.yaml
├── domain/
│   ├── models.py                   # 주문·잔고·체결 모델 (RuntimeState는 단타 원본 — 새 코드는 SwingState 사용)
│   ├── cost_model.py               # 비용 3시나리오 (fail-closed 로딩)
│   ├── position/lifecycle.py       # 포지션 상태머신(PSM) — 체결 확인 게이트
│   ├── position/swing_state.py     # 스윙 상태(주문 의도·포지션 메타)
│   ├── position/fill_event.py      # 체결 사건 모델
│   ├── position/position_book.py   # 원장·잔고·메타 대조 보고
│   ├── service/lot_ledger.py       # 여러 날 FIFO 로트·실현/평가손익
│   ├── service/order_executor.py   # 주문 실행·체결 추적·재시작 복구 (단타 TradingService에서 추출)
│   ├── service/pnl_calculator.py   # FIFO 손익 (여러 날 보유 대응 수정 예정)
│   ├── strategy/exit_calc.py       # 손절·트레일링 순수 계산
│   └── indicator/indicators.py     # ATR·볼린저
├── infra/
│   ├── broker/                     # 키움 REST 브로커 + MockBroker + 미체결/체결 판정
│   ├── notify/kakao_notifier.py    # 카카오 알림
│   └── storage/
│       ├── tracked_order_journal.py        # 체결 확정 전 주문 원자적 보존
│       ├── order_status_observation_store.py # 체결조회 증거 JSONL
│       ├── swing_state_store.py            # 스윙 상태 JSON (원자적 쓰기, 손상 시 거부)
│       ├── fill_ledger.py                  # 체결 원장 JSONL (수량·단가·진입일의 원천)
│       ├── state_store.py                  # (단타 원본, 동등성 비교용)
│       ├── run_baseline.py                 # 실행 기준선 기록
│       ├── process_lock.py                 # 중복 실행 차단
│       └── logger.py                       # app.log / trades.csv / position_lifecycle.csv
├── utils/time_utils.py, trade_outcome.py
├── utils/trading_calendar.py       # KRX 거래일·장 단계 (config/krx_calendar.yaml)
├── tools/probe_market_data.py      # 조회 전용 실측 프로브 (모의투자 도메인만)
├── tools/equivalence/              # OrderExecutor ↔ 단타 TradingService 동작 비교 도구
├── provenance.json                 # 파일별 원본 출처·해시
├── testing_helpers.py              # 테스트 공용 Settings 헬퍼
└── test_*.py                       # 회귀 테스트 (run_regression_tests.py)
```

단타 레포와 **같은 상대 경로**를 유지했습니다. 단타 쪽에서 브로커·상태머신이
수정되면 같은 경로끼리 바로 diff해 반영할 수 있습니다.

## 환경 설정

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env   # 스윙 계좌 값 입력
```

`config/settings.yaml`의 `broker.use_mock: true`로 시작합니다. 실제 키움
모의투자 서버로 점검하려면 `false`로 바꿉니다.

## 실행

```powershell
python -m app.main
```

## 테스트

```powershell
python run_regression_tests.py
```

`test_broker_order_status.py`는 실측 fixture가 필요합니다. 단타 레포 로컬의
`tests/fixtures/order_reconciliation/` 폴더를 같은 경로로 복사하세요
(단타 레포 git에도 포함돼 있지 않은 파일입니다).

## 주문 실행 (`OrderExecutor`)

```python
executor = OrderExecutor(settings=..., broker=..., state=state, highest_price=hp,
                         state_store=store, app_logger=log, trade_logger=trades,
                         on_first_fill_buy=..., on_sell_closed=...)
executor.sync_with_balance(balance, watch_symbols)   # 매 폴링, 주문 판단 전에
sub = executor.submit_buy("005930", 10, 70000, context={"entry_strategy": "..."})
if sub.block_code: ...                                # 주문 안전 게이트에 막힘
```

- 진입 조건·리스크 한도는 호출부가 먼저 판단합니다. `OrderExecutor`는 주문 안전 게이트만 봅니다.
- 첫 체결과 완전 청산은 훅으로 알려줍니다(접수 시점이 아님).
- 사람 확인이 필요한 상태(ERROR / orphan)는 `commands/ack_error_{종목}.json`,
  `commands/ack_orphan_{종목}.json` 파일로만 해제됩니다.

동작이 단타 레포와 같은지 확인:

```powershell
python tools/equivalence/compare.py --orig ..\KIWOOM-AUTO-TRADER
```

## 상태와 원장 — 한 사실은 한 곳에만

| 사실 | 저장 위치 |
|---|---|
| 보유 수량·매입단가·진입 거래일 | `data/fill_ledger.jsonl` (체결 원장, 추가만 함) |
| 보냈는지 모르는 주문 | `data/state.json` → `unresolved_order_intents` |
| 전략 ID·손절가 등 | `data/state.json` → `positions` |

기동할 때 원장·잔고·메타를 대조해 `app.log`의 `[STARTUP_RECONCILE]`에 남깁니다.
어긋나도 자동으로 고치지 않습니다. 이미 계좌에 있던 종목을 인수하려면 사람이 확인한 뒤
`opening_events_from_balance()`로 OPENING 사건을 기록합니다.

## 거래일 캘린더

`config/krx_calendar.yaml`에 연도별 평일 휴장일을 적습니다. 목록에 없는 연도를 조회하면
예외가 납니다 — **매년 말 다음 해 휴장일을 추가하세요.**

## 실측 프로브 (조회 전용)

```powershell
python tools/probe_market_data.py                     # 장중 1회 + 장 마감 후 1회
python tools/probe_market_data.py --env-file ..\KIWOOM-AUTO-TRADER\.env --skip-daily   # 장 시작 전, 주문 이력 있는 모의계좌
```

결과 요약은 `logs/probes/*_summary.txt`에 남습니다.

## 카카오 알림

`.env`의 `KAKAO_*` 값을 비워두면 알림이 자동으로 꺼집니다(코드 변경 불필요).

## 원본과의 관계 (`provenance.json`)

| status | 의미 |
|---|---|
| `unchanged` | 원본과 바이트 동일 — `test_extraction_boundary.py`가 해시로 검증 |
| `modified` | 원본을 부분 수정 |
| `rewritten` | 원본을 참고해 새로 작성 |
| `derived` | 원본 테스트 일부를 옮김 |
| `new` | 신규 |

`unchanged` 파일을 수정하면 `provenance.json`의 status를 `modified`로 바꿔야
테스트가 통과합니다. 단타 레포 수정 사항을 가져올 때는 `unchanged` 파일부터
원본과 비교하세요.
