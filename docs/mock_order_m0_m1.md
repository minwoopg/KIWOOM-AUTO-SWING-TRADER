# 모의투자 주문 단계 M0·M1 — 실행 모드, 복구 이중 차단, 원장 정정, 1주 수동 주문 실측

GPT 지시서 `swing-w2-to-mock-execution-plan-20261013.md`(main `88686b9` 검토) 커밋 1~3. 전략·S1 연결·일봉 정책(커밋 4~6)은 다음 단계.
**실계좌는 범위 밖** — 실전 주소·실전 플래그는 기동 거부.

## 실행 모드

| 모드 | settings.yaml | 브로커 | 기동 기준 |
|---|---|---|---|
| MOCK_LOCAL | `use_mock: true` | 프로그램 안 가짜 `MockBroker` — 키움 서버에 닿지 않음 | 잔고 실패 등은 경고 후 계속(예전과 같음) |
| KIWOOM_PAPER | `use_mock: false`, `is_paper_trading: true`, `base_url: https://mockapi.kiwoom.com` | `PaperKiwoomBroker` | 잔고·주문 기록·원장 확인 실패면 시작 거부(실전과 같은 엄격 기준) |
| (거부) | 그 밖 — `api.kiwoom.com`, http, 다른 호스트·포트·경로, `is_paper_trading: false` | 만들지 않음 | `ExecutionModeError` |

- `PaperKiwoomBroker`는 단타 원본 `KiwoomBroker`를 바꾸지 않고 상속: 요청마다 주소 검사(https·mockapi.kiwoom.com·기본 포트),
  리다이렉트 따라가지 않음(3xx = 응답 불명 → 주문은 ERROR로 사람 확인), 주문 직전 설정 재검사.
- 계좌 식별: 앱키·시크릿·계좌번호(숫자 8자리 이상)·계좌 라벨 필수, 단타 라벨 `acct-a` 거부. 모의 서버 토큰은 모의투자 앱키로만
  발급되므로 기동 때 mockapi 인증 + 잔고 조회 성공이 '모의 계좌' 실측 근거(계좌번호만으로 모의/실전을 가르는 규칙은 쓰지 않음).
- 세션 요약·`session_status_<날짜>.json`·일일 리포트 세션 요약에 `실행 모드 … | 계좌 라벨 …`.

## 데이터 폴더 실행 모드 표식 `data/execution_mode.json`

MOCK_LOCAL로 쌓인 원장·상태를 키움 모의 계좌의 것처럼 쓰지 않게. 처음 기동 때 만들고, 모드·계좌 라벨이 바뀌면
**원장이 비어 있고 미해결 주문 흔적이 없을 때만** 전환을 기록(이력 남김). 아니면 시작 거부 — 사람이 정리(자동 이관 없음).
지금 사용자 PC는 NullStrategy만 돌아 원장이 비어 있으므로 `use_mock: false`로 바꾸면 첫 기동에서 전환이 기록됨.

## 복구 이중 차단 (ERROR·orphan)

주문 실행부의 ERROR·orphan 게이트에 더해 계좌 안전 한도가 먼저 막음.

| 상황 | 결과 |
|---|---|
| 복구 필요 종목의 매수·매도(강제 매도 포함) | `RECOVERY_REQUIRED` |
| 계좌 어디든 복구 필요 종목이 있을 때 다른 종목 신규 매수 | `ACCOUNT_RECOVERY_REQUIRED` |
| 복구 필요가 아닌 보유 종목 매도 | 허용(신규 매수 보류가 정상 보유 청산을 막지 않음) |

해제는 기존 복구 명령(`commands/ack_error_*.json`·`ack_orphan_*.json`, recovery_id 필수)으로만.

## 주문 경로 잠금

`app.main`·`tools/paper_order.py`·`tools/ledger_review.py`(note·correct)는 같은 잠금 두 개(상태 파일, 계좌 라벨)를 잡음 —
하나가 돌면 나머지는 거부. **작업 스케줄러 실행기(평일 08:40~15:45)가 도는 동안에는 M1 도구가 실행되지 않음.**

## 원장 최소 정정 `tools/ledger_review.py`

```powershell
python tools/ledger_review.py status
python tools/ledger_review.py note 005930 --reason "HTS와 일치 확인"
python tools/ledger_review.py correct 005930 --side BUY --qty 1 --price 251000 --trade-date 2026-10-14 --order-id 0012345 --evidence "HTS 체결내역 10/14 09:12 1주 251,000" --reason "재시작 중 체결 누락"
# 위 출력(미리보기) 확인 뒤 같은 명령 + --confirm
```

- 정정 = 브로커 근거(주문번호·HTS 체결)가 있는 빠진 체결을 원장에 **추가**(BROKER_FILL). 원장 줄 수정·삭제 없음.
  같은 주문번호·수량은 한 번만. 매도 정정은 원장 보유 이하. 원장 손상이면 도구도 거부.
- `data/ledger_reviews.jsonl`(append-only): 검토 ID·시각·모드·계좌 라벨·종목·근거·사유·전후 수량.
- ERROR·orphan을 풀지 않음, 마감 검증을 VERIFIED로 만들지 않음 — 다음 기동의 장부 대조가 다시 판정.

## M1 — 1주 수동 주문 경로 실측 `tools/paper_order.py`

전략과 무관. 하루 수명주기와 **같은 경로**(잔고 → 대조 → 원장 기록 → 안전 한도 → 주문 실행부)로 1주 시장가 한 번.

```powershell
python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014              # 미리보기(네트워크 없음)
python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014 --execute    # 실제 모의 주문
python tools/paper_order.py sell-held 005930 --command-id m1-sell-1014 --execute # 원장 보유 1주 매도
python tools/paper_order.py status
```

| 규칙 | 내용 |
|---|---|
| 모드 | KIWOOM_PAPER만 |
| 수량·유형 | 1주 시장가 고정(M1 경로 시험 — 자동 S1 매수는 이후 상한 지정가) |
| 시간 | 안전 한도 그대로: 정규장 09:05~15:15 |
| 명령 ID | 한 번만. `data/paper_orders.jsonl`에 네트워크 전 STARTED, 끝나면 FINISHED(결과). 기록에 있으면 결과와 상관없이 다시 안 냄 |
| 기동 점검 | app.main과 같음. 미해결 주문 흔적·원장 오류·장부 대조 불일치(계좌에 원장 밖 보유 포함)면 REFUSED |
| 확인 | 체결·원장 기록·주문 종료를 최대 `--max-wait`(기본 180초), `--poll`(기본 3초, 2 이상) 간격 |
| 결과 | FILLED / CLOSED_NO_FILL / UNRESOLVED / NOT_ACCEPTED(응답 불명 포함) / DENIED(안전 한도·실행부) / REFUSED |
| 응답 원문 | 비밀값(토큰·앱키·시크릿·계좌번호) 지워 `data/m1_fixtures/<명령 ID>/NNN_<api-id>.json` |
| 재전송 | 없음. UNRESOLVED·응답 불명은 다음 기동에서 ERROR 복원 → HTS 확인 → ack_error / 원장 정정 |

통과 기준(GPT M1): 실제 키움 모의 주문번호와 응답 fixture, 매수·매도 양방향 체결·잔고·원장 일치. 당일 왕복 허용.

## 바꾸지 않은 것

전략(`NullStrategy`), S1·게이트·관리자(W2 조회 전용), 단타 원본과 같은 경로 코드의 동작(동등성 18/18), 일봉 수집 시각.
