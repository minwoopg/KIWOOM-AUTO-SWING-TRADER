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
- 주봉: 그 주 마지막 **예정** 세션 마감에 완성. `week_end_session`·`session_closed_at`·`available_at`을 따로 기록.
  * 시각은 모두 **Asia/Seoul 기준 naive datetime**(`now_local()` 규약).
  * 예정 세션은 주 단위 일정(`WeekSchedule`)에서만 가져옴 — 일봉용으로 기준일 뒤를 잘라낸 세션 목록을 쓰지 않음.
    `ExplicitWeekSchedule`은 `known_through`까지 모든 평일이 세션 또는 휴장일로 명시돼야 함(잘린 목록이면 오류).
  * `available_at`은 모드를 명시해 계산(두 모드 혼용 금지, `data_delay` 음수 거부).
    - `ASSUMED_DELAY`(백필): 마지막 세션 정규장 종료(특수 운영일 반영) + 30분. **가정 분석임을 기록에 유지.**
    - `OBSERVED`(앞으로 쌓는 기록): max(마지막 세션 종료, **그 주 모든 세션 봉의 실제 확보 시각**).
      하나라도 없으면 `READY_TIME_UNKNOWN`, 확보 시각이 그 세션 종료 전이면 `READY_BEFORE_SESSION_CLOSE`
      (장중 미완성 봉) — 둘 다 불완전 주. 30분 가정으로 메우지 않음.
  * 진행 중인 주는 제외. **끝났지만 입력이 준비 안 된 주는 `DATA_NOT_READY` 불완전 자리로 남김**
    (가장 최근에 끝난 주 하나만 "아직 도착 전"으로 잘라냄). 중간 주를 빼고 더 오래된 주로 34주를 채우지 않음.
  * 한 주 전체가 예정 휴장이면 항목 없이 다음 주에 `gap_weeks_before`로 기록. `weekly_trend`는 34주 창 안의
    주 시작 간격이 7×(1+gap_weeks_before)일이 아니면 `WEEK_SEQUENCE_GAP`, 불완전 주가 있으면 `INCOMPLETE_WEEK`로 UNKNOWN.
- **A2 수집기가 넘겨야 할 확보 시각 (A13-Q2)**
  * 일봉마다 `ready_at` = 그 봉이 **완성 봉으로 처음 들어온 조회 응답의 수신 시각**(Asia/Seoul naive).
    장중 조회 응답에 포함된 당일 봉(ka10081은 장중에도 당일 봉을 줌)은 완성 봉이 아니므로 ready_at으로 쓰지 않음 —
    그 세션 종료 후 다시 받은 응답의 시각을 씀. 누락 봉을 나중에 복구하면 복구한 조회 시각이 그 봉의 ready_at.
  * 저장: 봉 값과 함께 `ready_at`·`run_type`(BACKFILL / FORWARD)을 보존. 한 번 기록한 ready_at은 덮어쓰지 않음.
  * 평가: FORWARD 기록은 `weekly_bars(..., mode="OBSERVED", data_ready_at={날짜: ready_at})`,
    BACKFILL 기록은 `mode="ASSUMED_DELAY"`(ready_at 미전달). 한 주에 두 종류가 섞이면 OBSERVED로 계산해
    백필 봉은 확보 시각 없음 → 불완전으로 처리(가정 시각을 섞지 않음).
- **시장 조건과 지수 원천 (A13-Q1)**
  * 스캐너 기본 경로는 `evaluate_s1(..., market=None)` — RS에 쓴 지수 View로 시장 조건을 직접 계산.
  * 지수 View는 `SeriesView(..., source_id="INDEX:KOSPI:001" / "INDEX:KOSDAQ:101")`처럼 원천 식별자를 붙임.
  * 종목의 당시 소속 시장 지수는 `Eligibility.market_index_id`로 넘김 — 지수 View와 다르면 RS·시장 조건 UNKNOWN
    (`INDEX_NOT_STOCK_MARKET`). 당시 소속을 모르는 백필은 None(가정 표시).
  * 시장 판정을 캐시해 `market=`로 넘길 때는 `MarketRegime.index_id`와 View `source_id`가 둘 다 있고 같아야 함 —
    아니면 `INDEX_SOURCE_MISMATCH`로 UNKNOWN. `classify_market(view, index_id)`에 View와 다른 식별자를 주면 오류.
- EMA: 최초 N개 종가 SMA로 시작값 → **추가 5N번 갱신 후부터 유효**(필요 봉 수 6N: EMA20=120, EMA50=300).
  `계산 버전 · 입력 시작일 · 입력 해시 · 평가 기준일 · 기록 시각` 보존.
- 첫 재접촉 사건: 매 스캔 처음부터 순차 재계산. **사건 ID = 종목 + 시작 사건 날짜 + 규칙 버전**(입력 해시는 별도 필드). 최초 기록 보존.
- 보고: 신호 수, 서로 다른 종목 수, 신호 발생 거래일 수, 연속·겹침 신호, 시장 환경별, 결과 성숙 표본 수를 따로.

## 진행 순서
A1 원천 실측(`tools/probe_research_sources.py`) · A3 순수 계산(`domain/research/`) → A2 수집 → A4 스캔·저장·보고 → A5 다음날 확인·이후 움직임.

## A3 구현된 정의 (`domain/research/`)
| 모듈 | 내용 | 버전 |
|---|---|---|
| `series.py` | `ResearchBar`(유한한 양수 가격·bool 아닌 정수 수량·실제 거래대금 원/None), `SeriesView`(t 이후 차단, 세션 목록 오름차순·중복 검증, 세션 기준 창, INSUFFICIENT_HISTORY / DATA_GAP 구분) | — |
| `features.py` | SMA·기울기·ret·RS·ATR14(단순평균)·extension·close_location·volume_ratio·거래대금 평균·tr_contraction·volume_dryup(t 제외)·high/low252·return_atr·narrow_range7·EMA(6N) | f1 |
| `market.py` | UNKNOWN → RISK_OFF → RISK_ON → MIXED 우선순위 | m1 |
| `weekly.py` | 완성 주봉(주 단위 예정 일정, 가정/관측 모드별 사용 가능 시각 — 관측은 주 전체 봉 확보 시각, 불완전·일정 불명·준비 안 된 주 자리 유지, 전체 휴장 주 간격), SMA30W·slope4W(34주 연속성 검사), UP/DOWN_PROXY, 비유한값 UNKNOWN | w3 |
| `s1.py` | pattern / eligibility / market / **stop_valid** / eligible_signal(네 묶음 모두 PASS) — 다음날 확인 후보는 eligible_signal만 사용. 종목·지수·시장 판정 **기준일 일치 필수**(불일치 AS_OF_MISMATCH, 수익률 구간 날짜 불일치 SESSION_ALIGNMENT_MISMATCH → UNKNOWN). 시장 조건은 기본적으로 같은 지수 View에서 계산, 외부 판정은 원천 식별자 일치 필수(INDEX_SOURCE_MISMATCH / INDEX_NOT_STOCK_MARKET). 조건별 값·사유, 참고 손절가·진입 상한·위험 비율, 관찰값, 결정적 후보 정렬, 설정 검증·해시 | s1_pullback_v0.1 |
