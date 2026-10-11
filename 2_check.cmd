@echo off
rem Dry run: only checks how entries map to files, writes nothing
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" --verify-only %*
pause
