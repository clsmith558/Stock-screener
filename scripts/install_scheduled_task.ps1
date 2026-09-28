<#
.SYNOPSIS
  Register Windows Task Scheduler job for screener background updates.

.DESCRIPTION
  Runs scripts/run_scheduled_updates.py on an interval (default every 12 hours)
  and optionally at a daily clock time. Configures run-missed-task-ASAP so if
  the PC was asleep at the scheduled time, the job runs after the next wake.

  Does NOT run while the machine is asleep (S3 sleep freezes the CPU).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1

  powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1 -IntervalHours 6 -DailyTime 07:30

  powershell -ExecutionPolicy Bypass -File scripts\install_scheduled_task.ps1 -Unregister
#>

param(
    [string]$TaskName = "SignalScreener-ScheduledUpdates",
    [int]$IntervalHours = 12,
    [string]$DailyTime = "07:30",
    [switch]$WakeToRun = $false,
    [switch]$Unregister = $false
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Script = Join-Path $RepoRoot "scripts\run_scheduled_updates.py"
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
    throw "Missing orchestrator script: $Script"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Quote script path for Task Scheduler argument
$arg = '"' + $Script + '"'
$action = New-ScheduledTaskAction -Execute $Python -Argument $arg -WorkingDirectory $RepoRoot

$triggers = @()
if ($IntervalHours -gt 0) {
    $start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes(5)
    if ($start -lt (Get-Date)) { $start = $start.AddHours(1) }
    $triggers += New-ScheduledTaskTrigger -Once -At $start `
        -RepetitionInterval (New-TimeSpan -Hours $IntervalHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
}

if ($DailyTime -and $DailyTime.Trim() -ne "") {
    $parts = $DailyTime.Split(":")
    $h = [int]$parts[0]
    $m = if ($parts.Length -gt 1) { [int]$parts[1] } else { 0 }
    $dailyAt = (Get-Date).Date.AddHours($h).AddMinutes($m)
    $triggers += New-ScheduledTaskTrigger -Daily -At $dailyAt
}

if (-not $triggers.Count) {
    throw "No triggers configured (set -IntervalHours and/or -DailyTime)"
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 15)

if ($WakeToRun) {
    try {
        $settings.WakeToRun = $true
    } catch {
        Write-Warning "Could not set WakeToRun"
    }
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered scheduled task: $TaskName"
Write-Host "  Python:  $Python"
Write-Host "  Script:  $Script"
Write-Host "  Workdir: $RepoRoot"
Write-Host "  IntervalHours: $IntervalHours"
if ($DailyTime) {
    Write-Host "  DailyTime:     $DailyTime"
} else {
    Write-Host "  DailyTime:     (none)"
}
Write-Host "  StartWhenAvailable (missed catch-up): yes"
Write-Host "  WakeToRun: $WakeToRun"
Write-Host ""
Write-Host "Manual test:"
Write-Host ("  " + $Python + " " + $Script + " --status")
Write-Host ("  " + $Python + " " + $Script + " --dry-run")
Write-Host ("  Start-ScheduledTask -TaskName " + $TaskName)
Write-Host ""
Write-Host "Note: Tasks do not run while the PC is asleep (S3). Missed runs start after wake."
Write-Host "Note: A console window stays open while the job runs (yf_lists can take hours)."
Write-Host "      Live progress is written to logs\scheduled_updates.log and the console."
Write-Host "      The window closes only after all jobs finish."
