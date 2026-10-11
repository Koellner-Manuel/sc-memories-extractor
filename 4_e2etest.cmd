@echo off
rem End-to-end test: writes metadata in a mini export and verifies it
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\e2etest.ps1" %*
pause
