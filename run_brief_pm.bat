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
rem  2026-11-01 update: the AgentDailyBriefPM scheduled task now calls
rem  run_brief.bat directly with an explicit "--pm" argument, so the segment
rem  label is visible right in Task Scheduler instead of hiding behind this
rem  wrapper. This file stays as a MANUAL entry point:
rem      call run_brief_pm.bat   ==   run_brief.bat --pm
rem =====================================================================

call "%~dp0run_brief.bat" --pm
exit /b %errorlevel%
