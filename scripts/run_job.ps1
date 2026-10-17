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
    # Ollama's tray app normally starts at login. If it isn't up (e.g. it was
    # quit), start the server in the background and give it a minute.
    $up = $false
    try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null; $up = $true } catch {}
    if (-not $up) {
        $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
        if (-not $ollama) { $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
        if (Test-Path $ollama) {
            Write-Log "Ollama not running; starting it"
            Start-Process -FilePath $ollama -ArgumentList "serve" -WindowStyle Hidden
            for ($i = 0; $i -lt 30 -and -not $up; $i++) {
                Start-Sleep -Seconds 2
                try { Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null; $up = $true } catch {}
            }
        }
        # If it still isn't up, the Python job reports it on Telegram.
    }

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
