# M3 C2 — 진입 메타 저장 · E1 청산 · 신호 키 상태

GPT 최소판 요청 4(청산)·B 검토 C 요청(진입 메타 영속화, 신호 키 복구). **모드는 여전히 shadow만** — `paper`는 C3
(일일 손실 한도·모의 전송 사전 점검) 전까지 기동 거부.

## 1. 진입 메타 (포지션 메타, `data/state.json`의 positions)

| 시점 | 내용 |
|---|---|
| 매수 **전송 전** | `stop_price = floor(stop_ref)`, `origin=ORDER`, `strategy_id=s1_pullback_v0.1`, meta: signal_id·signal_key·signal_date·contract_hash·scan_run_id·entry_cap·stop_ref·limit_price·run_id, `entry_status=SUBMITTING`. 실행기의 주문 의도 기록과 **같은 저장**에 실림 — 전송 직후 꺼져도 손절가가 남음 |
| 접수 | `entry_status=ACCEPTED`, order_id |
| 미접수(거부·전송 전 차단) | 보유가 없으면 메타 삭제 |
| 응답 불명 | 메타 유지(`entry_status=UNKNOWN`) — 접수됐다면 체결 시 손절가 필요. 종목은 ERROR로 사람 확인 |
| 체결(원장 보유 > 0) | 폴링마다 `entry_trade_date = 원장 첫 체결일`, `entry_status=FILLED` (재시작 후에도 원장에서 다시 맞춤) |
| 미체결 취소·청산 완료 | 보유 0 + 미해결 주문 없음 → 기존 META_ORPHAN 정리로 삭제 |

## 2. E1 청산 (`app/s1_strategy.py`)

정규장 폴링마다 **진입보다 먼저** 판단. 신규 매수 중단·신호 키 손상·후보 오류와 무관하게 동작.

| 규칙 | 조건 | 주문 |
|---|---|---|
| 손절 `E1_STOP` | 유효한 현재가(양의 정수) ≤ 손절가 | 원장 보유 전량 시장가, **forced**(안전 한도의 주문 시각 제한 예외 → 09:00부터) |
| 시간 `E1_TIME` | 진입 거래일 = 0일, **10번째 거래일 마감 20분 전**(일반 15:10) 이후. 지나쳤으면 다음 정규장 | 전량 시장가, **forced(C4)** — 다음 날 09:00부터·15:15 이후 정규장에서도 |

매도하지 않는 경우(판단 기록에 사유):
- 메타 없음·다른 전략·손절가 없음 → `META_MISSING` (사람 확인. 최대 보유 1이라 신규 매수도 없음)
- 미해결 주문·복구 필요·장부 불일치 종목 → `EXIT_SKIP_BLOCKED`
- 원장 수량 ≠ 잔고 수량 → `QTY_MISMATCH`
- 현재가 조회 실패 `PRICE_FAILED`, 0원·없음 `NO_PRICE` — **손절·시간 청산 모두** 보류(C4: 손절가를 관측가 대신 쓰지 않음).
  시간 청산 기한은 남아 다음 유효 조회에서 실행(`time_due` 기록)
- 미접수 매도 뒤 `EXIT_BACKOFF` — 종목별 60·120·240…초(최대 15분) 뒤 다시(C4, 메모리 — 재시작하면 처음부터)

## 3. 신호 키 (`data/strategy_signal_keys_<mode>.jsonl`)

- 의도 전에 `CLAIMED` 한 줄 → 실행기 결과로 상태 줄 추가: `SHADOW` / `GUARD_REJECTED:<사유>` / `SUBMITTED` /
  `NOT_ACCEPTED[:<차단>]` / `UNKNOWN`. 키의 상태 = 마지막 줄. 어떤 상태든 같은 신호로 다시 진입하지 않음.
- 읽을 수 없는 줄이 하나라도 있으면 그날 신규 진입 없음(`SIGNAL_KEYS_UNREADABLE`). 보유 청산은 계속. 사람이 파일을 고친 뒤 재개.

## 4. 판단 기록 (`data/strategy_decisions.jsonl`)

추가 이벤트: `E1_STOP`, `E1_TIME`, `META_MISSING`, `EXIT_SKIP_BLOCKED`, `QTY_MISMATCH`, `PRICE_FAILED`, `NO_PRICE`,
`SIGNAL_KEYS_UNREADABLE`, `RESULT_BUY`/`RESULT_SELL`(실행기 결과 — 같은 상태 반복은 한 번만).

## 5. 시험 — `test_e1_exit.py` (18건, 가짜 브로커·임시 폴더)

진입 → 체결 → 손절 / 손절가+1 유지 / 재시작 후 저장 메타로 손절 / 9번째 거래일 매도 없음·10번째 15:10 매도 /
시간 지나친 뒤 다음 날 / 매수 중단 중 손절 / 가격 실패·0원 / 메타 없음·다른 전략 / 09:30 취소 후 재진입 없음 /
거부·응답 불명 / 키 손상(진입 중단·청산 유지).
