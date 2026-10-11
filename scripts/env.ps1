# env.ps1 - sets up the environment for the portable tools.
# Dot-sourced by the other scripts:  . .\scripts\env.ps1
# Changes PATH for the current session only; nothing is set permanently.

$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "tools"

$PythonDir   = Join-Path $Tools "python"
$ExiftoolDir = Join-Path $Tools "exiftool"
$FfmpegDir   = Join-Path $Tools "ffmpeg"

$env:PATH = "$PythonDir;$ExiftoolDir;$FfmpegDir;$env:PATH"

# Never pull packages from a Python that happens to be installed on the host
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Python = Join-Path $PythonDir "python.exe"
