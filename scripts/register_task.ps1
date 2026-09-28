# Windows 작업 스케줄러 등록 — 평일 장 시작 전에 스윙 자동매매 실행
#
# 관리자 권한 불필요(현재 사용자 계정으로 등록, 로그온 상태일 때 실행).
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 08:40 -Python "C:\...\.venv\Scripts\python.exe"
#
# - 월~금 지정 시각에 scripts\run_swing.ps1 실행. 공휴일은 프로그램이 캘린더로 판단해 바로 종료.
# - 이미 실행 중이면 새로 띄우지 않음(IgnoreNew) — 프로그램 자체 중복 실행 락과 이중 보호.
# - 최대 실행 시간 9시간(비정상적으로 끝나지 않을 때 강제 종료).
# - 노트북 배터리 모드에서도 실행/중단하지 않음.
param(
    [string]$TaskName = "SwingAutoTrader",
    [string]$Time = "08:40",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Root "scripts\run_swing.ps1"
if (-not (Test-Path $Script)) { throw "run_swing.ps1을 찾을 수 없습니다: $Script" }

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Python `"$Python`"" `
    -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Time
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 9) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "스윙 자동매매 하루 실행 ($Root)" -Force | Out-Null

Write-Host "등록 완료: '$TaskName' — 평일 $Time, $Script"
Write-Host "확인:  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "지금 한 번 실행:  Start-ScheduledTask -TaskName $TaskName"
