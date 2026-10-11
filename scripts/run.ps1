# run.ps1 - runs the tool with the portable programs.
# Paths are relative to the folder; the drive letter does not matter.
#
#   export\   extract the Snapchat export here (with json\ and memories\)
#   output\   saved memories + _verification.csv end up here
#
# Further arguments are passed through to main.py, e.g.:
#   .\run.ps1 --verify-only
#   .\run.ps1 --hash --join-multisnap

. (Join-Path $PSScriptRoot "env.ps1")

if (-not (Test-Path $Python)) {
    Write-Host "Portable Python is missing - run 1_setup.cmd first." -ForegroundColor Red
    exit 1
}

$Export = Join-Path $Root "export"
$Output = Join-Path $Root "output"

if (-not (Get-ChildItem $Export -Recurse -Filter "memories_history.json" -ErrorAction SilentlyContinue)) {
    Write-Host "No memories_history.json found under $Export." -ForegroundColor Red
    Write-Host "Extract the Snapchat export into: $Export"
    exit 1
}

& $Python (Join-Path $Root "app\main.py") $Export --out $Output @args
exit $LASTEXITCODE
