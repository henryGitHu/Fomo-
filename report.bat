@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    echo The scanner isn't set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
rem Extra words are passed along, e.g.:  report.bat --days 7   or   report.bat --live-only
".venv\Scripts\python.exe" -m scanner.report %*
echo.
pause
