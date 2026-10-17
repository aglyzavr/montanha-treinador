# Registers the two scheduled tasks (Windows replacement for the launchd plists
# + `pmset repeat wake`). Safe to re-run: existing tasks are replaced.
#
#   Montanha Coach Daily   every day   06:30
#   Montanha Coach Weekly  Mondays     07:30
#
# Both: wake the PC from sleep to run, run as soon as possible if the PC was
# off at that time, never two at once (see run_job.ps1). They run as you, only
# while you're signed in (sleep/lock is fine) -- this avoids storing your
# Windows password in Task Scheduler.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $Root "scripts\run_job.ps1"

function Register-CoachTask([string]$Name, [string]$Job, $Trigger) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`" -Job $Job" `
        -WorkingDirectory $Root
    $settings = New-ScheduledTaskSettingsSet `
        -WakeToRun `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger -Settings $settings `
        -Principal $principal -Description "Montanha coach $Job job ($Root)" -Force | Out-Null
    $next = (Get-ScheduledTask -TaskName $Name | Get-ScheduledTaskInfo).NextRunTime
    Write-Host "  Registered '$Name' -- next run $next"
}

Register-CoachTask "Montanha Coach Daily"  "daily"  (New-ScheduledTaskTrigger -Daily -At "06:30")
Register-CoachTask "Montanha Coach Weekly" "weekly" (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "07:30")

# Waking from sleep only works if the power plan allows wake timers.
try {
    powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
    powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
    Write-Host "  Wake timers enabled (when plugged in)."
} catch {
    Write-Host "  Could not enable wake timers automatically. Set it by hand:"
    Write-Host "  Control Panel > Power Options > Change plan settings > Advanced > Sleep > Allow wake timers > Enable"
}

Write-Host ""
Write-Host "  Keep the PC asleep (not shut down) overnight. Shut down = the job runs"
Write-Host "  when you next turn it on and sign in, and the message arrives then."
