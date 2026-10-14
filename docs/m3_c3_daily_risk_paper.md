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
| 미해결 주문 흔적, 원장·잔고·메타 [BLOCK], S1 메타 없는 보유 | 기동은 하되 신규 매수 중단(by=paper_precheck) — 청산은 계속 |

실전(is_paper_trading false)은 실행 모드에서 이미 거부.

## 4. 작은 항목

- 체결 근거 재확인 목록 재시작 복구: 오늘 원장 사건 중 근거 없는 것을 첫 폴링에 다시 올림(파일이 원천).
- 관리 창 zip에 execution_mode.json·daily_risk·strategy_decisions, 번들에 strategy_decisions·shadow_intents·daily_risk·신호 키·`logs/market_basis_*.json`.
- `input_check.market_basis` = `KRX(10/13 실측: 단축코드 = KRX 단독, 수정주가)`.

## 5. paper 켜기 (사용자 결정 후)

```yaml
swing_strategy:
  mode: paper
  allowed_symbols: ["005930"]
```
그 전에 `python -m app.main --check-only`로 `[STARTUP_PAPER] 사전 점검 통과` 확인.
