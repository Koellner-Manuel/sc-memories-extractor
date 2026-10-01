# shell.ps1 - PowerShell-Fenster mit aktiven portablen Tools (fuer eigene Befehle).
. (Join-Path $PSScriptRoot "env.ps1")
Set-Location $Root
& (Join-Path $PSScriptRoot "selftest.ps1")
Write-Host "`nexiftool, ffmpeg, ffprobe und python sind in diesem Fenster verfuegbar."
