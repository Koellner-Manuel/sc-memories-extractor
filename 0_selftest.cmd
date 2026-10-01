@echo off
rem Prueft, ob die portablen Tools auf diesem PC laufen
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\selftest.ps1"
pause
