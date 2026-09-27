@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
title Crypto Momentum Scanner

if not exist ".venv\Scripts\python.exe" (
    echo The scanner isn't set up yet. Double-click setup.bat first.
    pause
    exit /b 1
)

rem Any extra words after run.bat are passed along, e.g.:
rem    run.bat --once       one scan, then stop
rem    run.bat --dry-run    print alerts here instead of sending them
".venv\Scripts\python.exe" -m scanner.main %*

echo.
echo Scanner has stopped. (Details are in logs\scanner.log)
pause
