@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo   Setup - create .venv and install dependencies
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo     [ERROR] Failed to create venv. Is Python installed?
        pause
        exit /b 1
    )
) else (
    echo [1/2] .venv already exists, skip.
)

echo [2/2] Installing gemini_webapi + Pillow...
".venv\Scripts\python.exe" -m pip install -U gemini_webapi Pillow
echo.
echo Setup done. Next: get cookies, then run run_gemini_web.bat.
echo.
pause
