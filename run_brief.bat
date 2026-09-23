@echo off
rem AgentDailyBrief scheduled task launcher (ASCII only, per dingtalk run_sync.bat lesson)
C:\Python314\python.exe C:\Users\dell\Desktop\Agent\daily_brief.py --push >> C:\Users\dell\Desktop\Agent\briefs\task.log 2>&1
