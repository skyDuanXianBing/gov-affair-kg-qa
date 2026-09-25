@echo off
rem Boot-time entry for GovKG full proposition extraction (called by Startup VBS).
rem Skips instantly once the supervisor has logged EXTRACT FULL COMPLETE,
rem so a finished run costs nothing at logon.
cd /d E:\Graudate\gov-affair-kg-qa\repo
findstr /C:"EXTRACT FULL COMPLETE" kg\import\checkpoints\extract_full_run.log >nul 2>&1
if %errorlevel%==0 exit /b 0
"C:\Program Files\Git\bin\bash.exe" kg/import/supervise_extract_full.sh
