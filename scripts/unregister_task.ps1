# 작업 스케줄러에서 스윙 자동매매 등록 해제
#   powershell -ExecutionPolicy Bypass -File scripts\unregister_task.ps1
param([string]$TaskName = "SwingAutoTrader")
$ErrorActionPreference = "Stop"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "해제 완료: $TaskName"
} else {
    Write-Host "등록된 작업 없음: $TaskName"
}
