# 사용자 지정 종목 설정·검증·데이터 준비 (방향 전환 2단계, 2026-10-07)

최종 목표는 사용자가 지정한 국내 종목의 24시간 운영·자동매매입니다. 이 단계는 **대상 선택(수동)과 데이터 준비**까지이며
주문은 없습니다. 이후 단계(상시 실행 관리자 → 장중 감시·알림 → 다일 장애 검증 → 전략·모의 주문)가 같은 설정과
적용 버전을 그대로 씁니다. 해외(미국)는 후속 단계로 미룸.

## 책임 분리
| 층 | 파일 | 하는 일 |
|---|---|---|
| 대상 선택 | `config/watchlist.yaml` (git 제외), `config/watchlist.example.yaml` | 사용자가 종목·관심 조건·수동 보유 정보를 정함 |
| 해석·검증 | `domain/watchlist/config.py` | 순수 함수. 형식·범위·종목 목록 대조, 종목·필드별 오류/경고 |
| 적용·준비 | `infra/watch/manager.py`, `infra/watch/store.py` | 적용 이력, 마지막 정상 설정 유지, 등록 종목만 수집, 준비 상태 |
| 명령 | `tools/watchlist.py` | init·add·set·enable·disable·holding-close·remove·validate·apply·prepare·status·history |
| 전체 시장 연구 | `tools/research_collect.py` (그대로) | 전체 종목 수집·S1 스캔·A5 — 별도 명령으로 보존 |

브로커·주문 실행부·원장·전략·알림을 import하지 않습니다(테스트 5-1). 조회 TR은 연구 클라이언트와 같은
ka10099(목록)·ka10081(종목 일봉)·ka20006(지수 일봉)뿐, 모의 도메인.

## 설정 (스키마 w1)
```yaml
schema: w1
monitor: {interval_sec: 60, history_sessions: 160}
alerts: {repeat_limit_per_day: 3, min_repeat_interval_min: 30, rearm_on_clear: true}
symbols:
  - code: "005930"            # 따옴표 필수 — 숫자로 읽히면(앞자리 0 손실) 오류
    interest: {enabled: true, s1_analysis: true, price_bands: [{low: 60000, high: 62000, label: 눌림}]}
    holding: {quantity: 10, avg_price: 61000, stop_price: 57000, target_price: 70000}   # 수동 입력
```
- 종목 하나가 관심과 수동 보유를 함께 가질 수 있음(같은 가격 조회 공유). 같은 코드를 두 번 쓰면 오류.
- **holding = 수동 보유 정보**: 증권사 잔고와 같다고 보지 않고 화면에도 "수동 보유(증권사 잔고 아님)"로 표시.
  보유가 있는 동안 보유 감시는 끌 수 없음 — `disable`은 관심만 끔, `disable --holding`·보유 있는 항목 `remove`는 거부.
  보유가 끝나면 `holding-close`.
- 종목 목록(연구 DB의 최신 ka10099 스냅숏) 대조: 관심 종목이 목록에 없으면 오류, 보유만 있으면 경고(보유 감시 유지·
  데이터 UNKNOWN), 보통주가 아닌 종목의 S1 분석은 오류, 위험 표시·이름 불일치는 경고(감시 유지, 신규 진입 관찰 제외).
  스냅숏은 KOSPI·KOSDAQ 주식 행만 담음 — ETF·ETN은 지금 지원 안 함. 목록이 없으면 적용 불가(대조할 수 없음).
- `market`은 지금 KRX만. 종목 키는 `KRX:<코드>`(해외 추가 때 충돌 방지 자리).

## 적용과 오류 (GPT 조건)
| 상황 | 동작 |
|---|---|
| 정상 | `config_version`에 APPLIED — 새 버전이 사용 중 설정 |
| 최초 설정이 틀림(정상 설정 없음) | REJECTED, **감시 시작 안 함**(`prepare` 종료 코드 2, 조회 0) |
| 운영 중 잘못 고침·파일 없음·읽기 실패(UTF-8 아님·권한 등 OSError) | REJECTED, **마지막 정상 버전으로 감시 유지**(보유 감시 포함), 오류·사용 중 버전 표시, **신규 매수 차단**(`entry_blocked`) |
| 마지막 정상 설정의 수동 보유가 새 설정에서 사라짐(파일 직접 수정 포함) | `holding-close` 청산 기록(같은 보유 값)이 있을 때만 APPLIED. 없으면 REJECTED — 보유 감시 유지 (W1-R1) |
| 같은 파일을 다시 읽음 | 새 기록 없음(원문·판정·오류·경고가 같으면). 종목 목록 스냅숏만 바뀌면 `config_check`에 재검증 기록, 경고가 바뀌면 새 적용 기록 |
| CLI로 바꿈 | 바꾼 결과를 먼저 검증 — 틀리면 파일·이력 그대로(거부), 맞으면 파일을 쓰고 APPLIED(origin `CLI:<명령>`) |

청산 기록(`holding_close`): `holding-close`가 마지막 정상 보유 값으로 OPEN 기록 → 그 보유를 지운 설정이 적용되면 USED(적용
버전 연결). 보유가 그대로 남은 설정이 적용되면 VOID — 나중에 실수로 지운 설정에 쓰이지 않음.

이력(`data/watch/watch.sqlite3` `config_version`): 시도 시각·버전·APPLIED/REJECTED·원문·정규화 설정·오류·경고·대조한
목록 스냅숏 ID. 사용 중 설정은 DB에 저장된 마지막 APPLIED에서 복원(파일을 다시 믿지 않음).

## 데이터 준비 (`prepare`, `add`)
- 대상 = **등록 종목(관심 켜짐 또는 보유 있음) + KOSPI·KOSDAQ 지수**. 비활성 항목·목록의 다른 종목은 조회하지 않음.
- 연구 수집기(`update_series`)를 그대로 씀 — 연구 DB에 없는 종목은 **등록 즉시 전체 이력 수집**(`add`가 바로 실행,
  `--no-fetch`로 생략), 있는 종목은 새 완성 봉만 추가(다시 받지 않음). 열린 연구 백필 작업이 맡은 시계열은 건너뜀.
- 그날 첫 `prepare`는 종목 목록(ka10099 2회)만 새로 받아 설정을 다시 대조(`--no-list-refresh`로 생략). 전체 시장 일봉은 받지 않음.
- 준비 상태는 둘로 나눔 (W1-R3):
  * **가격 데이터**(`status`): 최근 완성 거래일(정규장 종료 + 160분 기준)까지 그 시각에 확보 시각이 입증된 일봉이 있고 정합성
    정상이면 READY, 아니면 UNKNOWN(NO_SERIES·NOT_IN_LIST·STALE·UNPROVEN·INTEGRITY:*·CALENDAR). 보유 가격 감시는 이것만 봄.
  * **S1 분석**(`analysis_status`): S1 계산과 같은 `SeriesView.window(history_sessions)` 계약 — 기준일 봉 없음·거래 없음
    (NO_TRADES_AT_T), 창 안 거래 없는 봉(NO_TRADES)·누락(DATA_GAP), 상장 이력 부족(INSUFFICIENT_HISTORY), 달력 부족
    (INSUFFICIENT_SESSIONS)이면 HOLD. 분석이 보류돼도 보유 가격 감시는 유지.
  * 사유는 범주만, 날짜·봉 수는 detail(`window`: 예 NO_TRADES:2026-09-15) — 매일 숫자가 바뀌어도 상태 이력이 늘지 않음.
- **위험 자격**(`symbol_risk`, W1-R2): 설정을 읽을 때마다(status·apply·prepare·편집) 최신 종목 목록으로 갱신 — prepare를
  기다리지 않음. 확인한 목록 스냅숏·위험 표시를 함께 저장, 바뀌면 `symbol_risk_log`.
- **진입 관찰 가능 = 설정 정상 + 관심 켜짐 + 위험 자격 OK(그 목록 기준) + 가격 데이터 READY + S1 분석 READY**
  (`entry_gate` — 이후 전략·주문 단계가 확인). 위험 자격이 없거나(RISK_UNVERIFIED) 더 새 목록으로 확인되지 않았으면
  (RISK_STALE) 차단. 보유만 있는 종목은 진입 관찰 대상 아님(보유 감시만).
- 준비 상태·준비 실행에 **적용 설정 버전**과 실행 ID를 기록(`readiness.config_version`·`run_id`, `prepare_run.config_version`).
  상태가 바뀔 때만 `readiness_log`에 한 행. 한 종목 조회 실패는 그 종목만 UNKNOWN, 실행 PARTIAL(종료 코드 1).
- 종목을 끄거나 지워도 연구 DB의 일봉과 다른 종목 기록은 그대로(삭제·재수집 없음).

## 달력 범위
- `history_sessions` 기본 160은 S1 최소 이력(S1_BASE) 기준. 거래일 달력(`config/krx_calendar.yaml`)이 지금 2026년만 다뤄,
  연초나 더 긴 지표(예: 252일)에는 이전 연도 휴장일을 달력에 추가해야 함 — 달력 범위 때문에 필요한 이력을 줄이지 않음.

## 다음 단계에서 붙일 것
- 상시 실행 관리자: 재시작 후 이어서, 중복 실행 잠금, 긴 수집이 보유 감시를 막지 않는 구조, 기존 작업 스케줄러 정리.
- 장중 가격 감시·알림: `monitor.interval_sec`·`alerts.*` 사용, 알림 기록에 적용 버전 연결.
