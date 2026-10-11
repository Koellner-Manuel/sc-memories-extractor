# e2etest.ps1 - end-to-end test using the portable tools.
#
# Builds a mini export from export\ in a temp folder, runs main.py on it and
# verifies the written metadata with exiftool and ffprobe.
# export\ and output\ are only read, never modified.
#
#   .\e2etest.ps1            test against export\
#   .\e2etest.ps1 --keep     keep the temp folder for inspection

. (Join-Path $PSScriptRoot "env.ps1")

if (-not (Test-Path $Python)) {
    Write-Host "Portable Python is missing - run 1_setup.cmd first." -ForegroundColor Red
    exit 1
}

$Export = Join-Path $Root "export"
if (-not (Get-ChildItem $Export -Recurse -Filter "memories_history.json" -ErrorAction SilentlyContinue)) {
    Write-Host "No memories_history.json found under $Export." -ForegroundColor Red
    exit 1
}

& $Python (Join-Path $Root "app\e2etest.py") --export $Export @args
exit $LASTEXITCODE
