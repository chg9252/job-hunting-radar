@echo off
REM 채용 플랫폼 워처 실행 래퍼 (Windows 작업 스케줄러에서 이 파일을 호출).
REM 모든 프로필을 순서대로 실행. 추가 인자는 각 실행에 그대로 전달.
REM   run.bat            → config 기본 신규 윈도우(7일)
REM   run.bat --days 1   → 매일 돌릴 때(하루 내 신규만)
REM   run.bat --days 3   → 3일 만에 돌릴 때(3일 내 신규)
REM   run.bat --no-llm   → LLM 생략(규칙 점수만)
REM 프로필 추가 시 아래에 한 줄만 더 붙이면 됨.
cd /d "%~dp0"
REM 전용 venv 파이썬 사용(crawl4ai 등 격리). venv 없으면 전역 python으로 폴백.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" platform_watcher.py --config config.json %*
"%PY%" platform_watcher.py --config config.jungmi.json %*
