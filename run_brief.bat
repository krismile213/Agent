@echo off
rem =====================================================================
rem  AgentDailyBrief scheduled task launcher
rem  (ASCII only, per dingtalk run_sync.bat lesson: non-ASCII in a .bat
rem   gets mangled by the active console codepage)
rem
rem  2026-10-15 rewrite.
rem  Why: the previous version hard-coded C:\Python314\python.exe, which has
rem  NO dependencies available (not even requests). Running
rem      C:\Python314\python.exe daily_brief.py --push
rem  would die immediately on ModuleNotFoundError: pandas, yet task.log
rem  shows successful runs -> the live scheduled task is NOT using this
rem  hard-coded interpreter. It resolves to some other python on PATH.
rem
rem  This launcher now probes candidates and picks the first one that can
rem  actually import pandas+requests, so the outcome no longer depends on PATH.
rem  Candidates in priority order:
rem    1) the user's own Python 3.14  (has pandas/openpyxl/numpy/requests)
rem    2) WorkBuddy isolated venv       (full deps incl. fastapi/uvicorn)
rem =====================================================================

setlocal

rem App dir = this script's dir, so it works through the C:/D: junction too.
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "LOG=%APP_DIR%\briefs\task.log"

rem Keep the whole log in one encoding (daily_brief reconfigures stdout+stderr
rem to UTF-8 as well; these two vars cover import-time prints before that).
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PY="

call :probe "C:\Users\dell\AppData\Local\Python\bin\python.exe"
if not defined PY call :probe "C:\Users\dell\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"

if not defined PY (
  echo [launcher] FATAL: no interpreter with pandas found - brief NOT run.>>"%LOG%"
  echo [launcher]   tried: C:\Users\dell\AppData\Local\Python\bin\python.exe>>"%LOG%"
  echo [launcher]   tried: C:\Users\dell\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe.>>"%LOG%"
  exit /b 2
)

echo [launcher] interpreter: %PY%>>"%LOG%"
"%PY%" "%APP_DIR%\daily_brief.py" --push >>"%LOG%" 2>&1
set "RC=%errorlevel%"
echo [launcher] daily_brief exit=%RC%>>"%LOG%"
exit /b %RC%

rem ---------- probe: set PY to %1 if it exists and can import deps ----------
rem Check pandas AND requests: agentcore/dingtalk_push need requests at import
rem time, so a pandas-only interpreter would pass a pandas-only probe and then
rem die on ModuleNotFoundError anyway (the exact failure this launcher fixes).
:probe
if "%~1"=="" exit /b 0
if not exist "%~1" exit /b 0
"%~1" -c "import pandas, requests" >nul 2>&1
if errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
