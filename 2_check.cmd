@echo off
rem Trockenlauf: prueft nur die Zuordnung JSON zu Dateien, schreibt nichts
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" --verify-only %*
pause
