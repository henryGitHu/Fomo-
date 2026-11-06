@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    echo The scanner isn't set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
rem What the bot has learned. Add --retrain to re-analyse every trade right now.
".venv\Scripts\python.exe" -m scanner.learning %*
echo.
pause
