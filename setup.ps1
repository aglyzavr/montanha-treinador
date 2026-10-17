# Montanha coach -- one-shot setup for Windows.
#
#   Right-click > "Run with PowerShell", or from a terminal in this folder:
#       powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# Safe to re-run at any time: every step checks first and skips what's done.
# Everything stays on this PC. The only things that ever leave it are the
# Garmin download (your own account) and the final Telegram message.

# "Continue", not "Stop": Windows PowerShell 5.1 turns ANY stderr output from
# a native program (pip warnings, ollama's progress bar, the py launcher's
# "No suitable Python runtime found") into a fatal error under "Stop". Each
# step checks $LASTEXITCODE itself instead.
$ErrorActionPreference = "Continue"
$Root = $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"

function Step([string]$text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Info([string]$text) { Write-Host "   $text" }
function Warn([string]$text) { Write-Host "   $text" -ForegroundColor Yellow }
function Ask([string]$q) { return (Read-Host "   $q [y/N]") -match '^(y|yes)$' }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

# ---------------------------------------------------------------- 1. Python
Step "1/8  Python 3.12+"
function Find-Python {
    # The py launcher can exist with no Python behind it, and "python" can be
    # the Microsoft Store stub; only trust a candidate that prints a version.
    foreach ($cand in @("py -3.13", "py -3.12", "python")) {
        $exe, $arg = $cand.Split(" ", 2)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $ver = & $exe $arg -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ver" -match '^\d+\.\d+$' -and [version]"$ver" -ge [version]"3.12") {
            return $cand
        }
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Warn "Python 3.12+ not found."
    if (Ask "Install Python 3.13 with winget now?") {
        winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $py = Find-Python
        if (-not $py) {
            Warn "Python was installed, but this window can't see it yet."
            Warn "Close this terminal, open a new one in this folder, and run setup.ps1 again."
            exit 1
        }
    } else {
        Warn "Install Python 3.13 from python.org (tick 'Add to PATH'), then re-run setup.ps1."
        exit 1
    }
}
Info "Using: $py"

# ---------------------------------------------------------------- 2. venv
Step "2/8  Virtual environment + dependencies"
$venvPy = "$Root\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $exe, $arg = $py.Split(" ", 2)
    & $exe $arg -m venv .venv
    if (-not (Test-Path $venvPy)) { Warn "Could not create .venv (see the error above)."; exit 1 }
    Info "Created .venv"
}
& $venvPy -m pip install --quiet --disable-pip-version-check -e . pytest
if ($LASTEXITCODE -ne 0) { Warn "Installing dependencies failed (see above)."; exit 1 }
Info "Dependencies installed."

# ---------------------------------------------------------------- 3. .env
Step "3/8  Secrets (.env)"
if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Warn "Created .env from the template. Fill in Garmin + Telegram, save, close Notepad."
    Start-Process notepad.exe "$Root\.env" -Wait
} else { Info ".env exists (not shown)." }

# ---------------------------------------------------------------- 4. data
Step "4/8  Private data (data/)"
foreach ($f in @("plan.json", "coach_principles.md", "strength_reference_uphill_athlete.md", "coach.db")) {
    if (Test-Path "$Root\data\$f") { Info "found  data\$f" } else { Warn "MISSING data\$f" }
}
if (-not (Test-Path "$Root\data\plan.json")) {
    Warn "Without plan.json the coach can't prescribe anything. Copy it (and the"
    Warn "other files) from the SSD backup or the old Mac into data\, then re-run."
}

# ---------------------------------------------------------------- 5. Ollama
Step "5/8  Ollama + model (local, nothing leaves the PC)"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    if (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") { $env:Path += ";$env:LOCALAPPDATA\Programs\Ollama" }
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    if (Ask "Ollama not found. Install it with winget now?") {
        winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        Refresh-Path
        $env:Path += ";$env:LOCALAPPDATA\Programs\Ollama"
    } else { Warn "Install Ollama from ollama.com/download, then re-run setup.ps1."; exit 1 }
}
# Flash attention + 8-bit KV cache roughly halve the memory the 32k context
# window needs -- the difference between fitting in 32 GB RAM or not.
$restartOllama = $false
foreach ($pair in @(@("OLLAMA_FLASH_ATTENTION", "1"), @("OLLAMA_KV_CACHE_TYPE", "q8_0"))) {
    if ([Environment]::GetEnvironmentVariable($pair[0], "User") -ne $pair[1]) {
        [Environment]::SetEnvironmentVariable($pair[0], $pair[1], "User")
        $restartOllama = $true
    }
    Set-Item -Path "Env:$($pair[0])" -Value $pair[1]
}
if ($restartOllama) {
    Info "Set OLLAMA_FLASH_ATTENTION=1 and OLLAMA_KV_CACHE_TYPE=q8_0; restarting Ollama to apply."
    Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 2
}
$up = $false
try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null; $up = $true } catch {}
if (-not $up) {
    $app = "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
    if (Test-Path $app) { Start-Process $app } else { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden }
    for ($i = 0; $i -lt 30 -and -not $up; $i++) {
        Start-Sleep 2
        try { Invoke-RestMethod "http://localhost:11434/api/tags" -TimeoutSec 5 | Out-Null; $up = $true } catch {}
    }
}
if (-not $up) { Warn "Ollama did not start. Open the Ollama app, then re-run setup.ps1."; exit 1 }
$model = & $venvPy -c "from src.config import CONFIG; print(CONFIG['model']['name'])"
$have = (Invoke-RestMethod "http://localhost:11434/api/tags").models.name
if ($have -contains $model -or $have -contains "$model`:latest") {
    Info "Model $model already pulled."
} else {
    Info "Downloading $model (about 22 GB, one time)..."
    ollama pull $model
    if ($LASTEXITCODE -ne 0) { Warn "Model download failed; re-run setup.ps1 to resume it."; exit 1 }
}

# ---------------------------------------------------------------- 6. Garmin
Step "6/8  Garmin login (once; jobs reuse the cached token)"
$tokenDir = "$env:USERPROFILE\.garminconnect"
if ((Test-Path $tokenDir) -and (Get-ChildItem $tokenDir -ErrorAction SilentlyContinue)) {
    Info "Token already cached at $tokenDir"
} else {
    & $venvPy scripts\setup_garmin_login.py
}

# ---------------------------------------------------------------- 7. tests
Step "7/8  Tests"
& $venvPy -m pytest tests -q
if ($LASTEXITCODE -ne 0) { Warn "Some tests failed -- tell Claude before relying on the coach." }

# ---------------------------------------------------------------- 8. shortcut
Step "8/8  Desktop shortcut for the morning check-in"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Montanha Coach.lnk"
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath = "$Root\.venv\Scripts\pythonw.exe"    # pythonw: no console window
$sc.Arguments = "-m src.app"
$sc.WorkingDirectory = $Root
$sc.Description = "Montanha coach morning check-in (runs on this PC only)"
# Without this the shortcut shows the generic pythonw icon. The .ico holds a
# 256 px layer, so it stays sharp at large desktop icon sizes.
$sc.IconLocation = "$Root\src\static\favicon.ico,0"
$sc.Save()
Info "Created '$lnk'. Double-click it after you wake up."
# Earlier versions of this script registered 06:30 / Monday scheduled tasks.
foreach ($t in @("Montanha Coach Daily", "Montanha Coach Weekly")) {
    if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Info "Removed old scheduled task '$t'."
    }
}

Write-Host ""
Write-Host "== Health check" -ForegroundColor Cyan
& $venvPy -m src.doctor --send-test
Write-Host ""
Write-Host "Next: double-click 'Montanha Coach' on the desktop."
Write-Host "Back up whenever the SSD is plugged in: double-click backup.cmd"
