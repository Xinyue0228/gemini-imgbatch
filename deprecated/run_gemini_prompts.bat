@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   run_gemini_prompts - Gemini image batch (no API key)
echo ============================================================
echo.
echo   Reads your own prompt file (.csv / .txt / .jsonl),
echo   generates images with your Gemini Pro account,
echo   and remembers progress for resume.
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

rem ---------- 2. launch (Python asks for count + folder) ----------
python run_gemini_prompts.py --input "!INFILE!"

echo.
echo Done. Progress is saved automatically; next run resumes where it left off.
echo.
pause
