# Swing Auto Trader

키움증권 REST API 기반 스윙 자동매매 시스템.
단타 레포 [`kiwoom-auto-trader`](https://github.com/minwoopg/kiwoom-auto-trader)
(`bdde6c2`, 2026-09-28)에서 **매매 로직을 제외한 기반 코드**를 가져와 시작했습니다.

> 현재 상태(7라운드, 뼈대 완료): 하루 수명주기·안전 한도·체결 원장 자동 기록·일일 리포트·번들·작업 스케줄러·CI.
> 전략 자리는 비어 있음(`NullStrategy` — 주문을 내지 않음).

---

## 운영 원칙

- **계좌 분리**: 단타 프로그램과 다른 계좌·앱키를 사용합니다. 단타 프로그램은
  계좌의 모든 보유 종목을 자기 포지션으로 처리하고 15:10 이후 강제청산하므로
  같은 계좌를 쓰면 스윙 보유분이 청산됩니다.
- **실행 경로는 둘이고 수명·토큰 처리가 다릅니다**:

  | 실행 경로 | 수명 | 토큰 | 주문 |
  |---|---|---|---|
  | `app/main.py`(→ `app/session_runner.py`, 주문 경로 기반 코드) | **매일 기동·종료** — 장 시작 전에 켜고 장 마감 후 끔 | 시작 때 1회 발급, 자동 갱신 없음 | 전략 자리 비어 있음(`NullStrategy` — 주문 안 냄) |
  | `tools/watch_daemon.py run`(W2 지정 종목 조회 전용 관리자) | **상시 실행** — 거래일 달력에 맞춰 마감 준비·개장 확인 | 조회 클라이언트가 응답의 `expires_dt`로 만료 10분 전 재발급(형식 미실측 — 못 읽으면 401 때 재인증, 10분 안 3번 상한) | 없음(모의 도메인 조회 TR만) |

  `app/main.py`가 상시 실행으로 바뀐 것은 아닙니다. 연구 수집 `tools/research_collect.py`도 실행할 때만 도는 별도 프로세스입니다.
- **시장가 주문만 사용** (취소 주문 API 미구현)

## 프로젝트 구조

```
swing-auto-trader/
├── app/main.py                     # 진입점 — 기동 점검 + 하루 수명주기 (--check-only: 점검만)
├── app/session_runner.py           # 하루 수명주기 (장전 대기·장중 폴링·마감 후 대조)
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
│   ├── strategy/interface.py       # 전략 자리: OrderIntent·TickContext·NullStrategy
│   ├── strategy/exit_calc.py       # 손절·트레일링 순수 계산
│   ├── risk/account_guard.py       # 계좌 안전 한도 (전략과 무관한 절대 한도)
│   ├── service/fill_recorder.py    # 체결 → 원장 자동 기록
│   ├── market_data/daily_bar.py    # 일봉 모델·엄격 파서
│   └── indicator/indicators.py     # ATR·볼린저
├── infra/
│   ├── broker/                     # 키움 REST 브로커 + MockBroker + 미체결/체결 판정
│   ├── market_data/                # 일봉 수집(속도 제한·429 재시도)·저장·증분 갱신
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
├── infra/reporting/                # 일일 리포트·마스킹
├── app/reports.py                  # 리포트·번들 조립
├── tools/daily_report.py, export_bundle.py  # 리포트 재생성·번들 내보내기
├── scripts/*.ps1                   # 작업 스케줄러 등록·실행
├── .github/workflows/regression.yml  # 푸시마다 회귀 테스트 (Windows·Ubuntu)
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
python -m app.main                # 장 시작 전 기동 → 장 마감 후(15:30~15:45) 자동 종료
python -m app.main --check-only   # 기동 점검만
```

하루 흐름: 휴장일이면 바로 종료 → 장 시작 대기 → 60초마다 잔고·주문 대조·원장 기록·장부 대조·전략·안전 한도
→ 마감 후 미해결 주문 대조 → 요약(`[SESSION_SUMMARY]`) 후 종료. 한도 값은 `config/settings.yaml`의 `guard`.

안전 한도 요점(8-B):
- 장부 대조는 **종목별**입니다. 미해결 주문이 걸린 종목의 수량 차이만 보류하고, 다른 종목 불일치는 계속 차단.
- 계좌 어디든 장부 불일치가 있으면 **모든 신규 매수 보류**(불일치 없는 종목의 매도는 가능).
- 매도 수량은 원장 보유와 이번 잔고 수량 **둘 다** 이하여야 함.
- 총 노출 = 계좌 전체 보유(원장·잔고 중 큰 수량) × **현재가** + 미체결 매수 + 이번 주문×(1+`buy_price_buffer_pct`%).
  매수 의도가 있을 때만 보유 종목 현재가(ka10001)를 조회하며, 하나라도 모르면 매수 보류.

## 테스트

```powershell
python run_regression_tests.py
```

`test_multiday_integration.py`(9단계)는 4거래일을 이어서 돌리며 분할매도·장중 재시작·복구 명령·API 장애·상태 파일 저장 실패를 한 번에 검증합니다.

GitHub Actions가 main 푸시마다 Windows·Ubuntu에서 같은 테스트를 돌립니다(실측 fixture가 필요한 `test_broker_order_status.py`만 제외).

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
- 분할청산(8-C/8-E): 매도 주문마다 목표 잔고(보유 − 요청)를 고정합니다. 잔고가 목표에 도달하고
  **주문 조회가 그 주문번호를 FILLED로 확인할 때만** 주문이 종료되고 포지션은 OPEN으로 남습니다(청산 훅 없음).
  주문 조회가 OPEN·UNKNOWN·오류면 목표 잔고여도 차단 유지 → 타임아웃 후 orphan(FILLED 확인 또는
  `ack_orphan`으로만 해제). 목표보다 더 줄면 ERROR. 전량매도(목표 0)는 원본과 같이 잔고 0으로 확정합니다.
- 사람 확인이 필요한 상태(ERROR / orphan)는 `commands/ack_error_{종목}.json`,
  `commands/ack_orphan_{종목}.json` 파일로만 해제됩니다. **명령에는 현재 복구 사건 ID(`recovery_id`)가
  있어야 합니다(8-F)** — ID는 app.log `[RECOVERY_REQUIRED]`와 `commands/recovery_required.json`(명령 템플릿 포함)에
  나옵니다. 재시작하면 복원된 ERROR에 새 ID가 붙으므로, 이전 사건·재시작 전에 쓴 명령은 적용되지 않습니다.
  폴더는 `storage.commands_dir`(기본 `commands`)이며 실행 산출물이라 git에서 제외합니다. 목록 파일은 기동할 때마다 새로 씁니다.

  ```powershell
  $body = @{ recovery_id = "ERROR-005930-1a2b3c4d"; broker_quantity = 10; note = "HTS 확인" } | ConvertTo-Json
  $body | Out-File -Encoding utf8 commands\ack_error_005930.json
  ```
- 명령은 실행 **전에** `commands/processing/`으로 옮겨 확보하고(못 옮기면 이번에는 실행 안 하고 30초 간격으로 재확보 시도), 실행 후 `commands/processed/`
  또는 사유(`.error.txt`)와 함께 `commands/failed/`로 옮깁니다. 보관에 실패하면 원문은 `processing/`에 그대로 남으며
  다시 실행되지 않습니다(재시작 포함). 실행 결과와 보관 결과는 로그에 따로 남습니다. BOM 있는 UTF-8도 허용.

동작이 단타 레포와 같은지 확인:

```powershell
python tools/equivalence/compare.py --orig ..\KIWOOM-AUTO-TRADER
```

## 매일 자동 실행 (작업 스케줄러)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1                 # 평일 08:40
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:30 -Python "C:\경로\.venv\Scripts\python.exe"
Start-ScheduledTask -TaskName SwingAutoTrader                                        # 지금 한 번 실행해 확인
powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1               # 해제
```

공휴일은 프로그램이 캘린더로 판단해 바로 종료합니다. 실행 출력: `logs\scheduler\run_<시각>.log`.

하루가 제대로 끝났는지는 **리포트 파일이 아니라** `reports\session_status_<날짜>.json`의 `close_check`로 봅니다(8-D).

| close_check | 뜻 | 종료 코드 |
|---|---|---|
| `VERIFIED` | 마감 후 잔고 조회·장부 대조 성공, 불일치·미해결 주문·신규 주문 중단·일봉/리포트 실패 없음 | 0 |
| `NEEDS_REVIEW` | 위 중 하나라도 실패 — `close_issues`와 app.log `[SESSION_CLOSE]` 확인 | 2 |
| `NOT_RUN` | 마감 전에 중지됨 | 0 |
| (없음) | 휴장일 | 0 |

예외로 비정상 종료하면 종료 코드 1. 작업 스케줄러의 "마지막 실행 결과"에 그대로 나타납니다.
상태 파일에는 `run_id`·`generated_at`이 있어 이번 실행의 결과인지 확인할 수 있고, 상태 파일 저장에
실패하면 이전 파일을 치우고 `STATUS_WRITE_FAILED`로 종료 코드 2를 냅니다(8-E). 마감 보고서의 장부 대조는
마감 후 최종 대조만 쓰며, 없으면 "마감 최종 대조 미확보"로 표시합니다.

## 리포트·번들

- 하루가 끝나면 `reports\daily_report_<날짜>.md`가 자동 생성됩니다(평가는 완성 일봉 종가 기준).
- 다시 만들기: `python tools/daily_report.py --date 2026-09-28`
  지난 날짜는 **그날까지의 체결 원장만**으로 보유·손익을 다시 계산합니다(이후 매매가 섞이지 않음).
  그날의 포지션 메타·미해결 주문·장부 대조는 기록이 없어 표시하지 않습니다.
- 공유용 번들(민감정보 가림): `python tools/export_bundle.py --date 2026-09-28` → `exports\swing_bundle_<날짜>.zip`

## 상태와 원장 — 한 사실은 한 곳에만

체결 사건 id(8-C): `{account_scope_id}|{주문 거래일}|{BUY/SELL}|{종목}|{주문번호}|{누적 체결수량}` —
같은 주문 재조회는 중복 기록되지 않고, 다른 날·다른 계좌의 같은 주문번호는 다른 사건입니다.
`broker.account_scope_id`(계좌번호 아님)는 계좌를 바꾸면 반드시 다른 값으로 바꾸세요.

| 사실 | 저장 위치 |
|---|---|
| 보유 수량·매입단가·진입 거래일 | `data/fill_ledger.jsonl` (체결 원장, 추가만 함) |
| 보냈는지 모르는 주문 | `data/state.json` → `unresolved_order_intents` |
| 전략 ID·손절가 등 | `data/state.json` → `positions` |

기동할 때 원장·잔고·메타를 대조해 `app.log`의 `[STARTUP_RECONCILE]`에 남깁니다.
어긋나도 자동으로 고치지 않습니다. 이미 계좌에 있던 종목을 인수하려면 사람이 확인한 뒤
`opening_events_from_balance()`로 OPENING 사건을 기록합니다.

## 일봉 데이터

```powershell
python tools/update_daily_bars.py 005930 000660        # 첫 실행: 약 7년치, 이후: 1페이지 증분
```

- 장중에 실행해도 **당일 미완성 봉은 저장하지 않습니다**(마지막 완성 거래일까지).
- 호출 간격 1초, 429는 대기 후 재시도 (실측: 0.5초 간격 5번째 호출에서 429).
- 저장된 과거 값과 새로 받은 값이 다르면(액면분할 등 수정주가 재계산) 그 종목을 전체 재수집합니다.
- 저장 위치: `data/daily_bars/<종목코드>.csv` + `.meta.json`

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

## 연구(관찰) 계층 — A단계 (주문 없음)

매매 로직(S1 눌림 회복)을 **주문 없이** 신호·관찰 데이터로 먼저 쌓는 단계입니다. 합의 명세: `docs/research_a_stage.md`.

- `domain/research/`: 순수 계산(지표·주봉·시장 환경·S1 평가기). 네트워크·파일·주문 경로를 쓰지 않습니다.
  확인할 수 없는 값은 UNKNOWN(사유 포함)이고, 기준일 이후 데이터는 계산 전에 잘라냅니다.
- 원천 확인(조회 전용, 모의 도메인만): `python tools/probe_research_sources.py` — 종목 목록 필드·위험 상태 후보,
  일봉 거래대금 필드와 단위, 지수 일봉 TR을 실측합니다(TR 이름은 조사 후보). 장 마감 후 권장.
- A2 수집(조회 전용, 모의 도메인만, 허용 TR 3개): `python tools/research_collect.py universe | backfill | update | status | holidays`
  — 종목 목록 스냅숏, 재개 가능한 일봉·지수 백필(2017-01-02부터), 매일 18:10 이후 갱신. 저장은 `data/research/`(git 제외).
  규칙은 `docs/research_a_stage.md`의 A2 절. 저장소 스키마가 바뀌면 새 버전이 기존 DB를 열 때 백업 후 자동 이전합니다
  (연구 DB r5, 관찰 DB s3). 바꾸기 전에 `inspect-unproven`(읽기 전용)으로 무엇이 바뀔지 미리 볼 수 있습니다.
- A4-A S1 관찰 스캔(주문 없음): `update`가 끝나면 자동으로 스캔·보고서(`reports/research/s1/`), 스캔만은 `scan`,
  같은 시각 재현은 `scan --at ... --verify`. 관찰 기록은 `data/research/s1_scans.sqlite3`(git 제외).
  스캔 시각에 알 수 있던 값만 쓰고, 데이터·지수·스냅숏이 불완전하면 후보로 넘기지 않고 보류합니다.
  실행·대표 기록은 계산 계약(설정·정책·계산 버전·lookback·완성 지연·달력)별로 따로 저장합니다.
- A5-1 개장 가격 기록(주문 없음): 거래일 09:05(개장 + 5분)에 `open-check` — 대상 계약(`config/research.yaml`)의 전날
  확정 후보만 현재가·기준가를 조회해 상한 이내·초과·손절가 이하·가격 기준 변경·거래 불가·조회 실패·지연·누락을 구분해
  기록(`data/research/a5_checks.sqlite3`, 보고서 `reports/research/a5/`). 관찰 가격이며 체결이 아닙니다.
- 다음 단계: 조회 전용 상시 실행 관리자(A24-A), 이후 움직임 평가(A5-2), 과거 일괄 스캔(A4-B).

## 사용자 지정 종목 (주문 없음) — 방향 전환 2단계

최종 목표는 사용자가 지정한 국내 종목의 24시간 운영·자동매매. 지금은 대상 선택(수동)·검증·데이터 준비까지. 명세: `docs/watchlist.md`.

- 설정 `config/watchlist.yaml`(git 제외, 예시 `config/watchlist.example.yaml`): 종목마다 관심(켜기·S1 분석·가격대)과
  수동 보유 정보(증권사 잔고 아님). `python tools/watchlist.py init | add | set | enable | disable | holding-close | remove`
  — 바꾼 결과를 검증한 뒤에만 파일을 씁니다.
- 잘못된 설정: 처음이면 감시를 시작하지 않고, 운영 중이면 마지막 정상 버전으로 계속(오류·버전 표시, 신규 매수 차단).
  `apply`·`status`·`history`로 적용 이력 확인(`data/watch/watch.sqlite3`).
- `prepare`: 등록 종목 + KOSPI·KOSDAQ만 일봉 갱신(새 종목은 전체 이력 즉시 수집), 준비 상태 READY/UNKNOWN 판정.
  전체 시장 수집·S1 스캔은 그대로 `tools/research_collect.py`.
- 상시 실행 관리자(W2, 조회 전용): `python tools/watch_daemon.py run | status | stop | report --day D | doctor` — 거래일 달력에 맞춰
  마감 뒤 지정 종목·지수 일봉 준비와 지정 종목 S1 관찰(별도 계약·별도 DB), 다음 거래일 개장 + 5분 가격 기록. 중복 기동·설정 적용 잠금,
  재시작 이어하기, 호출 예산·양보. 명세·Windows 명령·장애별 복구: `docs/watch_daemon.md`.
  계산 PASS와 운영 진입 자격(설정 정상·관심·위험·준비 기준 게이트)을 따로 저장 — 게이트에서 빠진 신호는 개장 후보가 아님.
  호출 상한은 실제 요청마다(토큰·연속조회·재시도 포함) 검사, 예산 소진 시 다음 날 00:00까지 미룸. 미해결 적용 저널이면
  `watchlist.py restore` 또는 `resolve-journal --keep-file`로 사람이 해결할 때까지 신규 진입 차단.
  개장 확인을 재시도·재기동할 때도 아직 가격이 없는 후보의 지금 자격을 다시 확인(부적격이면 조회 안 함).

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
