@echo off
rem =====================================================================
rem  AgentPulseIncrement scheduled task launcher  (ASCII only)
rem
rem  T1 incremental pulse from docs/分层同步框架.md.
rem  Runs every 30 min at :07/:37 since 2026-11-17 (tuned down from 15 min
rem  after day-one stats: 63 of 66 rounds wrote nothing, so 15 min was
rem  pure duplicate API load). StartBoundary :07 keeps us off :00/:59
rem  where the script self-skips, and clear of T0 at 08:40/13:59.
rem
rem  Launched by the scheduled task through run_hidden.py (pythonw +
rem  CREATE_NO_WINDOW) so nothing flashes on the desktop. Do NOT point
rem  the task at this .bat directly - a console window will show.
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
