@echo off
setlocal
cd /d "%~dp0"

rem The marker is written only after every install step succeeds, so a failed
rem install is retried on the next run instead of leaving a venv without Playwright.
if not exist .venv\.autocheckin-ready (
  if not exist .venv\Scripts\python.exe (
    py -m venv .venv || exit /b 1
  )
  .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
  .venv\Scripts\python.exe -m playwright install chromium || exit /b 1
  type nul > .venv\.autocheckin-ready
)

.venv\Scripts\python.exe agentrouter_checkin.py %*
