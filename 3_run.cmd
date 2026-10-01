@echo off
rem Hauptlauf: sichert alle Memories nach output\
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" --hash %*
pause
