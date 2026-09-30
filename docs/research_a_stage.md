# 연구(관찰) A단계 — 합의 명세 요약

작성 2026-09-30. 원문: 사용자 제공 `swing_strategy_llm_request_20260930.md`(요청서)와
`swing_strategy_research_extension_20260930.md`(보충안), 그 뒤 Claude·GPT 검토로 합의한 수정.
**수치는 검증 전 가설이며 수익성 근거가 아닙니다. A단계에는 주문이 없습니다.**

## 범위
| 단계 | 내용 | 주문 |
|---|---|---|
| A (지금) | 목록·지수·일봉·거래대금 수집, 지표, S1 신호, 다음날 확인, 이후 움직임 관찰 | 없음 |
| B | 비용·갭·자금 점유 백테스트, 가상 포지션 장부 | 없음 |
| C | 검증된 S1을 Strategy → AccountGuard → OrderExecutor에 연결, 모의 운영 | 별도 지시 후 |

C 전 필수 후속: 원장 정정 도구, ERROR·orphan 종목 guard 이중 차단.

## 합의 사항
- **주 가설은 S1_BASE(`s1_pullback_v0.1`) 하나.** S1 변형·B20·S3·첫 재접촉은 탐색용(다음 묶음). 최고 성과 변형을 사후 선택하지 않음.
- 수집: **전체 보통주** 원천 데이터. 09:05 가격 확인만 상위 20개.
- 백필: 페이지 수가 아니라 **평가 시작일 이전 준비 구간까지** 연속 조회. API 이력이 끝나면 실제 범위·부족 사유 기록.
  백필 기록과 앞으로 관측한 기록은 `run_type`으로 분리. 생존 편향·수집 대상 선정 편향 표시.
- 백필 자격: 당시 확보된 값(거래대금·지수)으로 유동성·시장 조건은 판정. 과거 위험 상태는 UNKNOWN.
  시장 소속·증권 유형도 당시 값을 모르면 UNKNOWN(현재 목록을 과거에 적용한다면 가정임을 표시).
- 달력: 수집 시작 연도(준비 구간 포함)부터 2026년까지. 과거 휴장일은 지수 날짜에서 뽑아 공휴일·근로자의 날·
  연말 휴장·거래소 지정 휴장·선거일 등과 대조한 뒤 사람이 확인해 `krx_calendar.yaml`에 추가.
- 주봉: 그 주 마지막 세션 마감에 완성. `week_end_session`과 `available_at`(마감 + 데이터 확보 시각)을 따로 기록.
- EMA: 최초 N개 종가 SMA로 시작값 → **추가 5N번 갱신 후부터 유효**(필요 봉 수 6N: EMA20=120, EMA50=300).
  `계산 버전 · 입력 시작일 · 입력 해시 · 평가 기준일 · 기록 시각` 보존.
- 첫 재접촉 사건: 매 스캔 처음부터 순차 재계산. **사건 ID = 종목 + 시작 사건 날짜 + 규칙 버전**(입력 해시는 별도 필드). 최초 기록 보존.
- 보고: 신호 수, 서로 다른 종목 수, 신호 발생 거래일 수, 연속·겹침 신호, 시장 환경별, 결과 성숙 표본 수를 따로.

## 진행 순서
A1 원천 실측(`tools/probe_research_sources.py`) · A3 순수 계산(`domain/research/`) → A2 수집 → A4 스캔·저장·보고 → A5 다음날 확인·이후 움직임.

## A3 구현된 정의 (`domain/research/`)
| 모듈 | 내용 | 버전 |
|---|---|---|
| `series.py` | `ResearchBar`(실수 가격·실제 거래대금 원/None), `SeriesView`(t 이후 차단, 세션 기준 창, INSUFFICIENT_HISTORY / DATA_GAP 구분) | — |
| `features.py` | SMA·기울기·ret·RS·ATR14(단순평균)·extension·close_location·volume_ratio·거래대금 평균·tr_contraction·volume_dryup(t 제외)·high/low252·return_atr·narrow_range7·EMA(6N) | f1 |
| `market.py` | UNKNOWN → RISK_OFF → RISK_ON → MIXED 우선순위 | m1 |
| `weekly.py` | 완성 주봉(달력 기준 마지막 세션, 불완전 주 표시), SMA30W·slope4W, UP/DOWN_PROXY | w1 |
| `s1.py` | pattern / eligibility / market / eligible_signal (PASS·FAIL·UNKNOWN), 조건별 값·사유, 참고 손절가·진입 상한·위험 비율, 관찰값, 결정적 후보 정렬, 설정 해시 | s1_pullback_v0.1 |
