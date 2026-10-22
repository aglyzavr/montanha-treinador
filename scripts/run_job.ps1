# Runs one coach job. This is what Task Scheduler calls (Windows replacement
# for the launchd plists). Can also be run by hand:
#     powershell -ExecutionPolicy Bypass -File scripts\run_job.ps1 -Job daily
param(
    [Parameter(Mandatory = $true)][ValidateSet("daily", "weekly")] [string]$Job
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
$Log = "$Root\logs\$Job.log"
$env:PYTHONUTF8 = "1"

function Write-Log([string]$text) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $text" | Out-File -FilePath $Log -Append -Encoding utf8
}

# Daily (06:30) and weekly (Monday 07:30) can overlap when the model is slow,
# and two copies of a 22 GB model do not fit in 32 GB. One job at a time.
$mutex = New-Object System.Threading.Mutex($false, "MontanhaCoachJob")
if (-not $mutex.WaitOne([TimeSpan]::FromMinutes(90))) {
    Write-Log "$Job skipped: another coach job held the lock for 90 min"
    exit 1
}

try {
    # The Python job starts Ollama itself and stops it when done
    # (runtime.ollama_session), so nothing is left running afterwards.
    Write-Log "---- $Job start ----"
    $py = "$Root\.venv\Scripts\python.exe"
    $ErrorActionPreference = "Continue"     # Python writes progress to stderr too
    & $py -m "src.$Job" *>&1 | ForEach-Object { Write-Log "$_" }
    $code = $LASTEXITCODE
    Write-Log "---- $Job end (exit $code) ----"
    exit $code
}
finally {
    $mutex.ReleaseMutex()
}
