# W2 조회 전용 상시 실행 관리자 — 운영 명세 (2026-10-07)

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

## 작업 키·상태·재시도
- task_key = `CLOSE_PREP|D|scope:<감시·S1 대상 코드 해시>` / `OPEN_CHECK|D|OPEN+5m`. 실행한 계산 계약·설정 버전은 행에 기록.
- 상태: PENDING → RUNNING → COMPLETE / PARTIAL(일부 조회 실패) / YIELDED(양보) / FAILED(예외) / ABORTED(중단) / MISSED.
- PARTIAL·FAILED·ABORTED: 5·15·30·60분 뒤 다시(실패 4번까지 — 넘으면 status에 남기고 멈춤). YIELDED: 실패로 세지 않고 바로 다음 차례.
- COMPLETE는 다시 실행하지 않음 — 보고서만 `report --day`.

## 우선순위·양보·호출 예산
- OPEN_CHECK > CLOSE_PREP(오래된 날 먼저). 긴 준비는 대상마다 확인해 양보: 실행 가능한 오늘 OPEN_CHECK(`PRIORITY:OPEN_CHECK`),
  중지 요청(`STOP_REQUESTED`), 하루 호출 상한(`CALL_BUDGET`, 기본 3,000회 — 모든 작업 합계), 작업 시간 상한(`TIME_BUDGET`, 기본 30분).
- 한 프로세스·한 스레드·한 조회 클라이언트 — 호출 간격(1초)·429 재시도·토큰을 모든 작업이 공유(작업별 독립 제한 없음).
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
- run_id·pid·현재 작업·마지막 오류, 사용 중 설정 버전·신규 진입 차단 사유, 오늘 호출 수, 다음 예정, 작업별 상태·시도/실패·다음 시도·
  설정 버전·오류(보고서 실패 포함).
- 설정 표시는 마지막 반영 기준(관리자가 매 순회 다시 반영) — status는 설정을 바꾸지 않음.

## 장애별 동작과 복구
| 장애 | 관리자 동작 | 사용자 조치 |
|---|---|---|
| 설정 오류(운영 중) | 마지막 정상 설정으로 계속, 신규 진입 관찰 차단(작업 기록 entry_blocked) | 설정 고치기(`watchlist.py validate`·`apply`) |
| 정상 설정 없음 | 작업 FAILED(NO_VALID_CONFIG)·조회 0, 5분 뒤 다시 | `watchlist.py init`·`add` |
| API 오류·429·네트워크 | 429·전송 실패는 클라이언트가 대기 후 재시도, 그래도 실패하면 그 종목 오류(PARTIAL) → 다시 시도 | 계속되면 status 오류 확인 |
| 401·토큰 만료 | 재발급(만료 10분 전·401), 10분 안 3번 넘으면 작업 FAILED | `.env` 키 확인 |
| DB 잠김 | 30초 기다림, 그래도 잠기면 작업 FAILED → 5분 뒤 다시 | 다른 프로세스 확인 |
| 보고서 쓰기 실패 | 작업 결과는 저장(COMPLETE), 보고서 실패 표시·순회마다 다시 시도 | 폴더 권한 확인 → `report --day` |
| 강제 종료·정전 | 다음 기동 때 RUNNING → ABORTED, 바로 다시 시도 | 다시 `run` |
| 중복 기동 | 두 번째는 종료 코드 2 | — |
| 설정 적용 중 중단 | 적용 저널로 다음 실행이 원래 파일 복원 또는 확정 확인(docs/watchlist.md 적용 경계) | — |

## 저장·보고서
- `data/watch/daemon.sqlite3`(wd1 — task·task_event·daemon_run·call_usage), `watch_s1.sqlite3`(지정 종목 S1 관찰, 관찰 DB s4 형식),
  `watch_open.sqlite3`(개장 확인, A5 a3 형식). 기존 DB는 바꾸지 않음(새 파일).
- 보고서 `reports/watch/daily/watch_<D>.md`(작업·설정 버전·준비·관찰·개장 확인), `reports/watch/s1/`, `reports/watch/open/`. 로그
  `logs/watch_daemon.log`(토큰·앱키·계좌 값 가림). 모두 git 제외.
- 관찰 신호·관찰 가격은 체결이 아님. 수동 보유는 증권사 잔고가 아님. 과거 가정·실시간 관찰·모의 체결·실계좌 체결은 별도 기록(후속 단계).

## 미실측
- 실제 API로 2거래일 이상 운영한 로그·보고서는 아직 없음 — 가짜 API 시험 출력만. 토큰 `expires_dt` 형식, 장 마감 뒤 완성 시각(160분)도 미실측.
