@echo off
rem GovKG full proposition extraction supervisor launcher (Task Scheduler entry).
rem Started by scheduled task GovKGExtractFull (ONSTART + manual /run).
rem All child output is appended to kg\import\checkpoints\extract_full_run.log
rem by supervise_extract_full.sh itself.
cd /d E:\Graudate\gov-affair-kg-qa\repo
"C:\Program Files\Git\bin\bash.exe" kg/import/supervise_extract_full.sh
