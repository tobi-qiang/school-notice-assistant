@echo off
setlocal enabledelayedexpansion

REM School Notice Assistant - first-time install.
REM Usage: double-click from the project root, or run from a shell.

cd /d "%~dp0"

echo.
echo =====================================================
echo   School Notice Assistant - first-time install
echo =====================================================
echo.

REM ---- Step 1: locate a usable Python (>=3.13) ----
set "PY_CMD="
py -3.13 --version >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3.13"
    goto :have_python
)

python --version >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,13) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)

:have_python
if "%PY_CMD%"=="" goto :no_python

echo Using Python:  %PY_CMD%
%PY_CMD% --version
echo.

REM ---- Step 2: create .venv if missing ----
if exist ".venv\Scripts\python.exe" goto :venv_ready
echo Creating virtual environment in .venv ...
%PY_CMD% -m venv .venv
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create .venv.
    echo         Re-run from the project root after fixing the cause above.
    echo.
    pause
    exit /b 1
)

:venv_ready
REM ---- Step 3: install project + dev deps ----
echo Upgrading pip ...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :pip_failed

echo Installing project + dev dependencies (first run may take a minute) ...
call ".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 goto :pip_failed

echo.
echo =====================================================
echo   Install complete.
echo =====================================================
echo.
echo   1) Double-click "启动学校公告助手.vbs" to launch the app.
echo   2) For the JWC source, install Google Chrome on this machine.
echo   3) All runtime state (database, AI keys, JWC session)
echo      lives under the local "data\" directory and is created
echo      automatically on first run.
echo.
pause
exit /b 0

:pip_failed
echo.
echo [ERROR] pip install failed.
echo         If the cause is a network / proxy issue, fix it and re-run install.bat.
echo.
pause
exit /b 1

:no_python
echo.
echo [ERROR] Python 3.13 or newer was not found on PATH.
echo.
echo Install Python 3.13 from:
echo     https://www.python.org/downloads/windows/
echo.
echo On the first installer screen, tick at least:
echo     [x] Install Python launcher (py.exe)
echo     [x] Add python.exe to PATH   (optional but convenient)
echo Then re-run install.bat.
echo.
pause
exit /b 1