# shell.ps1 - PowerShell window with the portable tools active (for your own commands).
. (Join-Path $PSScriptRoot "env.ps1")
Set-Location $Root
& (Join-Path $PSScriptRoot "selftest.ps1")
Write-Host "`nexiftool, ffmpeg, ffprobe and python are available in this window."
