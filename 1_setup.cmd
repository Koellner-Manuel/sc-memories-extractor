@echo off
rem One-time, needs internet: downloads Python, exiftool and ffmpeg into tools\
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
pause
