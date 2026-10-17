@echo off
rem Double-click with the backup SSD plugged in. Copies data\ to the SSD only.
cd /d "%~dp0"
set PYTHONUTF8=1
".venv\Scripts\python.exe" scripts\backup.py %*
echo.
pause
