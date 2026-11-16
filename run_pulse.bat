@echo off
rem =====================================================================
rem  AgentPulseIncrement scheduled task launcher  (ASCII only)
rem
rem  T1 incremental pulse from docs/分层同步框架.md.
rem  Runs every 15 min at :07/:22/:37/:52 (task StartBoundary controls the
rem  offset so we never land on :00/:59 where the script self-skips, and
rem  stay clear of T0 at 08:40/13:59).
rem
rem  The script defaults to DRY; --live is passed here so the scheduled
rem  task actually writes the archive DB (never xlsx). LIVE is safe:
rem  approval_sync.archive_connect uses WAL + timeout=30, same as T0.
rem
rem  Uses the user's own Python 3.14 + user-site deps (requests/openpyxl),
rem  matching run_brief.bat. Logs append to briefs/pulse.log.
rem =====================================================================

setlocal
set "APP_DIR=C:\Users\dell\Desktop\Agent"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "PY=C:\Python314\python.exe"
set "PYPATH=C:\Users\dell\AppData\Roaming\Python\Python314\site-packages"
set "LOG=%APP_DIR%\briefs\pulse.log"

echo ===== %DATE% %TIME% ===== >> "%LOG%"
set "PYTHONPATH=%PYPATH%"
"%PY%" "%APP_DIR%\scripts\incremental_pulse.py" --live --page-gap 0.4 --detail-gap 0.3 >> "%LOG%" 2>&1
exit /b %errorlevel%
