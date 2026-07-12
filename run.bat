@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM platform-watcher launcher (runs all profiles in order).
REM   - Double-click (no args): asks how many days back (Enter = 3).
REM   - With args (e.g. Task Scheduler): runs directly, no prompt.
REM       run.bat --days 1    (daily)
REM       run.bat --days 3    (last 3 days)
REM       run.bat --no-llm    (skip LLM, rule score only)
REM Add a profile: append one more python line at the bottom.
REM (ASCII-only on purpose: cmd.exe mis-parses UTF-8 Korean in .bat files.)

REM Dedicated venv python (fallback to global python)
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM If args were passed, use them; otherwise ask for the day window.
set "ARGS=%*"
if "%ARGS%"=="" (
  set /p "DAYS=Days to look back? (Enter = 3): "
  if "!DAYS!"=="" set "DAYS=3"
  set "ARGS=--days !DAYS!"
)

echo.
echo [run] options: !ARGS!
echo.
"%PY%" platform_watcher.py --config config.json !ARGS!
"%PY%" platform_watcher.py --config config.jungmi.json !ARGS!

REM Keep the window open only for double-click (no args). Scheduler just exits.
if "%~1"=="" pause
