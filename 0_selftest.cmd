@echo off
rem Check that the portable tools run on this PC
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\selftest.ps1"
pause
