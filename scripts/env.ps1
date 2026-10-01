# env.ps1 - setzt die Umgebung fuer die portablen Tools.
# Wird von run.ps1 / shell.cmd per Dot-Sourcing geladen:  . .\scripts\env.ps1
# Aendert PATH nur fuer die aktuelle Sitzung, nichts wird am PC dauerhaft gesetzt.

$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "tools"

$PythonDir   = Join-Path $Tools "python"
$ExiftoolDir = Join-Path $Tools "exiftool"
$FfmpegDir   = Join-Path $Tools "ffmpeg"

$env:PATH = "$PythonDir;$ExiftoolDir;$FfmpegDir;$env:PATH"

# Keine Pakete aus einer evtl. installierten Python-Version des Host-PCs ziehen
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Python = Join-Path $PythonDir "python.exe"
