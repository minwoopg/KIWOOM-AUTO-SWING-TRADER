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
  * 진행 중인 주는 제외. **끝났지만 입력이 준비 안 된 주는 `DATA_NOT_READY` 불완전 자리로 남김**.
    가장 최근에 끝난 주 하나만, **다음 거래일 0시 전**이면 "아직 도착 전"(정상 대기)으로 잘라냄. 다음 거래일이
    시작됐는데도 미확보면 `DATA_NOT_READY:OVERDUE`(장애 지연)로 남아 추세 UNKNOWN. 다음 거래일을 모르면(일정 불명)
    판단할 수 없으므로 자리를 남김(w4, Q-R2). 중간 주를 빼고 더 오래된 주로 34주를 채우지 않음.
  * 한 주 전체가 예정 휴장이면 항목 없이 다음 주에 `gap_weeks_before`로 기록. `weekly_trend`는 34주 창 안의
    주 시작 간격이 7×(1+gap_weeks_before)일이 아니면 `WEEK_SEQUENCE_GAP`, 불완전 주가 있으면 `INCOMPLETE_WEEK`로 UNKNOWN.
- **A2 수집기가 넘기는 확보 시각 (A13-Q2 → A2-R1에서 정정)** — 값마다 세 가지 시각을 따로 둡니다.
  * `received_at`: **그 값**(그 revision의 그 날짜 봉)이 들어 있던 응답 페이지의 수신 시각. BACKFILL도 기록.
    장중 응답의 당일 봉(미완성)은 저장하지 않으므로 이 시각이 될 수 없음.
  * `available_at` = max(received_at, 그 revision의 활성 시각) — **주봉 OBSERVED의 data_ready_at으로 쓰는 값**.
    재수집으로 바뀐 값은 새 revision이 활성화된 뒤에만 사용 가능. 누락 봉을 나중에 복구하면 복구 조회 시각.
  * `first_ready_at`: 그 날짜의 완성 봉을 처음 확보한 시각(모든 revision). **기록용** — 새 값의 사용 가능 시각으로 쓰지 않음.
  * 평가: 수집 이후 시점(현재·앞으로)은 `research_series(sid, as_of=X)`로 X에 활성이던 revision의 값만 받아
    `weekly_bars(..., mode="OBSERVED", data_ready_at=available_at)`. 수집 전 과거 시점은 확보 시각이 모두 X 뒤이므로
    OBSERVED로는 UNKNOWN — 과거 재현은 현재 revision + `mode="ASSUMED_DELAY"`로 하고 **가정 분석**으로 표시.
- **시장 조건과 지수 원천 (A13-Q1)**
  * 스캐너 기본 경로는 `evaluate_s1(..., market=None)` — RS에 쓴 지수 View로 시장 조건을 직접 계산.
  * 지수 View는 `SeriesView(..., source_id="INDEX:KOSPI:001" / "INDEX:KOSDAQ:101")`처럼 원천 식별자를 붙임.
  * 종목의 당시 소속 시장 지수는 `Eligibility.market_index_id`로 넘김 — 지수 View와 다르면 RS·시장 조건 UNKNOWN
    (`INDEX_NOT_STOCK_MARKET`). 당시 소속을 모르는 백필은 None(가정 표시).
  * 시장 판정을 캐시해 `market=`로 넘길 때는 `MarketRegime.index_id`와 View `source_id`가 둘 다 있고 같아야 함 —
    아니면 `INDEX_SOURCE_MISMATCH`로 UNKNOWN. 이름·날짜가 같아도 **같은 View로 다시 계산한 판정과 모든 값이 같아야**
    사용(다르면 `MARKET_INPUT_MISMATCH`, Q-R1). `classify_market(view, index_id)`에 View와 다른 식별자를 주면 오류.
- EMA: 최초 N개 종가 SMA로 시작값 → **추가 5N번 갱신 후부터 유효**(필요 봉 수 6N: EMA20=120, EMA50=300).
  `계산 버전 · 입력 시작일 · 입력 해시 · 평가 기준일 · 기록 시각` 보존.
- 첫 재접촉 사건: 매 스캔 처음부터 순차 재계산. **사건 ID = 종목 + 시작 사건 날짜 + 규칙 버전**(입력 해시는 별도 필드). 최초 기록 보존.
- 보고: 신호 수, 서로 다른 종목 수, 신호 발생 거래일 수, 연속·겹침 신호, 시장 환경별, 결과 성숙 표본 수를 따로.

## 진행 순서
A1 원천 실측(`tools/probe_research_sources.py`) · A3 순수 계산(`domain/research/`) → A2 수집 → A4 스캔·저장·보고(A4-A 앞으로의 신호 관찰 완료, A4-B 과거 일괄) → A5 다음날 확인·이후 움직임.

## A3 구현된 정의 (`domain/research/`)
| 모듈 | 내용 | 버전 |
|---|---|---|
| `series.py` | `ResearchBar`(유한한 양수 가격·bool 아닌 정수 수량·실제 거래대금 원/None, 거래량 0 = `no_trades`), `SeriesView`(t 이후 차단, 세션 목록 오름차순·중복 검증, 세션 기준 창, INSUFFICIENT_HISTORY / DATA_GAP / **NO_TRADES** 구분) | — |
| `features.py` | SMA·기울기·ret·RS·ATR14(단순평균)·extension·close_location·volume_ratio·거래대금 평균·tr_contraction·volume_dryup(t 제외)·high/low252·return_atr·narrow_range7·EMA(6N). f2: 거래 없는 봉 정책 | f2 |
| `market.py` | UNKNOWN → RISK_OFF → RISK_ON → MIXED 우선순위 | m1 |
| `universe.py` | 종목 목록 분류(증권 유형·현재 위험 표시 합집합·수집 대상/현재 자격 분리), 정책 버전·해시. u2: 빈 state 보류, state 위험 범주 전체·모르는 토큰 보류 | u2 |
| `holiday_candidates.py` | 지수 날짜 → 과거 휴장일 후보·추정 이름(사람 확인용 초안) | — |
| `weekly.py` | 완성 주봉(주 단위 예정 일정, 가정/관측 모드별 사용 가능 시각 — 관측은 주 전체 봉 확보 시각, 불완전·일정 불명·준비 안 된 주 자리 유지, 전체 휴장 주 간격), SMA30W·slope4W(34주 연속성 검사), UP/DOWN_PROXY, 비유한값 UNKNOWN. w4: 최근 주 장애 지연은 OVERDUE 자리 | w4 |
| `s1.py` | pattern / eligibility / market / **stop_valid** / eligible_signal(네 묶음 모두 PASS) — 다음날 확인 후보는 eligible_signal만 사용. 종목·지수·시장 판정 **기준일 일치 필수**(불일치 AS_OF_MISMATCH, 수익률 구간 날짜 불일치 SESSION_ALIGNMENT_MISMATCH → UNKNOWN). 시장 조건은 기본적으로 같은 지수 View에서 계산, 외부 판정은 원천 식별자 일치·재계산 값 일치 필수(INDEX_SOURCE_MISMATCH / MARKET_INPUT_MISMATCH / INDEX_NOT_STOCK_MARKET). 조건별 값·사유, 참고 손절가·진입 상한·위험 비율, 관찰값, 결정적 후보 정렬, 설정 검증·해시 | s1_pullback_v0.1 |

## A2 수집 (`infra/research/`, `tools/research_collect.py`) — 2026-09-30

### 결정 (사용자·GPT 합의)
| 항목 | 결정 |
|---|---|
| 지수 가격 | ka20006 OHLC 모두 **÷100** (공식 명세: 소수점 뺀 100배 값). KOSPI 실제 값 독립 확인은 별도 |
| 투자주의·투자주의환기종목 | 초기 S1 후보에서 제외(auditInfo≠정상이면 위험). 원래 값·제외 사유 보존 |
| 외국기업 | 초기 S1·수집 대상 제외(유형 FOREIGN). 분류 정책(u2, 버전·해시)으로 기록 |

### 원천·단위
- ka10099 종목 목록(mrkt_tp 0/10, 한 페이지에 전체), ka10081 종목 일봉(upd_stkpc_tp=1), ka20006 지수 일봉. 이 셋만 허용.
- 거래대금(trde_prica)은 **백만원** → 원 환산(×1,000,000). 정밀도는 백만원 단위, 원천 반올림 방식은 미확인(오차 범위 단정 안 함).
- 지수 거래량 단위는 미확인(S1은 안 씀). 저장소에는 원천 정수를 그대로 두고 배율·단위는 읽을 때 적용.
- 2026-09-30 12:31 스냅숏 기준: 주식 2,740 → 우선주 추정 114·스팩 67·외국기업 15 제외 → **수집 대상 2,544**,
  그중 현재 위험 표시 257, 현재 자격 2,287(유동성·이력·패턴 검사 전). state에만 관리종목인 행은 전체 목록 84 / 주식 82.

### 수집 대상과 신호 자격 분리 (보완 2)
- 수집 대상 = 보통주 전체(**현재 위험 표시 종목 포함**) + 지수 2개. 현재 상태로 과거 표본을 고르지 않음.
- 현재 자격(eligible_now)은 그 스냅숏을 관측한 시점에만 유효. 백필 날짜의 위험 상태는 UNKNOWN.
- 위험 표시 = auditInfo≠정상 ∪ state 토큰 ∪ orderWarning≠0. **orderWarning은 원래 숫자로만 기록**
  (ORDER_WARNING:5 등) — 관리·정지 등으로 번역하지 않음(보완 3).
- state(u2, A2-R4): '|'로 나눈 토큰에 합의한 위험 범주(관리종목·거래정지·투자주의·환기·투자경고·투자위험·단기과열·
  정리매매)가 있으면 STATE:<토큰>. 정상 토큰(증거금N%·담보대출·신용가능)이 아닌 모르는 토큰은 STATE_UNRECOGNIZED(보류).
- 필드가 없거나 비어 있거나 공백뿐이면 *_MISSING(보류). 모두 현재 자격만 막고 수집 대상(collect)은 바꾸지 않음.
  2026-09-30 실측 집계(2,544 / 257 / 2,287)는 u1과 u2가 같음(실측 state 토큰은 모두 정상·관리·정지).
- 백필 작업에 선정 기준·생존 편향(현재 상장 종목만) 문구를 남김.

### 거래 없는 봉 (보완 1)
- 거래량 0인 봉은 원본대로 저장하고 quality=NO_TRADES로 표시(예: 삼성전자 2018-04-30·05-02·05-03, OHLC 53,000).
  거래정지로 단정하지 않음.
- 계산 정책(feature f2): 기준일이면 NO_TRADES_AT_T(그날 신호·체결 가정 없음), 창 안에 있으면 UNKNOWN(NO_TRADES:<날짜>),
  EMA 연속 구간도 끊김. → S1은 최근 160세션 안에 거래 없는 봉이 있으면 **HISTORY 조건이 UNKNOWN**(다른 조건이
  FAIL이면 최종은 FAIL일 수 있음). A4 보고서에 이 정책 때문에 보류된 종목·사건 수를 따로 집계. 완화는 버전을 올려 별도로.
- 주봉은 종가 기반 관찰값이라 합산하고 no_trade_days로 표시.
- 다음날 확인(A5)에서 거래 없는 날의 진입·체결은 가정하지 않음.

### 완성 봉과 장중 스냅숏 (보완 6)
- 날짜 d의 봉은 응답 수신 시각 ≥ d의 정규장 종료 + **160분**(15:30 종료면 18:10, 시간외 단일가 18:00 뒤)일 때만 저장.
  그 전 당일 봉은 버리고 사유(INTRADAY)를 남김. 달력이 d를 모르면 저장 안 함(CALENDAR_UNKNOWN). 160분은 보수적 잠정값.
- 종목 목록은 snapshot_date와 **observed_at(수신 시각)**·장 단계를 함께 저장. 원래 응답 전체는 gzip으로 보존.
- A4 규칙(예정): t일 평가에는 t일 장 마감 뒤~다음 거래일 개장 전에 관측한 스냅숏만 현재 자격으로 사용.
- 주봉 가정 지연(weekly data_delay 기본 30분)은 백필 해석용. A4 스캐너는 수집 완성 기준과 같은 값을 넘김.

### 백필 종료·부족 사유 (보완 5)
- 페이지 수가 아니라 **필요 시작일(기본 2017-01-02)** 이하 날짜를 받으면 종료(2019년 평가 + EMA50 300봉·252일 준비 구간).
  A1 실측의 6페이지 cont-yn=Y는 "이력 끝"이 아니라 실측 상한 도달이었음.
- 원천이 먼저 끝나면 상장일로 구분: LISTED_AFTER_START(상장일 > 필요 시작일) / HISTORY_END(그 밖 — 원천 이력 한계).
  안전 상한(기본 8페이지)에 걸리면 PAGE_CAP. 부족해도 받은 만큼 저장하고 종목별 사유를 series.coverage에 남김.
- 규모: 약 2,546 시계열 × 4~5페이지 ≈ 1.1만 호출, 1초 간격 약 3시간. 종목 단위로 재개 가능.

### 수정주가 기준 (보완 4)
- 백필 작업은 만들 때 base_dt를 고정(upd_stkpc_tp=1). 중단 후 다른 날 재개해도 같은 base_dt로 조회.
- 한 종목은 모든 페이지를 한 번에 받아 한 트랜잭션에 저장 — 중간에 끊기면 그 종목은 저장되지 않음.
- series에 조정 기준(upd_stkpc_tp·base_dt)·조회 시각·revision, 매일 확인한 기준일(verified_base_dt) 저장.
- 매일 갱신: 첫 페이지를 오늘 base_dt로 받아 저장분과 겹치는 구간을 비교. **모두 같을 때만** 새 날짜를 FORWARD로 추가.
  이것은 **첫 페이지 범위(약 600거래일) 안의 정합 검사**이며, 그보다 오래된 과거 정정까지 확인한 것은 아님
  (필요하면 별도 주기의 전체 재검증을 둠).
- 값 변경·날짜 소실·새 날짜 출현이 있으면 새 기준으로 전체를 다시 받고, **후보를 검증한 뒤에만** 교체(A2-R2):
  비어 있지 않음 · 저장된 마지막 날짜까지 포함 · 필요한 시작일(또는 저장된 첫 날짜)까지 포함 · 변경을 발견한 첫 페이지
  값과 일치. 통과하면 겹친 값이 같을 때 EXTEND(없는 날짜만 추가), 다르면 REBASE(통째 교체, revision+1, 이전 값 보존).
- 검증 실패·재수집 조회 실패: 값·조정 기준일·확인 기준일·revision을 **그대로 두고** integrity=REBASE_REQUIRED,
  VERIFY_FAILED 기록. 정상 갱신으로 세지 않음(update 종료 코드 1). 그 상태에서는 새 날짜를 붙이지 않고(append 거부),
  다음 갱신 때 바로 전체 재수집을 다시 시도. A4는 REBASE_REQUIRED 시계열을 UNKNOWN으로 다뤄야 하며,
  시점 재평가에서는 그 시각의 상태(정합성 이력)로 판단함.
- 새 시계열의 첫 저장(INIT)은 부족해도 받은 만큼 저장(SHORTFALL). 기존 시계열을 다시 받는 백필은 위 검증을 거치며
  실패하면 SHORTFALL이 아니라 ERROR(REBASE_FAILED).
- base_dt 고정은 재현성을 위한 기록입니다. 원천이 base_dt와 무관하게 최신 조정을 적용하더라도 일관성은
  "한 종목 = 한 번의 연속 조회" + "매일 겹침 구간 전체 비교"로 보장됩니다(어긋나면 재수집·교체).
- **수정가격 ≠ 과거 실제 체결가격.** 분할·증자 등으로 과거 가격이 다시 계산된 값이므로, 과거 호가·체결 가능 여부·
  금액 기준(최소 주문금액 등)을 수정가격으로 판단하면 안 됨. 거래대금(원)은 조정되지 않은 실제 금액.
  앞으로 A4가 스캔 당시 관측한 가격을 따로 남기면 나중에 조정 비율로 대조.

### 값 revision과 시각 (A2-R1, 저장소 스키마 r2)
- 수정 기준일(base_dt)·수집 종류(run_type)·실제 값 확보 시각은 서로 다른 정보로 따로 저장.
- `received_at`(행마다 그 페이지의 수신 시각, BACKFILL 포함) / `available_at`(= max(received_at, revision 활성 시각)) /
  `first_ready_at`(그 날짜 완성 봉 최초 확보, 기록용). 새 revision의 활성 시각 = 재수집 마지막 페이지 수신 시각.
- 재수집(REBASE)으로 바뀐 값은 새 revision 활성 전 평가에 쓰이지 않음(10/2에 정정한 값이 10/1 평가에 들어가지 않음).
  run_type·first_ready_at은 날짜의 이력으로 보존.
- `research_series(sid, as_of=X)`: X에 활성이던 revision(series_revision)을 골라 available_at ≤ X인 봉만 반환
  → 10/1 재평가는 이전 값·이전 revision으로 당시와 같은 결과. `as_of=None`은 현재 revision 전체(가정 분석용).
- 매일 갱신 구간에서 누락됐다가 나중에 나타난 봉은 FORWARD·사용 가능 시각 = 복구 조회 시각.
- **정합성 이력 (스키마 r3)**: 재수집 실패(REBASE_REQUIRED)·복구(OK)를 시각과 함께 `series_integrity`에 남김.
  `research_series(as_of=X)`는 `query_mode=AS_OF`와 **X 시각의 integrity**를 따로 돌려줌 — 9/30 정상 → 10/1 실패 →
  10/2 복구 뒤에도 10/1 20:00 조회는 REBASE_REQUIRED(A4는 UNKNOWN으로 보류). 조회에 쓴 revision의 기록은
  `revision_info`(조정 기준·활성·대체 시각), 지금 상태는 `current_meta` — 과거 판단에 현재 메타를 쓰지 않음.
- **기존 DB 이전 (열 때 자동, 한 번만)**: r1 → r2 → r3 → r5 (r4 DB도 r5로). r1에서 옮겨 온 판은 **r1이 저장 때마다 남긴 변경 기록**으로
  시각을 복원(2차 재검토 #1): 판 활성 시각 = 그 판을 만든 INIT/REBASE 기록 시각(전체 수집 후 저장한 시각 — 첫 페이지
  수신 시각이 아님), 대체 시각 = 다음 판 활성 시각, 봉 available_at = max(판 활성 시각, 수신 뒤 첫 저장 기록 시각).
  활성 시각이 증가하지 않아 구간이 겹치거나 기록이 없어 입증할 수 없는 판·봉은 `time_basis=UNPROVEN` —
  시점 조회에서 돌려주지 않고 `time_proof=UNPROVEN`으로 보류 표시. **이미 r2로 이전된 DB도 열 때 같은 규칙으로 보정**하고
  정합성 이력도 VERIFY_FAILED 기록에서 다시 만듦. 열린 백필 작업은 그대로 이어서 진행. 240만 봉 기준 약 7초.
- **r3 → r4 (2026-10-02, 사용자 실측 UNPROVEN 199봉)**: 저장 기록 시각은 초 내림·봉 수신 시각은 초 올림으로 남아, 한 페이지로
  끝나는 짧은 시계열(최근 상장)의 새 봉은 수신과 저장이 같은 초라 저장 기록이 수신보다 1초 앞서 보였음 → r3 복원이 UNPROVEN으로 잘못
  남김. r4는 '수신 시각 − 1초 이후의 첫 저장 기록'을 그 봉의 저장으로 보고 사용 가능 시각 = max(판 활성, 수신, 저장 기록)
  (수신 시각을 포함하므로 실제 저장보다 이르지 않음). 이미 r3인 DB는 열 때 MIGRATED·UNPROVEN 봉만 다시 계산
  (r3 뒤 새로 저장된 OBSERVED 봉은 그대로).
  * 저장 근거는 **같은 revision의 저장 기록**만 인정. 근거 없는 봉은 UNPROVEN 유지(일괄 해제 안 함).
  * 스키마를 올리기 전에 같은 폴더에 자동 백업(`research.sqlite3.bak-<옛 버전>-<시각>`, SQLite 백업 API). 확인 뒤 지워도 됨.
  * 보정 전에 `inspect-unproven`(읽기 전용, 이전·백업 없음)으로 UNPROVEN 봉을 종목·날짜·저장 기록과 대조할 수 있음
    (PROVABLE_SAME_SECOND·PROVABLE은 보정 대상, REVISION_UNPROVEN·NO_EVIDENCE는 계속 보류).
- **r3·r4 → r5 (2026-10-02, GPT 재검토 `c308ccb` B1) — 저장 기록의 판 귀속**: r4는 revision이 적히지 않은 APPEND 기록
  (r1~r4의 매일 갱신)을 판 확인 없이 **모든 판의 근거**로 합쳐, 끝난 판의 봉을 다음 판의 APPEND로 입증할 수 있었음
  (r3는 다른 판의 REBASE 기록까지 근거로 씀). r5 규칙:
  * revision이 적힌 기록(INIT·REBASE·EXTEND, 새 APPEND)은 그 판. **새 APPEND 기록에는 revision을 적음**.
  * revision 없는 예전 기록은 **기록 순서(event_id)상 바로 앞 INIT·REBASE의 판**으로 보되, 그 판의 활성 구간
    [활성 시각, 다음 판 활성 시각) 안일 때만 인정. 앞 활성 기록이 없거나, revision 없는 활성 기록 뒤이거나, 구간 밖·다음 판
    활성과 같은 초(경계)면 **어느 판인지 모호 → 근거로 쓰지 않음**.
  * 봉 판정은 `plan_migrated_bar` 하나 — `inspect-unproven`과 실제 보정이 같은 함수를 씀.
  * 이미 r4로 보정된 DB도 열 때 MIGRATED·UNPROVEN 봉을 다시 계산 — 잘못 입증된 봉은 UNPROVEN으로. 바뀐 봉 수는
    출력(`[시각 재점검 r5] {...}`)과 meta `recheck_r5`에 남김. UNPROVEN 봉의 available_at은 기존 값 유지(시점 조회에 쓰지 않음).
  * `inspect-unproven`은 이제 이미 MIGRATED인 봉도 같은 규칙으로 다시 계산해 바뀔 것을 미리 보여 줌(`recheck_migrated`:
    MIGRATED→UNPROVEN·사용 가능 시각 변경), 판을 정할 수 없는 예전 기록 수(`unattributed_writes`), 관찰 기록 DB의
    final 보정 대상(`scan_db`)도 함께.

### 응답·연속조회 계약 (A2-R3)
- return_code가 **있고 0**이어야 성공(누락을 성공으로 보지 않음). cont-yn 헤더는 Y/N, Y이면 next-key 필수.
- 페이지 안 날짜 엄격한 내림차순, 다음 페이지는 이전 페이지보다 과거, base_dt 뒤 날짜 없음, 다음 키 반복 없음,
  첫 응답·이어지는 페이지가 비어 있지 않음. 어기면 오류 — 백필 항목 ERROR(재시도 대상), 시계열·스냅숏을 만들거나 바꾸지 않음.
  정상적인 cont-yn=N 종료만 SOURCE_END(→ LISTED_AFTER_START / HISTORY_END).
- r1 시절 받은 HISTORY_END·PAGE_CAP 시계열은 `backfill --recheck-shortfall`로 다시 받아 검증할 수 있음.

### A4로 넘길 규칙 (GPT A2 검토 정책 항목)
- 18:10(정규장 종료+160분)은 확인된 완성 시각이 아닌 잠정 수집 기준 — 장 마감 후 여러 시각·다음 거래일에 같은 날짜
  OHLCV·거래대금을 대조해 TR이 포함하는 장 구간을 확인.
- 호출 성공과 최신 날짜 확보는 별개: update가 UNCHANGED/NO_DATA여도 최신 완료 세션이 없을 수 있음. A4는 기대 세션·
  last_date·available_at·integrity를 검사해 이전 신호를 오늘 신호처럼 재사용하지 않음.

### 실행 (PowerShell, 레포 루트 — 저장: data/research/research.sqlite3, git 제외)
1. `python tools/research_collect.py universe` — 목록 스냅숏.
2. `python tools/research_collect.py backfill --limit 20` 시험 → `python tools/research_collect.py backfill` 이어서 전부.
   작업이 끝난 뒤 `backfill`을 다시 실행하면 아무것도 하지 않음 — 전체를 새로 받을 때만 `--new`.
3. 매일 18:10 이후 `python tools/research_collect.py update` (목록 스냅숏 + 새 봉). 열린 백필 작업의 남은 종목은 건너뜀.
4. `python tools/research_collect.py holidays` — 2017~2025 휴장일 후보 초안(reports/research/) → 사람이 확인해
   `config/krx_calendar.yaml`에 추가.
5. `python tools/research_collect.py status` — 스냅숏·작업·커버리지·거래 없는 봉·재수집 횟수·integrity·스키마.
6. (선택) `python tools/research_collect.py backfill --recheck-shortfall` — HISTORY_END·PAGE_CAP 시계열만 다시 받아 검증.

## A4-A 앞으로의 S1 신호 관찰 (`infra/research/s1_scanner.py`·`scan_store.py`·`scan_report.py`) — 2026-10-02

### 실행
- `update`가 끝나면(종료 코드와 무관하게) 바로 스캔·보고서. `update --no-scan`으로 끌 수 있음.
  스캔만: `scan` (지금 시각), 재현: `scan --at 2026-10-02T19:30:00 --verify`.
- 관찰 기록 `data/research/s1_scans.sqlite3`, 보고서 `reports/research/s1/s1_scan_<신호일>_<실행ID>.md·json` (git 제외).
- S1_BASE(s1_pullback_v0.1) 조건·기준값 그대로. 주문 경로와 연결하지 않음(import 경계 테스트).

### 스캔 시각의 입력만
- 신호일 t = 스캔 시각에 완성된 가장 최근 거래일(정규장 종료 + 160분 — **잠정** 기준, 보고서에도 표시).
  달력이 다루지 않는 해면 스캔 불가.
- 종목·지수 = `research_series(sid, as_of=scan_at)`. 다음이면 그 종목은 **데이터 보류**(UNKNOWN, 평가 안 함):
  시계열 없음(NO_SERIES) · revision 없음 · integrity ≠ OK · time_proof ≠ OK(UNPROVEN) · t의 봉 없음(STALE).
- 지수가 위 조건에 걸리면 **지수 보류** — 그 지수를 쓰는 종목은 지수 없이 평가돼 RS·시장 판정 UNKNOWN(후보 아님).
- 종목 목록 = 스캔 시각까지 **수집이 끝난** 스냅숏(`snapshot_as_of`, latest_snapshot 아님). 그 스냅숏이 t 장 마감 전
  관측이면 현재 위험 상태를 모르는 것으로 보고 RISK_STATUS UNKNOWN(스냅숏 보류). 스냅숏의 원래 필드를 **현재 정책(u2)으로
  다시 분류**(저장 정책·적용 정책 둘 다 기록). 스냅숏이 없으면 스캔 실패(FAILED).
- 세션 목록은 거래소 달력에서만(최근 300세션). 과거 연도 달력이 없으면 긴 창은 INSUFFICIENT_SESSIONS(보수적).

### 저장 (스키마 s4)
| 표 | 내용 |
|---|---|
| `scan_run` | 실행. run_key = 신호일·스캔 시각·전략·설정 해시·분류 정책·**계산 계약 해시** — COMPLETE는 하나뿐(유일 인덱스). RUNNING → COMPLETE / FAILED / ABORTED. 실행 공통 증거(지수 revision·판정, 스냅숏, 세션, 버전)는 context |
| `s1_eval` | 실행마다 종목별 판정 전부(PASS·FAIL·UNKNOWN, 조건별 값·사유, 참고 손절가·진입 상한, 관찰값). append-only. 결과·증거(revision·조정 기준일·지수 revision·스냅숏 ID·위험 표시)는 사전 압축 JSON, 입력 해시는 열 |
| `s1_observation` | 신호 ID = `S1|전략|c:<계산 계약>|종목|신호일`(**입력 해시 없음**)마다 대표 판정 — 계약이 다른 판정은 섞이지 않음. final(데이터·지수·스냅숏 모두 정상이고 **판정이 PASS/FAIL로 정해진** 경우)은 절대 안 바뀜 — 이후 정정·새 스냅숏에도 유지. final이 아닌 기록(데이터 보류·UNKNOWN — 거래 없는 봉·이력 부족 등)은 더 늦은 스캔 시각의 실행이 대체(이력 보존). 과거 시각 재현 실행은 더 늦은 기록을 되돌리지 않음 |
| `obs_audit` | 대표 기록을 실행이 아닌 이유(규칙 변경)로 고친 이력 — 바꾸기 전·후 값과 사유 (s2) |

- final 규칙(`scan_store.final_rule`, 실행 context `final_rule="inputs_ok+decided"`): 종목·지수·스냅숏 입력이 모두 정상이고
  판정이 PASS/FAIL. 스캐너·관찰 저장소 이전·재현 검증이 같은 함수를 씀.
- **s1 → s2 (GPT B2, 열 때 자동·한 번만, 바꾸기 전 백업 `s1_scans.sqlite3.bak-s1-<시각>`)**: 이전 버전(`f837185`)은 입력만
  정상이면 UNKNOWN 판정도 final=1로 고정 → 이후 정상 판정으로 대체되지 않았음. 대표 기록 중 현재 규칙을 만족하지 않는
  final=1을 final=0으로 풀고 `obs_audit`에 남김(PASS·FAIL 확정 기록은 그대로). 실행·종목별 판정(`scan_run`·`s1_eval`)은 당시
  그대로 보존. `--verify`는 저장된 행에 현재 규칙을 적용한 final과 비교하고, 이전 규칙 차이는 `final_rule_changed`로 따로 셈.
- **계산 계약 (s3, GPT 재검토 `016907a`)**: 같은 DB·같은 스캔 시각이라도 결과를 바꿀 수 있는 계산 조건 전부 —
  전략·설정 해시·분류 정책·지표/시장 계산 버전·lookback(세션 수)·완성 봉 지연(초)·거래일 달력 내용(`calendar_version`:
  다루는 해·휴장일·세션 시각의 해시, 휴장일 이름·주석은 무관)·final 규칙·스캔 입력 규칙 버전(`SCAN_RULES_VERSION`)·지수 ID.
  해시(12자리)를 실행 키·context(`contract_hash`·`contract`)·실행 행·대표 기록 키에 넣음 → 같은 계약 재실행은 건너뛰고,
  계약이 다르면 별도 실행·별도 대표 기록. 입력 데이터 해시는 계약이 아니라 종목별 증거(`input_hash`)로 그대로.
  `--verify`는 계약이 다른 실행이면 비교하지 않고 `contract_match=False`·다른 항목(`contract_diff`)만 표시.
  보고서 머리말에 계약 해시·lookback·달력·규칙 표시. 스캔 입력 규칙(보류 조건·세션 창·입력 해시 구성)을 고치면
  `SCAN_RULES_VERSION`을 올려야 함.
- **s3 → s4 (A5-R1, 열 때 자동·백업)**: `scan_run.committed_at` — 커밋이 끝난 뒤 잰 시각(초 올림). 기존 실행은 NULL.
- **s2 → s3 (열 때 자동·한 번만, 바꾸기 전 백업 `s1_scans.sqlite3.bak-s2-<시각>`)**: `scan_run`·`s1_observation`에
  contract_hash 열 추가. 기존 실행·대표 기록은 NULL — 어떤 lookback·달력으로 계산했는지 저장돼 있지 않아 **추정하지 않고**,
  계약이 있는 기록과 다른 묶음으로 그대로 보존(덮어쓰지 않음). 대표 기록을 볼 때는 `observations(contract_hash=…)`로 한 계약만.
- actionable = 스캔 시각 < 다음 거래일 개장 — 개장 뒤에 늦게 해소된 기록은 0(A5에서 진입 가정에 쓰지 않음).
- 같은 run_key 재실행 → 건너뜀(중복 저장 없음). `--verify`는 다시 계산해 종목별 판정·입력 해시·결과 비교만.
  건너뛸 때 보고서(md·json)가 없으면 저장된 실행(context·집계·판정)으로 **보고서만 다시 만듦**(재계산 없음).
- 실행 ID = 신호일·스캔 시각·run_key 해시·시도 번호 — run_key만 다른 실행(분류 정책 등)도 ID가 겹치지 않음.
- 원자성: 판정·대표 기록·COMPLETE 표시를 한 트랜잭션. 도중 중단 → 아무것도 안 남고 실행은 ABORTED(강제 종료면
  RUNNING으로 남았다가 다음 실행이 ABORTED로 정리). ABORTED로 정리된 실행은 늦게 끝나도 완료 표시 안 됨.
- 크기: 2,500종목 한 번에 약 3MB(하루 1회면 1년 약 0.75GB). 스캔 약 7~8초.

### 보고서
- 대상·신호(PASS)·서로 다른 종목·FAIL·UNKNOWN·확정 수, 데이터 미확보 수, 거래 없는 봉 보류 수, 대표 기록 변화.
- 지수별 데이터 상태·시장 판정, 시장별(KOSPI·KOSDAQ) PASS/FAIL/UNKNOWN, 보류 사유(데이터·지수·스냅숏·조건),
  탈락 사유(묶음·조건별), 후보 표(참고 손절가·진입 상한·위험 비율·눌림 봉 수·RS60·20일 거래대금·개장 전 스캔 여부).
- "신호는 관찰 후보이며 수익이 아님" 고지. 체결 검증·이후 움직임은 A5.

### 다음
- A4-B: 과거 일괄 스캔 — 과거 달력 확인 뒤, 백필은 생존 편향 있는 탐색용 결과로 분리, 현재 위험 상태를 과거에 적용하지 않음.
- A5: 다음 거래일 09:05 가격 확인(A5-1, 아래), 5·10·20거래일 뒤 움직임 평가(A5-2, actionable 기록만 진입 가정).

## A5-1 다음 거래일 개장 가격 기록 (`infra/research/open_check.py`) — 2026-10-02

GPT 재검토 `71b78e3` 지시. 주문 없음·조회만(모의 도메인, 시세 TR ka10001 하나 추가). 관찰 가격 기록이며 체결이 아님.

### 실행
- `python tools/research_collect.py open-check` — 오늘(거래일)의 목표 시각(개장 + 5분, 보통 09:05)에 후보 가격을 기록.
  목표 30분 전 안이면 기다렸다가 실행, 더 멀거나 `--no-wait`면 기록 없이 끝냄. 휴장일은 다음 거래일 안내.
  지난 날짜는 `--day 2026-10-06` — 정규장이 끝났으면 조회 없이 누락(MISSED)으로 기록.
- 대상 계약: `config/research.yaml`의 `s1.active_contract` — `current`(지금 코드·기본 설정 계약, update·scan과 같은 완성
  지연) 또는 12자리 해시. 모르는 키·잘못된 값은 오류(fail-closed). 확인 시각 `a5.open_check.offset_min`(기본 5),
  ON_TIME 허용 `on_time_tolerance_sec`(기본 120).
- 저장 `data/research/a5_checks.sqlite3`(수집·관찰 DB와 별도), 보고서 `reports/research/a5/a5_open_<대상일>.md·json` (git 제외).
- 원천 확인: `python tools/probe_price_sources.py`(장중) — ka10001 필드와 시각 필드 후보(ka10003 체결·ka10004 호가)를 기록.
  **10/2 11:40 실측**: ka10001에는 가격 시각 필드 없음(cur_prc·base_pric·open/high/low·upl/lst_pric·trde_qty 확인, 부호는
  전일 대비 방향, base_pric = 전일 종가로 pred_pre = 현재가 − 기준가). ka10003 `cntr_infr` 첫 행이 최근 체결(tm HHMMSS·cur_prc·
  stex_tp=KRX, 요청보다 약 1초 앞), ka10004 `bid_req_base_tm`·`sel_fpr_bid`·`buy_fpr_bid`. 세 TR 모두 return_code 0.

### 후보 확정 (`candidate_set`·`candidate`, 대상 거래일 D마다 한 번)
- 신호일 t = D의 직전 거래일. 대상 계약의 t 대표 기록 중 **PASS·final=1·actionable=1·스캔 시각 < D 개장**만.
  다른 계약·계약 기록 전(NULL)·개장 뒤 확정된 기록은 후보가 아님 → 종목당 하나.
- **개장 전 실제 저장 완료(GPT 재검토 `14eb8c0` A5-R1)**: 대표 기록의 실행이 COMPLETE이고 저장 완료 상한 < D 개장이어야 함.
  저장 완료 상한 = 관찰 저장소 `committed_at`(커밋이 끝난 뒤 잰 시각을 초 올림, s4). 그 값이 없는 실행(s4 이전·커밋 시각
  기록 전에 끝난 실행)은 저장 완료를 입증할 수 없어 제외 — finished_at에 여유를 더해 추정하지 않음. 개장 뒤 과거 시각(scan_at)으로 계산·저장한 PASS는
  actionable=1이어도 제외. 제외한 실행과 근거는 후보 목록 원천(`source.excluded_runs`·`commit_basis`)에 기록.
- 첫 확인 실행(목표 시각 이후)이 signal_id·run_id·contract_hash·입력 해시·진입 상한·참고 손절가·신호일 종가(그 스캔 시각의
  값)·revision을 저장해 확정 — 이후 실행은 다시 고르지 않고 이 목록을 씀(관찰 기록이 나중에 바뀌어도).
- D마다 목록 하나: 대상 계약을 바꿔도 이미 확정된 D는 그 계약 그대로(CONTRACT_CHANGED 표시), 새 계약은 다음 D부터.
- 개장 전 대상 계약 스캔이 없으면 NO_SCAN, 스캔은 있었는데 후보가 없으면 NO_CANDIDATES로 확정(조회 0회).

### 가격 확인 (`price_check`, 후보·확인 종류(`OPEN+5m`)마다 한 행)
- 목표 시각 = 달력의 D 개장 시각 + offset(특수 개장일 1/2 10:00 개장이면 10:05). 목표 전에는 실행 거부(아무것도 기록 안 함).
- 요청·수신 시각·시도 횟수·지연(초)·응답 소요(ms) 기록. 요청·수신 모두 목표 + 허용 안이면 ON_TIME, 요청은 안인데 수신이
  넘으면 LATE_RESPONSE, 요청이 넘으면 LATE — 실제 조회 시각 그대로(09:05 가격으로 간주하지 않음). 일봉으로 채우지 않음.
- 마감(A5-R2): D 정규장 종료를 조회 함수에 넘겨, 인증·호출 간격·재시도 대기 뒤 **실제 요청 직전마다** 검사 — 지났으면
  요청하지 않음(첫 요청 전이면 MISSED, 재시도 중이면 FETCH_FAILED·MISSED). 마감 전에 요청했지만 마감 뒤에 받은 응답은
  AFTER_CLOSE — 원문·실제 시각만 보존, 관찰가·판정·가정 체결가격으로 쓰지 않음. 실행 시작이 정규장 뒤면 조회 없이 MISSED.
- 조회(판정 기준): ka10001 cur_prc(현재가)·base_pric(기준가) 필수, 시가·고가·저가·상한가·하한가·거래량은 있으면 기록,
  응답 본문 보존(토큰처럼 보이는 키는 가림).
- 보조 조회(기록용, 스키마 a3): ka10001 행을 **먼저 저장**한 뒤 ka10003 → 그 조회의 최근 KRX 체결 시각(`trade_time`)·체결가
  (`trade_price`)·체결 조회 요청 대비 지연(`trade_lag_sec`), ka10004 → 호가 기준 시각(`quote_time`)·최우선 매도/매수호가
  (`best_ask`·`best_bid`). 체결 시각은 **ka10001 가격의 원천 시각이 아님**(별도 조회) — ka10001 `source_time`은 원천에 시각이
  없어 비워 둠. 판정·가정 체결가격에는 쓰지 않음 — 실패하거나 형식이 틀려도 ka10001 판정은 그대로, 상태는 `extra_json`.
  보조 조회 중 중단되면 기본 가격 행은 남고(`extra_json` = 대기), 재시작 때 그 종목은 다시 조회하지 않고 보조 조회만 INTERRUPTED로 마감.
  ka10001 조회 실패·필드 없음이면 보조 조회도 하지 않음. 후보 하나에 조회 3회(1초 간격) — 후보 40개 안팎까지 ON_TIME.
- 체결 값의 근거 `trade_basis`: KA10003(이 코드가 ka10003에서 직접) / LEGACY_KA10003(a2에서 근거 확인 후 옮김) /
  UNKNOWN(값은 있으나 근거 불명 — 보고서에 `UNKNOWN(근거 불명)`, 판정·가정 체결에 쓰지 않음) / 비어 있음(체결 값 없음).
- a1·a2 → a3(열 때 자동·백업 `a5_checks.sqlite3.bak-<옛 버전>-<시각>`, 한 트랜잭션 — GPT 재검토 `0e494dd` P1):
  - 열 추가: `trade_time`·`trade_price`·`trade_exchange`·`trade_lag_sec`·`trade_basis`·`legacy_json`(+ a1이면 보조 조회 열 전부).
  - `53e56e5`(a2)는 ka10003 최근 체결을 `source_time`·`source_price`·`source_exchange`·`source_lag_sec`에 저장했음 →
    같은 행 `extra_json`의 ka10003 OK 응답 첫 행(tm·cur_prc)과 **일치할 때만** trade_*로 옮기고 LEGACY_KA10003.
    일치하지 않거나 대조할 응답이 없으면 trade_*는 비우고 UNKNOWN — 원래 값은 `legacy_json`과 옛 열에 그대로 보존.
  - 어느 경우든 `source_time`은 비움(이제 ka10001 원천 시각 자리 — 원천에 시각이 없어 항상 비어 있음).
  - a2 표시인데 이미 trade_* 열인 DB(`0e494dd` 코드가 새로 만든 경우)는 같은 근거 검사로 KA10003/UNKNOWN 표시만.
  - 후보·기본 가격·판정·가정 체결·요청 시각 등 다른 열은 바꾸지 않음. 결과 집계는 `meta.a3_upgrade`, CLI가 출력.
- 결과(fetch_status): OK / FETCH_FAILED(재시도 후 실패) / PARSE_FAILED(필수 필드 없음) / NOT_RUN(누락) — 그 확인의 결과로 남김.
- 판정(outcome, 우선순위): NOT_TRADABLE(현재가 0·거래량 0(정지 가능)·상한가) > BASIS_CHANGED(D 기준가 ≠ 신호일 종가 —
  액면분할·권리락 등으로 가격 기준이 달라져 진입 상한 비교 보류) > ABOVE_CAP(관찰가 > 진입 상한) > BELOW_STOP(관찰가 ≤ 참고
  손절가) > WITHIN_CAP. 신호일 종가·기준가·진입 상한 대비 괴리를 함께 기록.
- 가정 체결가격은 WITHIN_CAP일 때만 관찰가 + 규칙 이름 `OBSERVED_PRICE_AT_CHECK(가정 — 실제 체결 아님)`. 체결(FILLED) 표시 없음.
- 재시작: 이미 기록된 후보는 다시 조회·저장하지 않음(행마다 한 트랜잭션 — 중단돼도 남은 후보만 이어서, 늦으면 LATE).
  실행 기록(`check_run`)은 RUNNING → COMPLETE / FAILED, 남아 있던 RUNNING은 다음 실행이 ABORTED로 정리.
  실행 ID = 대상일·확인 종류·DB가 같은 트랜잭션에서 발급하는 시도 번호(A5-R3, `a5_20261006_open5m_a2`) — 같은 초에 다시
  실행해도 충돌 없이 기존 결과로 보고서만 다시 만듦(추가 조회 없음).

### 다음
- A24-A: 조회 전용 상시 실행 관리자 — 마감 갱신·스캔·아침 가격 기록 자동 실행, 재시작 후 이어감, 놓친 확인은 누락으로.
- A5-2: 5·10·20거래일 움직임(미완료 표본·데이터 미확보 구분). 18:10 완성 기준 실측.
