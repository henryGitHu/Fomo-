@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
if not exist ".venv\Scripts\python.exe" (
    echo The scanner isn't set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)
echo Sending a test message to your phone...
".venv\Scripts\python.exe" -m scanner.main --test-alert
echo.
pause
