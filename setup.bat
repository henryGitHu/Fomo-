@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   Crypto Momentum Scanner - one-time setup
echo ============================================================
echo.

rem --- Find Python 3.11 or newer (prefer the "py" launcher) ---
set "PY="
where py >nul 2>nul && (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>nul && (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
    )
)
if not defined PY (
    echo Python 3.11 or newer was not found.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during install, then run setup.bat again.
    pause
    exit /b 1
)
echo Using: %PY%
%PY% --version

rem --- Create the virtual environment (a private copy of Python for this tool) ---
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo.
echo Installing required packages (this can take a minute) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Package install failed. Check your internet connection and try again.
    pause
    exit /b 1
)

rem --- Create .env for secrets if it doesn't exist yet ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env ^(secrets file^). Phase 1 doesn't need anything in it.
)

echo.
echo Checking your settings file ...
".venv\Scripts\python.exe" -c "from scanner.config import load_config; c = load_config(); print('config.yaml OK - chains:', ', '.join(x.name for x in c.enabled_chains))"
if errorlevel 1 (
    echo Fix the problem in config.yaml described above, then run setup.bat again.
    pause
    exit /b 1
)

echo.
echo Setup complete. Double-click run.bat to start the scanner.
pause
