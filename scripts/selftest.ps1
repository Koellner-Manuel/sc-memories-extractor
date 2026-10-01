# selftest.ps1 - prueft, ob alle portablen Tools aus .\tools\ funktionieren.

. (Join-Path $PSScriptRoot "env.ps1")

Write-Host "`nSelbsttest"
$ok = $true

function Test-Tool([string]$Label, [scriptblock]$Cmd) {
    try {
        # Erst komplett einlesen: Select-Object -First 1 wuerde das Programm
        # abbrechen und einen falschen Exitcode liefern
        $all = @(& $Cmd 2>&1)
        $code = $LASTEXITCODE
        $out = ($all | Select-Object -First 1) -as [string]
        if ($code -ne 0) { throw $out }
        Write-Host ("  [ok]     {0,-10} {1}" -f $Label, $out)
    } catch {
        Write-Host ("  [FEHLER] {0,-10} {1}" -f $Label, $_) -ForegroundColor Red
        $script:ok = $false
    }
}

Test-Tool "python"   { & $Python --version }
Test-Tool "pakete"   { & $Python -c "import requests, PIL, timezonefinder, zoneinfo; zoneinfo.ZoneInfo('Europe/Vienna'); print('requests, pillow, timezonefinder, tzdata ok')" }
Test-Tool "exiftool" { & (Join-Path $ExiftoolDir "exiftool.exe") -ver }
Test-Tool "ffmpeg"   { & (Join-Path $FfmpegDir "ffmpeg.exe") -hide_banner -version }
Test-Tool "ffprobe"  { & (Join-Path $FfmpegDir "ffprobe.exe") -hide_banner -version }

# Sicherstellen, dass wirklich die portablen Tools gefunden werden, nicht die des PCs
$which = & $Python -c "import shutil; print(shutil.which('exiftool'), shutil.which('ffmpeg'), sep=' | ')"
Write-Host "  Python findet: $which"
if ($which -notlike "*$Tools*") {
    Write-Host "  [WARNUNG] Python findet nicht die Tools aus $Tools" -ForegroundColor Yellow
}

if ($ok) { Write-Host "`nAlles bereit." -ForegroundColor Green; exit 0 }
Write-Host "`nEs fehlen Tools - 1_setup.cmd erneut ausfuehren." -ForegroundColor Red
exit 1
