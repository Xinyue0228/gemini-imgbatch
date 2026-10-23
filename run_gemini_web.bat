@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   run_gemini_web - Gemini web image batch (no API key)
echo ============================================================
echo.
echo   Uses your gemini.google.com account cookies to batch-generate
echo   images through the web backend. No API key, no CLI OAuth.
echo.

rem ---------- 1. prompt file ----------
set "INFILE=%~1"
if "!INFILE!"=="" (
    set /p "INFILE=Prompt file path (drag a file here or type path): "
)
if not exist "!INFILE!" (
    echo.
    echo     [ERROR] File not found: !INFILE!
    echo.
    pause
    exit /b 1
)
echo     Prompt file: !INFILE!
echo.

rem ---------- 2. launch (uses local .venv) ----------
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_gemini_web.py --input "!INFILE!"
) else (
    echo     [ERROR] .venv not found. Run setup_web.bat first.
    pause
    exit /b 1
)

echo.
echo Done. Progress saved; next run resumes automatically.
echo.
pause
