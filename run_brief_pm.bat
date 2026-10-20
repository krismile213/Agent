@echo off
rem =====================================================================
rem  AgentDailyBriefPM scheduled task launcher  (ASCII only)
rem
rem  Thin wrapper over run_brief.bat that passes --pm explicitly, so the
rem  afternoon run no longer depends on the wall clock.
rem
rem  Why this exists: daily_brief.py used to decide am/pm with
rem  now.hour >= 12. That silently mislabels the segment when
rem    a) the afternoon task fails and is retried next morning, so the
rem       content gets written as the MORNING segment instead;
rem    b) the morning task slips past 12:00, so it gets written as the
rem       AFTERNOON segment and can clobber the real afternoon run.
rem  Passing --pm makes it explicit and immune to when it actually runs.
rem
rem  Point the AgentDailyBriefPM scheduled task at THIS file. Keep
rem  run_brief.bat for the morning run (and for ad-hoc clock-based reruns).
rem =====================================================================

call "%~dp0run_brief.bat" --pm
exit /b %errorlevel%
