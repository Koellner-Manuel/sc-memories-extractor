@echo off
rem Einmalig mit Internet: laedt Python, exiftool und ffmpeg nach tools\
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
pause
