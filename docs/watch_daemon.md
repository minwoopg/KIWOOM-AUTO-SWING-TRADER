# W2 조회 전용 상시 실행 관리자 — 운영 명세 (2026-10-07, W2 검토 R1~R5 반영)

지정 종목(관심 켜짐·수동 보유) + KOSPI·KOSDAQ의 일봉 준비, 지정 종목 S1 관찰, 다음 거래일 개장 확인을 거래일 달력에 맞춰
자동으로 실행합니다. **주문 없음** — 모의 도메인, 조회 TR(ka10099·ka10081·ka20006·ka10001·ka10003·ka10004)만.
전체 시장 수집·S1 스캔·A5는 `tools/research_collect.py`로 그대로(별도).

## 작업
| 작업 | 실행 가능 시각 (Asia/Seoul) | 하는 일 |
|---|---|---|
| CLOSE_PREP(D) | D 정규장 종료 + 160분(잠정 완성 기준 — 확정값 아님) 뒤, 다음 거래일 완성 전까지 | 그날 마감 뒤 목록이 없으면 목록(2회) → 설정 반영(적용 잠금) → 지정 종목·지수 일봉 준비 → 지정 종목 S1 관찰 → 보고서 |
| OPEN_CHECK(D) | D 개장 + 5분(달력의 실제 개장 — 1/2 특수 개장은 10:05) 뒤 | 직전 거래일 지정 종목 관찰의 **개장 전 저장 완료** PASS 후보 가격 기록(A5-1 규칙) |

- CLOSE_PREP 기한(다음 거래일 완성 시각)을 넘기면 MISSED — 지난 날의 신호를 나중에 만들지 않음(일봉은 다음 준비가 이어 받음).
  다음 개장 뒤에 늦게 실행된 관찰은 스캐너 규칙대로 actionable=0 — 그날 개장 후보가 아님.
- OPEN_CHECK를 놓치면(관리자가 꺼져 있었음) 늦게라도 실행해 A5 규칙대로 MISSED·LATE·AFTER_CLOSE로 기록 — 당시 가격으로 채우지 않음.
- 처음 켠 날(`since`) 이전 거래일의 작업은 만들지 않음.

## 지정 종목 S1 관찰 — 선정 방식 계약
- 대상 = 사용 중 설정에서 `interest.enabled ∧ interest.s1_analysis`인 종목(사용자가 선택). 수동 보유만 있는 종목은 관찰 대상 아님.
- 계산은 S1 스캐너 그대로, `selection = {kind: watchlist, rule, codes}`를 **계산 계약에 포함** — 전체 시장 연구 계약과 해시가
  달라 실행·대표 기록·신호 ID가 섞이지 않음. 저장도 별도 파일(`data/watch/watch_s1.sqlite3`). 설정 버전·선정 규칙은 context에 기록.
  selection이 없으면 연구 계약 해시는 이전과 같음(`170bd3f6d10b` 그대로 확인).
- 종목을 더하거나 빼면 대상 범위가 바뀌어 같은 날 CLOSE_PREP를 새 범위로 한 번 더(새 계약). 개장 확인은 그 신호일 관찰 중
  **개장 전 저장 완료**된 가장 최근 관찰의 계약을 씀 — 개장 뒤 다시 관찰한 것은 후보 원천이 아님.
- 개장 확인 기록은 별도 파일(`data/watch/watch_open.sqlite3`). 관찰 가격이며 체결이 아님.

## 계산 PASS와 운영 진입 자격은 따로 (W2 검토 R1)
| 구분 | 저장 위치 | 뜻 |
|---|---|---|
| S1 계산 결과 | `watch_s1.sqlite3`(관찰 DB — PASS·final·actionable 그대로, 원천 `final` 규칙 손대지 않음) | 그 데이터로 계산하면 신호가 나왔다(진단) |
| 운영 진입 게이트 | `daemon.sqlite3`의 `candidate_gate`(단계 SCAN·OPEN, 판정·사유·설정 버전·최근 시도 상태·목록 스냅숏·준비/위험 근거) | 지금 운영 후보로 써도 된다 |

- 게이트 = 설정 정상(최근 시도 거부·미해결 적용 저널 없음) ∧ 관심 켜짐 ∧ s1_analysis ∧ 위험 자격 OK(최신 목록 스냅숏으로 다시 확인) ∧
  가격 데이터 READY ∧ S1 분석 READY ∧ analysis_basis 일치(`watchlist.py status`의 '신규 진입 관찰'과 같은 함수 `entry_gate`).
- **SCAN 단계**: CLOSE_PREP의 관찰 직후 S1 대상 종목마다 기록. 개장 전에 설정이 바뀌면(거부→복구, 관심 해제 등) 관리자가 순회마다
  **개장 전 관찰만** 다시 판정해 결과가 바뀐 종목의 새 행을 남김(`task_key=REGATE`). 개장 뒤에는 다시 판정하지 않음(소급 없음).
- **OPEN 단계**: 개장 확인이 후보를 확정하기 직전 — 계산 PASS 후보마다 ① 개장 전 마지막 SCAN 근거가 통과했고 ② 지금(확인 시점) 상태로
  다시 통과해야 후보. 나머지는 후보 목록에서 빼고 사유(`SCAN:CONFIG_ERROR`, `SCAN:INTEREST_OFF`, `OPEN:ANALYSIS_HOLD:BASIS_CHANGED`,
  `NO_GATE_EVIDENCE` …)를 `candidate_set.source.gate.excluded`와 OPEN 근거 행에 기록. 빠진 종목은 가격을 조회하지 않음.
- 개장 확인 시점 정책: 설정 거부·미해결 저널 → 제외, 관심 해제·설정에서 빠짐(`NOT_WATCHED`)·비활성(`INACTIVE`) → 제외, 위험 자격 변경 →
  제외, 준비 기준 변경 뒤 아직 다시 준비 안 함 → `BASIS_CHANGED` 제외. 계산 PASS 기록·확정된 후보 목록은 지우지 않음.
- 이전 형식: 게이트 근거가 없는 관찰은 `NO_GATE_EVIDENCE`로 제외(유효로 추정하지 않음). 이 판 전에 게이트 없이 이미 확정된 개장 후보
  목록은 그대로 두되 `LEGACY_NO_EVIDENCE`로 표시하고 가격 조회 안 함(운영 표본으로 쓰지 않음).
- 연구 CLI(`research_collect.py open-check`)의 A5는 게이트 없이 이전과 같음(전체 시장 연구 기록).

## 작업 키·상태·재시도
- task_key = `CLOSE_PREP|D|scope:v2:<해시>` / `OPEN_CHECK|D|OPEN+5m`. CLOSE_PREP 범위(W2 검토 R4)는 결과를 바꾸는 것만 넣음:
  감시·S1 대상 코드 + **S1 계산 계약 해시**(전략·S1 설정 해시·달력 버전·완성 지연 after_close·선정 코드 등) + **분석 준비 기준**
  (analysis_basis) + 준비 이력 길이(history_sessions). 관심 가격대·수동 보유 수량/평단/손절·메모·알림 설정은 넣지 않음 — 바뀌어도 같은
  날 다시 준비하지 않음. 실행한 계산 계약·**실제로 쓴** 설정 버전(목록 갱신 뒤 다시 읽은 버전)은 행에 기록.
- 범위가 바뀌면 같은 날 새 키로 한 번 더(받은 일봉은 다시 받지 않음). 개장 전이면 새 관찰이 그날 후보 원천, 개장 뒤면 actionable=0이라
  소급하지 않음. 목록 갱신 뒤 다시 읽은 설정 범위가 키와 다르면 그 작업은 `SUPERSEDED`(새 키 작업이 맡음).
- 이전 키 이전(wd1 → wd2): 관리자 DB를 열면 표·열만 더하고(기존 행 보존, `meta.upgraded_from=wd1`) 이전 형식 키
  `scope:<10자리>`의 COMPLETE 행은 그대로. 진행 중 거래일은 새 키로 한 번 다시 준비될 수 있음(이때 게이트 근거가 생김).
- 상태: PENDING → RUNNING → COMPLETE / PARTIAL(일부 조회 실패) / YIELDED(양보·미룸) / FAILED(예외) / ABORTED(중단) / MISSED /
  SUPERSEDED.
- PARTIAL·FAILED·ABORTED: 실패 1·2·3번째 뒤 5·15·30분에 다시, **실패 4번째(총 4회 시도)면 더 예약 안 함** — status에 남김.
- YIELDED: 실패로 세지 않음. 다시 실행 가능한 시각은 양보 사유별(아래). COMPLETE는 다시 실행하지 않음 — 보고서만 `report --day`.

## 우선순위·양보·호출 예산 (W2 검토 R2·R3)
- OPEN_CHECK > CLOSE_PREP(오래된 날 먼저).
- **요청 경계 검사**: 조회 클라이언트가 실제 요청을 보내기 직전마다(토큰 발급·연속조회 페이지·429/401 재시도 포함) 관리자의 검사를
  부름. 막히면 그 요청은 보내지 않고 작업이 YIELDED. 대상 사이(`prepare_data`)도 같은 규칙.
- **사용량**: 통과한 요청은 보내기 **전에** 요청일(관리자 시계 날짜) 사용량 +1을 저장 — 강제 종료돼도 빠지지 않고, 자정에 걸친 작업은
  날짜별로 나뉨. 실패한 요청(429 등)도 셈(보수적).
- **한도**: 하루 상한 `--daily-call-cap`(기본 3,000). 마감 준비는 `cap − --open-check-reserve`(기본 100)에서 멈춤 → 개장 확인 몫을 남김.
  개장 확인도 cap은 넘지 않음 — 우선순위가 상한을 무시하지 않음.

| 양보 사유 | 다시 실행 가능한 시각 | 비고 |
|---|---|---|
| `CALL_BUDGET` | 다음 예산 창 = 요청일 다음 날 00:00 | 시작 전에 이미 소진이면 **시작하지 않고 미룸**(시도 수 그대로, 같은 미룸은 한 번만 기록) |
| `PRIORITY:OPEN_CHECK` | 지금(계획이 OPEN_CHECK를 먼저 고름) | 지금 실행 가능한 OPEN_CHECK가 있을 때만 — 실패 후 대기·예산 소진·끝남이면 양보 안 함(무한 양보 없음) |
| `TIME_BUDGET`(기본 30분) | poll_sec 뒤 | 받은 만큼 저장, 다음에 이어서 |
| `STOP_REQUESTED` | 지금 | 프로세스는 끝나고 다음 기동 때 이어서 |

- 개장 확인 예산 소진 시: 시작하지 않고 다음 예산 창까지 미룸 → 그때는 정규장이 지났으므로 A5 규칙대로 조회 없이 MISSED로 기록
  (늦은 가격으로 채우지 않음). 예약분(100)은 지정 종목 수 × 3 TR + 토큰보다 크게 두기.
- 루프: 실행한 작업이 있으면 바로 다음 순회, 없으면 다음 예정·다시 시도 시각까지(최대 poll_sec) 잠. 미뤄진 작업은 계획에 나오지 않음.
  안전장치로 쉬지 않고 50번 실행하면 한 번 쉼. `run --until-idle`은 **지금** 실행할 작업이 없으면 끝냄(미래의 예산 회복·다시 시도는
  기다리지 않음, 안전 상한 200회).
- **속도 제한 범위**: 호출 간격·429 재시도·토큰·하루 상한은 **이 관리자 프로세스 안에서만** 공유. `research_collect.py` 등 다른 조회
  프로세스는 자기 클라이언트·자기 간격을 따로 씀 — 같은 시간에 함께 돌리면 키움 쪽 호출 빈도는 합쳐짐(관리자 상한에 들어가지 않음).
  겹치지 않게 시간을 나누는 것을 권장(`doctor`가 예약 항목 안내).
- 한 프로세스·한 스레드·한 조회 클라이언트 — 관리자 안의 작업은 호출 간격(1초)·429 재시도·토큰을 공유.
  SQLite 연결을 스레드 사이에 나누지 않음. 다른 프로세스가 DB를 잠시 잠그면 SQLite busy 대기(30초) 뒤 진행.
- 장중 보유 가격 감시는 W3에서 이 우선순위·양보 구조에 붙임.

## 인증
- 토큰 응답의 `expires_dt`(YYYYMMDDHHMMSS — 운영 브로커 코드와 같은 필드, **형식은 미실측**)를 읽을 수 있으면 만료 10분 전부터 요청
  전에 새로 발급. 읽을 수 없으면 HTTP 401에서 한 번 재인증.
- 재인증은 10분 안 3번까지 — 넘으면 오류로 끝냄(무한 재발급 없음). 401 말고 다른 오류는 인증 오류로 보지 않음.

## 중복 실행·잠금
| 잠금 | 파일 | 대기 | 쓰는 곳 |
|---|---|---|---|
| 관리자 중복 기동 | `data/watch/daemon.sqlite3.lock` | 기다리지 않음(이미 있으면 종료 코드 2) | `run` |
| 설정 적용 | `data/watch/watch.sqlite3.config.lock` | 30초(`--lock-timeout`) | 관리자 설정 반영, CLI 편집·apply·status·prepare |
- 둘 다 OS 파일 잠금 — 프로세스가 죽으면 OS가 풀어 줌(강제 종료 뒤 바로 다시 기동 가능).
- 기동 때 남은 RUNNING 작업은 ABORTED로 바꾸고 다시 시도. 이전 실행 기록이 RUNNING이면 CRASHED로 표시.

## Windows 명령 (스윙 레포 루트, PowerShell)
```powershell
# 시작 — 창을 열어 둔 채(Ctrl+C로 종료)
python tools/watch_daemon.py run
# 백그라운드로 시작(창 없음)
Start-Process -WindowStyle Hidden -FilePath python -ArgumentList "tools/watch_daemon.py","run" -WorkingDirectory (Get-Location)
# 상태 확인
python tools/watch_daemon.py status
# 중지(다음 순회·대상 사이에서 멈춤) / 재시작
python tools/watch_daemon.py stop
python tools/watch_daemon.py status        # '중지됨' 확인 뒤
python tools/watch_daemon.py run
# 기존 작업 스케줄러와 겹치는지 확인(읽기만 — 바꾸지 않음)
python tools/watch_daemon.py doctor
# 그날 보고서 다시 만들기
python tools/watch_daemon.py report --day 2026-10-08
```
- 작업 스케줄러로 돌리려면 상시 `run` 대신 `run --until-idle`(지금 할 일만 하고 끝냄)을 18:20·09:06 등에 등록. 관리자가 이미 떠 있으면
  두 번째는 종료 코드 2로 바로 끝나므로 겹쳐도 중복 실행되지 않음. 관리자는 사용자 PC의 스케줄러를 바꾸지 않음.
- 기존 `watchlist.py prepare` 예약 항목은 관리자와 같은 일 — 관리자를 쓰면 정리 권장(`doctor`가 안내). `research_collect.py update`는
  대상이 달라 함께 써도 됨(같은 연구 DB를 써서 그 시간엔 서로 기다릴 수 있음).

## 상태 (`status`)
- 관리자: 실행 중(heartbeat N초 전) / 응답 없음(잠금은 있는데 heartbeat가 3×poll보다 오래됨 — stop 후 다시 run) /
  비정상 종료(잠금 없음·기록은 RUNNING — 다시 run하면 이어서) / 중지됨.
- 긴 작업 중에도 요청 경계에서 15초마다 heartbeat·진척(`작업 키 요청 N회 · 마지막 TR`) 갱신 — 정상 수집이 '응답 없음'으로 보이지 않음.
- 중지 응답 시간: `stop` 요청 시각과 실제 종료 시각을 함께 표시(대상·요청 경계에서 멈춤 — 한 요청 길이 + 호출 간격 이내 예상, 미실측).
- 거래일 달력이 지금·다음 거래일을 포함하지 않으면 `CALENDAR_UNAVAILABLE`(사유) — 조용히 기다리지 않음. `config/trading_calendar`를
  갱신한 뒤 관리자를 다시 시작.
- run_id·pid·현재 작업·마지막 오류, 사용 중 설정 버전·신규 진입 차단 사유, 오늘 호출 수, 다음 예정, 작업별 상태·시도/실패·다음 시도·
  설정 버전·오류(보고서 실패 포함).
- 설정 표시는 마지막 반영 기준(관리자가 매 순회 다시 반영) — status는 설정을 바꾸지 않음.

## 장애별 동작과 복구
| 장애 | 관리자 동작 | 사용자 조치 |
|---|---|---|
| 설정 오류(운영 중) | 마지막 정상 설정으로 계속, 신규 진입 관찰 차단(작업 기록 entry_blocked) | 설정 고치기(`watchlist.py validate`·`apply`) |
| 정상 설정 없음 | 작업 FAILED(NO_VALID_CONFIG)·조회 0, 5분 뒤 다시 | `watchlist.py init`·`add` |
| 미해결 적용 저널(손상·형식 오류·복원 실패) | 일반 적용 안 함, 마지막 정상 설정으로 감시 계속, 신규 진입 차단(JOURNAL_UNRESOLVED) — 저절로 풀지 않음 | `watchlist.py restore` 또는 파일 확인 뒤 `resolve-journal --keep-file` |
| 호출 예산 소진 | 시작하지 않고 다음 날 00:00까지 미룸(반복 기록 없음) | 필요하면 `--daily-call-cap` 조정 |
| 달력 범위 밖 | CALENDAR_UNAVAILABLE 기록·status 안내 | 달력 갱신 후 다시 run |
| API 오류·429·네트워크 | 429·전송 실패는 클라이언트가 대기 후 재시도, 그래도 실패하면 그 종목 오류(PARTIAL) → 다시 시도 | 계속되면 status 오류 확인 |
| 401·토큰 만료 | 재발급(만료 10분 전·401), 10분 안 3번 넘으면 작업 FAILED | `.env` 키 확인 |
| DB 잠김 | 30초 기다림, 그래도 잠기면 작업 FAILED → 5분 뒤 다시 | 다른 프로세스 확인 |
| 보고서 쓰기 실패 | 작업 결과는 저장(COMPLETE), 보고서 실패 표시·순회마다 다시 시도 | 폴더 권한 확인 → `report --day` |
| 강제 종료·정전 | 다음 기동 때 RUNNING → ABORTED, 바로 다시 시도 | 다시 `run` |
| 중복 기동 | 두 번째는 종료 코드 2 | — |
| 설정 적용 중 중단 | 적용 저널로 다음 실행이 원래 파일 복원 또는 확정 확인(docs/watchlist.md 적용 경계) | — |

## 저장·보고서
- `data/watch/daemon.sqlite3`(wd2 — task·task_event·daemon_run(+진척·중지 요청 시각)·call_usage·candidate_gate; wd1은 열 때 자동으로 올림), `watch_s1.sqlite3`(지정 종목 S1 관찰, 관찰 DB s4 형식),
  `watch_open.sqlite3`(개장 확인, A5 a3 형식). 기존 DB는 바꾸지 않음(새 파일).
- 보고서 `reports/watch/daily/watch_<D>.md`(작업·설정 버전·준비·관찰(계산)·운영 진입 게이트·개장 확인), `reports/watch/s1/`, `reports/watch/open/`. 로그
  `logs/watch_daemon.log`(토큰·앱키·계좌 값 가림). 모두 git 제외.
- 관찰 신호·관찰 가격은 체결이 아님. 수동 보유는 증권사 잔고가 아님. 과거 가정·실시간 관찰·모의 체결·실계좌 체결은 별도 기록(후속 단계).

- S1·개장 확인 하위 보고서 쓰기 실패는 작업 detail(`s1.report_error`·`report_error`)에만 남고 자동 재생성은 일일 보고서만 —
  하위 보고서 재생성 명령은 다음 단계(미구현).

## 실제 모의 도메인 조회 전용 운영 안내 (아직 실측 없음 — 사용자 PC에서 진행)
완료 기준은 날짜가 두 번 바뀌는 것이 아니라 **마감 준비 → 다음 개장 확인 최소 2쌍**(예: 10/7 저녁 → 10/8 아침, 10/8 저녁 → 10/12 아침 —
10/9 휴장·주말 포함; 실제 기동일에 맞춰 바꿈). PASS 0건도 정상일 수 있음 — 신호가 없다고 조건을 완화하지 않음.

```powershell
git pull
python tools/watchlist.py status                 # 설정 정상·신규 진입 관찰 열 확인(미해결 저널이면 restore/resolve-journal 먼저)
python tools/watch_daemon.py doctor              # 겹치는 작업 스케줄러 항목 확인(읽기만)
python tools/watch_daemon.py run                 # 창을 열어 둔 채 시작(또는 위 Start-Process)
python tools/watch_daemon.py status              # 다른 창에서 수시로 — 진척·오늘 호출·다음 예정·달력 상태
# 확인 1회씩: 제어된 중지·재시작, 중복 기동(두 번째 run → 종료 코드 2)
python tools/watch_daemon.py stop; python tools/watch_daemon.py status; python tools/watch_daemon.py run
```

확인할 것(관찰만 — 이 레포가 대신 판단하지 않음):
| 항목 | 어디서 |
|---|---|
| 작업별 due·실제 요청/수신·완료 시각, 호출 수, 지연·누락·차단 사유 | `status --json`, `reports/watch/daily/`, 로그 |
| 후보 근거(계산 PASS vs 운영 게이트 제외 사유)·설정 버전·계약 | 일일 보고서 '운영 진입 게이트', `reports/watch/open/` |
| 토큰 `expires_dt` — **필드 존재·형식만**(로그는 타입·길이·해석 결과만 남김) | 로그 `[인증] 토큰 발급` 줄 |
| 24시간 이상 유지 뒤 인증 갱신, 자정 전환, 휴장일 흐름 | 로그·status |
| 160분 완성 기준: 마감 뒤 여러 시각과 다음 거래일의 같은 날짜 OHLCV·거래대금 대조 | `research_collect.py` 조회 결과(별도 작업) |

번들 모으기(공유용 — token·appkey·secret·계좌 값은 로그에서 이미 가림, 그래도 열어서 한 번 더 확인):
```powershell
$d = Get-Date -Format yyyyMMdd
New-Item -ItemType Directory -Force exports\watch_$d | Out-Null
python tools/watch_daemon.py status --json > exports\watch_$d\status.json
python tools/watchlist.py status > exports\watch_$d\watchlist_status.txt
Copy-Item logs\watch_daemon.log exports\watch_$d\ -ErrorAction SilentlyContinue
Copy-Item -Recurse reports\watch exports\watch_$d\reports -ErrorAction SilentlyContinue
Copy-Item data\watch\daemon.sqlite3 exports\watch_$d\ -ErrorAction SilentlyContinue   # 작업·게이트·호출 수(계좌 정보 없음)
Select-String -Path exports\watch_$d\* -Pattern "token|appkey|secret|acnt" -SimpleMatch | Select-Object -First 20   # 가려졌는지(***) 확인
Compress-Archive -Force exports\watch_$d exports\watch_bundle_$d.zip
```
- `.env`·`config/watchlist.yaml`(수동 보유 값)은 번들에 넣지 않음. 필요하면 `watchlist.py status` 출력만.

## 미실측
- 실제 API로 운영한 로그·보고서는 아직 없음 — 가짜 API·가짜 시계 시험 출력만('2거래일' 시험도 가짜 날짜 전환). 토큰 `expires_dt`
  형식, 장 마감 뒤 완성 시각(160분), 중지 응답 시간, 개장 확인 예약분의 적정값도 미실측.
