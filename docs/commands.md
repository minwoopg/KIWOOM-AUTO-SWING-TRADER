# 스윙 자동매매 명령어 모음

모든 명령은 저장소 폴더(`swing-auto-trader`)에서 PowerShell로 실행합니다.

보고서·로그(UTF-8)를 PowerShell에서 볼 때는 `-Encoding UTF8`을 붙입니다(없으면 한글이 깨짐).
```powershell
Get-Content reports\watch\daily\watch_2026-10-14.md -Encoding UTF8
Get-Content data\strategy_decisions.jsonl -Encoding UTF8
Select-String -Path logs\app.log -Pattern "SHADOW|CANDIDATES" -Encoding UTF8 | Select-Object -Last 20
```

## 1. 관리자 (watch_daemon — 조회 전용, 상시 실행)

관리자는 **전용 창**에서 켜고, 그 창은 닫지 않습니다.
```powershell
python -u tools/watch_daemon.py run 2>&1 | Tee-Object -FilePath logs\watch_console.log
```

상태 확인·중지·보고서는 **다른 창**에서 합니다.
```powershell
python tools/watch_daemon.py status
python tools/watch_daemon.py stop
python tools/watch_daemon.py report --day 2026-10-13
python tools/watch_daemon.py doctor
python tools/watch_daemon.py export-db --out exports\watch_20261013\db
```

| 명령 | 용도 |
|---|---|
| `status` | 실행 여부·작업 결과·다음 예정 |
| `stop` | 중지 요청 — 코드·종목을 바꾸기 전에 먼저 |
| `report --day` | 그날 일일 보고서 다시 만들기 |
| `doctor` | 작업 스케줄러 항목과 겹치는지 확인(읽기만) |
| `export-db --out` | 관리자 DB 사본(공유용) |

## 2. 관심 종목 (watchlist)

종목을 바꾸기 전에 관리자를 멈추고, 바꾼 뒤 `status`로 확인하고 다시 켭니다.
```powershell
python tools/watch_daemon.py stop
python tools/watchlist.py add 005930 --interest --s1
python tools/watchlist.py status
python -u tools/watch_daemon.py run 2>&1 | Tee-Object -FilePath logs\watch_console.log
```

```powershell
python tools/watchlist.py status
python tools/watchlist.py history
python tools/watchlist.py add 005930 --interest --s1
python tools/watchlist.py add 005930 --interest --s1 --band 70000-72000:눌림
python tools/watchlist.py set 005930 --clear-bands
python tools/watchlist.py disable 005930
python tools/watchlist.py enable 005930
python tools/watchlist.py remove 005930
python tools/watchlist.py validate
python tools/watchlist.py apply
python tools/watchlist.py prepare
python tools/watchlist.py restore
```

| 명령 | 용도 |
|---|---|
| `add --interest --s1` | 관심·S1 분석 대상으로 추가 |
| `add ... --band 낮은값-높은값:이름` | 관심 가격대를 함께 지정 |
| `set --clear-bands` | 가격대 지우기 |
| `disable` / `enable` | 관심 끄기·켜기 |
| `remove` | 삭제(수동 보유가 있으면 거부) |
| `validate` | 설정 파일 검사만 |
| `apply` | 파일을 직접 고친 뒤 적용 |
| `prepare` | 등록 종목 일봉 갱신·준비 상태 판정 |
| `restore` | 마지막 정상 설정으로 되돌림 |

관찰 확대(E1): 후보 보기(읽기만) → 확인 → 한 번에 추가. 관리자를 멈춘 상태에서 합니다.
```powershell
python tools/watchlist.py suggest --kospi 20 --kosdaq 10
python tools/watchlist.py add-many "000660,035420" --interest --s1
```

신호 결과 측정(E3, 연구용 일봉 근사 — 운영 체결 재현 아님) — `reports\research\s1_outcome_<날짜>_<ID>.md`
```powershell
python tools/s1_outcome.py --days 250
```

S1 신호 빈도 분석(E1, 조회 전용) — 결과는 `reports\research\s1_frequency_<날짜>.md` · `.csv`
```powershell
python tools/research_collect.py backfill --codes "005930,000660" --from 2025-01-01   # 일봉이 모자라면
python tools/s1_frequency.py --days 250
python tools/s1_frequency.py --days 250 --group "base5=005930,005380,010120,058470,196170" --group "all=watch"   # 그룹·기간 비교(E3)
Get-Content reports\research\s1_frequency_2026-10-21.md -Encoding UTF8
```

## 3. 실행기 (app.main — 작업 스케줄러가 매일 기동)

```powershell
Start-ScheduledTask -TaskName SwingAutoTrader
Stop-ScheduledTask -TaskName SwingAutoTrader
python -m app.main --check-only
python -m app.main
```

| 명령 | 용도 |
|---|---|
| `Start-ScheduledTask` / `Stop-ScheduledTask` | 작업 스케줄러 실행기 시작·멈춤 |
| `--check-only` | 기동 점검만(주문 없음) |
| `python -m app.main` | 직접 실행(하루 수명주기) |

자동 전략 모드는 `config/settings.yaml` 맨 끝에서 정합니다.
```yaml
swing_strategy:
  mode: shadow        # off(기본) / shadow(기록만) / paper(모의 전송 — C3 사전 점검 통과 시)
  # paper에 필요(C3) — 자세한 내용은 docs/m3_c3_daily_risk_paper.md
  # allowed_symbols: ["005930"]   # 신규 매수 허용 종목(paper 필수)
  # daily_loss_limit: 10000       # 기본 10,000원
  # max_failed_orders: 1          # 기본 1건
```

일일 손실·실패 주문 한도로 중단되면 `commands/halt.json`(by=daily_risk)이 생깁니다. 그날은 풀어도 신규 매수가 없고,
다음 거래일부터 다시 가능 — 원인 확인 뒤 아래 4번 `resume`.

## 4. 신규 매수 중단 (매도·손절은 계속)

```powershell
python tools/buy_halt.py request --reason "점검"
python tools/buy_halt.py status
python tools/buy_halt.py resume --note "확인 내용" --confirm
```

`resume`은 사람이 확인한 뒤에만 합니다(자동 재개 없음).

## 5. 관리 창

```powershell
scripts\run_dashboard.bat
python -m dashboard.desktop
```

`run_dashboard.bat`은 콘솔 없이 창만 띄우고, `python -m dashboard.desktop`은 오류를 화면에서 볼 때 씁니다.

## 6. 모의 주문 수동 도구 (paper_order — KIWOOM_PAPER에서만)

`--execute`를 붙이지 않으면 미리보기만 합니다(네트워크 없음). 명령 ID는 한 번만 쓸 수 있으니 매번 새로 정합니다.

```powershell
python tools/paper_order.py status
python tools/paper_order.py check-orders
python tools/paper_order.py check-orders --order 0112293:005930
python tools/paper_order.py confirm-fills
python tools/paper_order.py confirm-fills --confirm
python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014
python tools/paper_order.py buy-one 005930 --command-id m1-buy-1014 --execute
python tools/paper_order.py sell-held 005930 --command-id m1-sell-1014 --execute
python tools/paper_order.py limit-cancel 005930 --command-id m1-lc-1014 --execute
python tools/paper_order.py ack-limit-cancel m1-lc-1014 --note "HTS 확인"
```

| 명령 | 용도 |
|---|---|
| `status` | 최근 명령 기록 |
| `check-orders` | 오늘 주문의 미체결·체결 조회(읽기 전용) |
| `check-orders --order 주문번호:종목` | 특정 주문만 조회 |
| `confirm-fills` | 오늘 체결의 실제 체결가·수수료·세금 미리보기 |
| `confirm-fills --confirm` | 위 결과를 체결 근거로 기록(원장은 그대로) |
| `buy-one` | 1주 시장가 매수 |
| `sell-held` | 원장 보유 1주 매도 |
| `limit-cancel` | 지정가 1주 → 취소 경로 실측 |
| `ack-limit-cancel` | 확인 안 된 limit-cancel을 사람이 확인했다고 기록 |

## 7. 원장 검토·정정

`correct`는 `--confirm`이 없으면 미리보기만 합니다.
```powershell
python tools/ledger_review.py status
python tools/ledger_review.py note 005930 --reason "HTS와 일치 확인"
python tools/ledger_review.py correct 005930 --side BUY --qty 1 --price 251000 --trade-date 2026-10-14 --order-id 0112293 --reason "체결 누락" --evidence "HTS 체결내역"
python tools/ledger_review.py correct 005930 --side BUY --qty 1 --price 251000 --trade-date 2026-10-14 --order-id 0112293 --reason "체결 누락" --evidence "HTS 체결내역" --confirm
```

## 8. 보고서·공유 묶음

```powershell
python tools/daily_report.py
python tools/daily_report.py --date 2026-10-13
python tools/export_bundle.py
python tools/export_bundle.py --date 2026-10-13
```

| 명령 | 용도 |
|---|---|
| `daily_report.py` | 오늘 일일 리포트(`--date`로 다른 날) |
| `export_bundle.py` | 그날 운영 묶음 zip — 비밀값을 가림, GPT 공유용(`--date`로 다른 날) |

## 9. 진단·실측 (읽기 전용)

```powershell
python diag_w2.py 005930 2026-10-13 --out logs\diag_20261013.txt
python tools/probe_market_basis.py 005930 --days 5 --out logs\market_basis_20261013.json
```

| 명령 | 용도 |
|---|---|
| `diag_w2.py` | 관리자·관찰 DB 진단 |
| `probe_market_basis.py` | 시장 기준 실측(단축코드·_NX·_AL 비교) |

`--out` 파일은 새 이름이어야 합니다(이미 있으면 거부).

## 10. 패치 적용 (받은 zip)

`swing_xxx`는 받은 zip 이름으로 바꿉니다. 실행기·관리자 코드가 바뀌는 패치면 먼저 멈춥니다.
```powershell
python tools/watch_daemon.py stop
Expand-Archive -Path "$HOME\Downloads\swing_xxx.zip" -DestinationPath . -Force
git am (Get-ChildItem swing_xxx\*.patch | Sort-Object Name | ForEach-Object FullName)
python run_regression_tests.py --skip test_broker_order_status.py
Remove-Item swing_xxx -Recurse
git push origin main
```

`git am`이 실패하면 상태를 확인하고 되돌립니다.
```powershell
git am --abort
git status
```

## 11. 연구 수집 (전체 시장 — 지금 운영에는 쓰지 않음)

```powershell
python tools/research_collect.py status
python tools/research_collect.py universe
python tools/research_collect.py update
python tools/research_collect.py scan
```

| 명령 | 용도 |
|---|---|
| `status` | 연구 DB 상태 |
| `universe` | 종목 목록 스냅숏 |
| `update` | 전체 시장 일봉 추가 + S1 스캔 |
| `scan` | S1 스캔만(조회 없음) |
