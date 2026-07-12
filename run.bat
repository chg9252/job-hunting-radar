@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 채용 플랫폼 워처 실행 래퍼 (모든 프로필 순차 실행).
REM   - 더블클릭(인자 없음): 며칠치 볼지 물어봄(그냥 Enter=3).
REM   - 인자 전달(작업 스케줄러 등): 묻지 않고 그대로 실행.
REM     예) run.bat --days 1   (매일)
REM         run.bat --days 3   (3일치)
REM         run.bat --no-llm   (LLM 생략)
REM 프로필 추가: 맨 아래에 python 실행 줄 하나만 더 붙이면 됨.

REM 전용 venv 파이썬(없으면 전역 python)
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM 인자가 있으면 그대로 사용, 없으면(더블클릭) 며칠치인지 물어봄
set "ARGS=%*"
if "%ARGS%"=="" (
  set /p "DAYS=최근 며칠치 신규 공고를 볼까요? (그냥 Enter = 3): "
  if "!DAYS!"=="" set "DAYS=3"
  set "ARGS=--days !DAYS!"
)

echo.
echo [실행] 옵션: !ARGS!
echo.
"%PY%" platform_watcher.py --config config.json !ARGS!
"%PY%" platform_watcher.py --config config.jungmi.json !ARGS!

REM 더블클릭(인자 없이)일 때만 창을 열어둠(결과 확인용). 스케줄러 실행은 그냥 종료.
if "%~1"=="" pause
