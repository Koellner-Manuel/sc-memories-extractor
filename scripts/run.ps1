# run.ps1 - startet das Tool mit den portablen Programmen.
# Pfade sind relativ zum SSD-Ordner, der Laufwerksbuchstabe ist egal.
#
#   export\   hierhin den entpackten Snapchat-Export (mit json\ und memories\)
#   output\   hier landen die gesicherten Memories + _verification.csv
#
# Weitere Parameter werden an main.py durchgereicht, z.B.:
#   .\run.ps1 --verify-only
#   .\run.ps1 --hash --join-multisnap

. (Join-Path $PSScriptRoot "env.ps1")

if (-not (Test-Path $Python)) {
    Write-Host "Portables Python fehlt - zuerst 1_setup.cmd ausfuehren." -ForegroundColor Red
    exit 1
}

$Export = Join-Path $Root "export"
$Output = Join-Path $Root "output"

if (-not (Get-ChildItem $Export -Recurse -Filter "memories_history.json" -ErrorAction SilentlyContinue)) {
    Write-Host "Keine memories_history.json unter $Export gefunden." -ForegroundColor Red
    Write-Host "Snapchat-Export entpacken nach: $Export"
    exit 1
}

& $Python (Join-Path $Root "app\main.py") $Export --out $Output @args
exit $LASTEXITCODE
