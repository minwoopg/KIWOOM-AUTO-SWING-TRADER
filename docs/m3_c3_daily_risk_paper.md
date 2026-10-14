# M3 C3 — 일일 손실·실패 주문 한도 · 공통 비용 · paper 전환

GPT 최소판 순서의 마지막 코드 단계. 이 패치부터 `swing_strategy.mode: paper`가 **사전 점검을 통과하면** 기동합니다.
설정을 바꾸기 전까지는 지금처럼 shadow로 돕니다(이 패치는 `config/settings.yaml`을 바꾸지 않음).

## 1. 공통 비용 (`domain/service/trade_costs.py`)

| 경우 | 비용 |
|---|---|
| 체결 근거(ka10076 수수료·세금) 있음 | 실제 값만 — 모델 비용을 더하지 않음 |
| 없음 | 추정: 수수료 0.35%·매도세 0.20%, 원 단위 **올림**(10/13 실측 229,000원 1주: 800 / 800+457 → 추정 802 / 802+458) |
| 한 사건이 여러 매칭에 나뉨 | 최대 나머지 배분 — 나눈 합 = 원래 비용 |

리포트(당일 비용·비용 후 실현손익), 관리 창(비용 탭 "비용(실제 우선·추정)" 열), 일일 손실 한도가 같은 함수를 씁니다.
연구용 Gross/Base/Stress(`domain/cost_model.py`)는 그대로.

## 2. 일일 위험 한도 (`app/daily_risk.py`, 기록 `data/daily_risk.jsonl`)

- 그날 첫 폴링에 **시작 기준**(원장 보유·현재가·현금·시작 평가액) 1회 기록 — 재시작해도 다시 쓰지 않음.
- **C4(GPT R1)**: 시작 기준이 없는데 오늘 체결 사건(BUY·SELL·OPENING)이 이미 있으면 기준을 만들지 않고 그날 신규 매수 보류
  (`BASELINE_MISSING_WITH_TRADES`, 청산은 계속) — 지금 보유로 기준을 만들면 오늘 매수가 두 번 들어감. 다음 거래일에 새 기준.
  M1 수동 주문을 한 날, 기준가 조회 실패로 기준이 늦어지는 사이 체결이 생긴 날이 여기에 해당.
- 지금 수량 ≠ 시작 수량 + 오늘 순체결이면 손익 모름 → 매수 보류.
- 표시 이름: "시작 기준 이후 거래 손익(비용 후, 입출금 제외)" — 첫 폴링 가격 기준이라 전일 종가 대비 갭은 포함하지 않음.
- 일일 손익 = Σ(지금 보유×현재가 + 오늘 매도 − 시작 보유×기준가 − 오늘 매수) − 오늘 비용. 현금을 쓰지 않아 **입출금 무관**.

| 조건 | 결과 |
|---|---|
| 손익 ≤ −`daily_loss_limit`(기본 10,000원) | `commands/halt.json`(by=daily_risk) + HALT 기록 → 신규 매수 중단 |
| 미접수·응답 불명 주문 ≥ `max_failed_orders`(기본 1) | 같음 |
| 시작 기준 없음·현재가 없음·기록 손상 | 그 폴링 신규 매수 보류(DAILY_RISK_UNKNOWN), 중단 파일은 안 씀 |

- 매도·손절은 계속. 중단은 재시작해도 유지, 사람이 `buy_halt.py resume`으로 풀어도 **그날은** HALT 기록 때문에 매수 없음 → 다음 거래일부터.
- shadow에서는 계산·기록만(중단했을 것 `would_halt`) — 중단 파일 안 씀.
- 상태 파일 `daily_risk`: pnl·costs·estimated_costs·failed_orders·limit·block.

## 3. paper 사전 점검

| 점검 | 실패 시 |
|---|---|
| 실행 모드 KIWOOM_PAPER(mockapi·use_mock false·is_paper_trading true), PaperKiwoomBroker, 계좌 설정 | **기동 거부** |
| `allowed_symbols` 1개 이상, 수량 1, `session.entry_cancel_at` | 기동 거부 |
| 미해결 주문 흔적, 원장·잔고·메타 [BLOCK], S1 메타 없는 보유(종목 한정 문제) | 기동은 하되 신규 매수 중단(by=paper_precheck) — 청산은 계속 |
| 잔고 조회 실패, 원장·주문 기록 손상(계좌 전체를 믿을 수 없음) | 기존 엄격 모드대로 **기동 거부** |

실전(is_paper_trading false)은 실행 모드에서 이미 거부.

## 4. 작은 항목

- 체결 근거 재확인 목록 재시작 복구: 오늘 원장 사건 중 근거 없는 것을 첫 폴링에 다시 올림(파일이 원천).
- **C4(GPT R3)**: 체결조회로 확인했지만 근거 파일 저장에 실패한 근거는 `data/fill_evidence_spool.jsonl`(PENDING·DONE·GAVE_UP)에 보관 →
  재시작하면(날짜 무관) 재조회 없이 저장부터. 보관도 실패하면 상태 `spool_error`("근거 미확정"). 번들·관리 창 zip 포함.
- 비용 표시: `ops-est-v1: 수수료 …%·매도세 …%` — 설정 비율을 바꾸면 표시도 실제 적용값.
- 관리 창 zip에 execution_mode.json·daily_risk·strategy_decisions, 번들에 strategy_decisions·shadow_intents·daily_risk·신호 키·`logs/market_basis_*.json`.
- `input_check.market_basis` = `KRX(10/13 실측: 단축코드 = KRX 단독, 수정주가)`.

## 5. paper 켜기 (사용자 결정 후 — GPT C 검토 순서)

1. 실행기 중지 → HTS로 계좌 보유·미체결 확인, `tools/buy_halt.py status`, 관리 창 복구 필요 없음 확인.
2. `daily_risk.jsonl`·신호 키·근거 파일을 지워서 점검을 통과시키지 않음.
3. 아래 설정 후 **paper 설정으로** `--check-only` — shadow 설정의 check-only 통과는 paper 점검 통과가 아님.
4. 005930 한 종목·1주·최대 1포지션. PASS가 없으면 거래 0건도 정상. 첫 목표는 승인 → 주문 → 체결/취소 → 원장·비용 → 청산 → 재시작 일치 완주.

```yaml
swing_strategy:
  mode: paper
  allowed_symbols: ["005930"]
```
그 전에 `python -m app.main --check-only`로 `[STARTUP_PAPER] 사전 점검 통과` 확인.
