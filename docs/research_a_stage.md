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
| `series.py` | `ResearchBar`(유한한 양수 가격·bool 아닌 정수 수량·실제 거래대금 원/None, 거래량 0 = `no_trades`), `SeriesView`(t 이후 차단, 세션 목록 오름차순·중복 검증, 세션 기준 창, INSUFFICIENT_HISTORY / DATA_GAP / **NO_TRADES** 구분) | — |
| `features.py` | SMA·기울기·ret·RS·ATR14(단순평균)·extension·close_location·volume_ratio·거래대금 평균·tr_contraction·volume_dryup(t 제외)·high/low252·return_atr·narrow_range7·EMA(6N). f2: 거래 없는 봉 정책 | f2 |
| `market.py` | UNKNOWN → RISK_OFF → RISK_ON → MIXED 우선순위 | m1 |
| `universe.py` | 종목 목록 분류(증권 유형·현재 위험 표시 합집합·수집 대상/현재 자격 분리), 정책 버전·해시 | u1 |
| `holiday_candidates.py` | 지수 날짜 → 과거 휴장일 후보·추정 이름(사람 확인용 초안) | — |
| `weekly.py` | 완성 주봉(주 단위 예정 일정, 가정/관측 모드별 사용 가능 시각 — 관측은 주 전체 봉 확보 시각, 불완전·일정 불명·준비 안 된 주 자리 유지, 전체 휴장 주 간격), SMA30W·slope4W(34주 연속성 검사), UP/DOWN_PROXY, 비유한값 UNKNOWN | w3 |
| `s1.py` | pattern / eligibility / market / **stop_valid** / eligible_signal(네 묶음 모두 PASS) — 다음날 확인 후보는 eligible_signal만 사용. 종목·지수·시장 판정 **기준일 일치 필수**(불일치 AS_OF_MISMATCH, 수익률 구간 날짜 불일치 SESSION_ALIGNMENT_MISMATCH → UNKNOWN). 시장 조건은 기본적으로 같은 지수 View에서 계산, 외부 판정은 원천 식별자 일치 필수(INDEX_SOURCE_MISMATCH / INDEX_NOT_STOCK_MARKET). 조건별 값·사유, 참고 손절가·진입 상한·위험 비율, 관찰값, 결정적 후보 정렬, 설정 검증·해시 | s1_pullback_v0.1 |

## A2 수집 (`infra/research/`, `tools/research_collect.py`) — 2026-09-30

### 결정 (사용자·GPT 합의)
| 항목 | 결정 |
|---|---|
| 지수 가격 | ka20006 OHLC 모두 **÷100** (공식 명세: 소수점 뺀 100배 값). KOSPI 실제 값 독립 확인은 별도 |
| 투자주의·투자주의환기종목 | 초기 S1 후보에서 제외(auditInfo≠정상이면 위험). 원래 값·제외 사유 보존 |
| 외국기업 | 초기 S1·수집 대상 제외(유형 FOREIGN). 분류 정책 u1(버전·해시)로 기록 |

### 원천·단위
- ka10099 종목 목록(mrkt_tp 0/10, 한 페이지에 전체), ka10081 종목 일봉(upd_stkpc_tp=1), ka20006 지수 일봉. 이 셋만 허용.
- 거래대금(trde_prica)은 **백만원** → 원 환산(×1,000,000). 정밀도는 백만원 단위, 원천 반올림 방식은 미확인(오차 범위 단정 안 함).
- 지수 거래량 단위는 미확인(S1은 안 씀). 저장소에는 원천 정수를 그대로 두고 배율·단위는 읽을 때 적용.
- 2026-09-30 12:31 스냅숏 기준: 주식 2,740 → 우선주 추정 114·스팩 67·외국기업 15 제외 → **수집 대상 2,544**,
  그중 현재 위험 표시 257, 현재 자격 2,287(유동성·이력·패턴 검사 전). state에만 관리종목인 행은 전체 목록 84 / 주식 82.

### 수집 대상과 신호 자격 분리 (보완 2)
- 수집 대상 = 보통주 전체(**현재 위험 표시 종목 포함**) + 지수 2개. 현재 상태로 과거 표본을 고르지 않음.
- 현재 자격(eligible_now)은 그 스냅숏을 관측한 시점에만 유효. 백필 날짜의 위험 상태는 UNKNOWN.
- 위험 표시 = auditInfo≠정상 ∪ state 토큰(관리종목·거래정지) ∪ orderWarning≠0. **orderWarning은 원래 숫자로만 기록**
  (ORDER_WARNING:5 등) — 관리·정지 등으로 번역하지 않음(보완 3). 필드가 없으면 *_MISSING(위험으로 봄).
- 백필 작업에 선정 기준·생존 편향(현재 상장 종목만) 문구를 남김.

### 거래 없는 봉 (보완 1)
- 거래량 0인 봉은 원본대로 저장하고 quality=NO_TRADES로 표시(예: 삼성전자 2018-04-30·05-02·05-03, OHLC 53,000).
  거래정지로 단정하지 않음.
- 계산 정책(feature f2): 기준일이면 NO_TRADES_AT_T(그날 신호·체결 가정 없음), 창 안에 있으면 UNKNOWN(NO_TRADES:<날짜>),
  EMA 연속 구간도 끊김. → S1은 최근 160세션 안에 거래 없는 봉이 있으면 HISTORY UNKNOWN. 완화는 버전을 올려 별도로.
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
  값 변경·날짜 소실·새 날짜 출현이 있으면 새 기준으로 전체를 다시 받아 **통째로 교체**(revision+1), 이전 값은 bar_history에 보존.
  한 시계열 안에 서로 다른 조정 기준이 섞이지 않음.
- base_dt 고정은 재현성을 위한 기록입니다. 원천이 base_dt와 무관하게 최신 조정을 적용하더라도 일관성은
  "한 종목 = 한 번의 연속 조회" + "매일 겹침 구간 전체 비교"로 보장됩니다(어긋나면 재수집·교체).
- **수정가격 ≠ 과거 실제 체결가격.** 분할·증자 등으로 과거 가격이 다시 계산된 값이므로, 과거 호가·체결 가능 여부·
  금액 기준(최소 주문금액 등)을 수정가격으로 판단하면 안 됨. 거래대금(원)은 조정되지 않은 실제 금액.
  앞으로 A4가 스캔 당시 관측한 가격을 따로 남기면 나중에 조정 비율로 대조.

### 확보 시각(ready_at)과 run_type
- BACKFILL 봉: ready_at 없음 → 주봉 ASSUMED_DELAY. FORWARD 봉: ready_at = 완성 봉으로 처음 들어온 응답 수신 시각.
- 재수집(REBASE) 뒤에도 그 날짜의 run_type·ready_at은 유지(값만 새 기준). 매일 갱신 구간에서 누락됐다가 나중에 나타난
  봉은 FORWARD·ready_at = 복구 조회 시각 → 그 주 주봉은 복구 뒤에야 사용 가능.

### 실행 (PowerShell, 레포 루트 — 저장: data/research/research.sqlite3, git 제외)
1. `python tools/research_collect.py universe` — 목록 스냅숏.
2. `python tools/research_collect.py backfill --limit 20` 시험 → `python tools/research_collect.py backfill` 이어서 전부.
3. 매일 18:10 이후 `python tools/research_collect.py update` (목록 스냅숏 + 새 봉). 열린 백필 작업의 남은 종목은 건너뜀.
4. `python tools/research_collect.py holidays` — 2017~2025 휴장일 후보 초안(reports/research/) → 사람이 확인해
   `config/krx_calendar.yaml`에 추가.
5. `python tools/research_collect.py status` — 스냅숏·작업·커버리지·거래 없는 봉·재수집 횟수.
