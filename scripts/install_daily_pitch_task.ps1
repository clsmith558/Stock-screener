<#
.SYNOPSIS
  Register Windows Task Scheduler job for Daily Stock Research Pitch at 8:00 AM.

.DESCRIPTION
  Runs scripts/daily_stock_pitch.py every day at 8:00 AM.
  Rotates across:
    1. ASX Miners & Producers
    2. Asian Market Opportunities
    3. US Large-Cap Value

  Includes StartWhenAvailable so if the PC was asleep at 8:00 AM, the task
  executes as soon as the machine wakes up.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_daily_pitch_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install_daily_pitch_task.ps1 -DailyTime "08:00"
  powershell -ExecutionPolicy Bypass -File scripts\install_daily_pitch_task.ps1 -Unregister
#>

param(
    [string]$TaskName = "SignalScreener-DailyStockPitch",
    [string]$DailyTime = "08:00",
    [switch]$Unregister = $false
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\daily_stock_pitch.py"
$LogDir = Join-Path $RepoRoot "logs"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Unregistered task: $TaskName"
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Warning "Venv python not found at $Python - using python on PATH"
    $Python = "python"
}
if (-not (Test-Path $Script)) {
    throw "Missing script: $Script"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$arg = '"' + $Script + '"'
$action = New-ScheduledTaskAction -Execute $Python -Argument $arg -WorkingDirectory $RepoRoot

$parts = $DailyTime.Split(":")
$h = [int]$parts[0]
$m = if ($parts.Length -gt 1) { [int]$parts[1] } else { 0 }
$dailyAt = (Get-Date).Date.AddHours($h).AddMinutes($m)

$trigger = New-ScheduledTaskTrigger -Daily -At $dailyAt

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Successfully registered scheduled task '$TaskName' to run daily at $DailyTime with wake/catch-up enabled."
