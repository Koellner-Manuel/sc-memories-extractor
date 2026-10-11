@echo off
rem Main run: saves all memories into output\
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" --hash %*
pause
