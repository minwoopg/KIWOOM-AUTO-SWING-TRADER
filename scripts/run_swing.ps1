# 스윙 자동매매 하루 실행 (작업 스케줄러가 부르는 스크립트)
#
# - 저장소 루트로 이동해 python -m app.main 실행 (휴장일이면 프로그램이 바로 종료)
# - 콘솔 출력은 logs\scheduler\run_<날짜>.log 에 남김 (app.log와 별개)
# - 종료 코드를 그대로 돌려줘 작업 스케줄러 "마지막 실행 결과"에 표시되게 함
#
# 수동 실행:  powershell -ExecutionPolicy Bypass -File scripts\run_swing.ps1
param(
    [string]$Python = "python",        # 가상환경을 쓰면 .venv\Scripts\python.exe 경로
    [switch]$CheckOnly                 # 기동 점검만
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$LogDir = Join-Path $Root "logs\scheduler"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$Log = Join-Path $LogDir "run_$Stamp.log"

$env:PYTHONUTF8 = "1"
$ArgsList = @("-m", "app.main")
if ($CheckOnly) { $ArgsList += "--check-only" }

"[$(Get-Date -Format s)] 시작: $Python $($ArgsList -join ' ') (cwd=$Root)" | Out-File -FilePath $Log -Encoding utf8
# 파이썬의 표준오류 출력이 PowerShell 오류로 바뀌어 스크립트가 멈추지 않도록 Continue로 실행
$ErrorActionPreference = "Continue"
& $Python @ArgsList 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath $Log -Encoding utf8 -Append
$Code = $LASTEXITCODE
"[$(Get-Date -Format s)] 종료 코드: $Code (0 정상 / 1 비정상 종료 / 2 마감 검증 NEEDS_REVIEW — reports\session_status_<날짜>.json 확인)" | Out-File -FilePath $Log -Encoding utf8 -Append
exit $Code
