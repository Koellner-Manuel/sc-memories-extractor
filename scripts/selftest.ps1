# selftest.ps1 - checks that the portable tools in .\tools\ actually run.

. (Join-Path $PSScriptRoot "env.ps1")

Write-Host "`nSelf-test"
$ok = $true

function Test-Tool([string]$Label, [scriptblock]$Cmd) {
    try {
        # Read everything first: Select-Object -First 1 would terminate the
        # program early and report a misleading exit code
        $all = @(& $Cmd 2>&1)
        $code = $LASTEXITCODE
        $out = ($all | Select-Object -First 1) -as [string]
        if ($code -ne 0) { throw $out }
        Write-Host ("  [ ok ]   {0,-10} {1}" -f $Label, $out)
    } catch {
        Write-Host ("  [FAIL]   {0,-10} {1}" -f $Label, $_) -ForegroundColor Red
        $script:ok = $false
    }
}

Test-Tool "python"   { & $Python --version }
Test-Tool "packages" { & $Python -c "import requests, PIL, timezonefinder, zoneinfo; zoneinfo.ZoneInfo('Europe/Vienna'); print('requests, pillow, timezonefinder, tzdata ok')" }
Test-Tool "exiftool" { & (Join-Path $ExiftoolDir "exiftool.exe") -ver }
Test-Tool "ffmpeg"   { & (Join-Path $FfmpegDir "ffmpeg.exe") -hide_banner -version }
Test-Tool "ffprobe"  { & (Join-Path $FfmpegDir "ffprobe.exe") -hide_banner -version }

# Make sure the portable tools are found, not ones installed on the host
$which = & $Python -c "import shutil; print(shutil.which('exiftool'), shutil.which('ffmpeg'), sep=' | ')"
Write-Host "  Python finds: $which"
if ($which -notlike "*$Tools*") {
    Write-Host "  [WARNING] Python is not finding the tools in $Tools" -ForegroundColor Yellow
}

if ($ok) { Write-Host "`nReady." -ForegroundColor Green; exit 0 }
Write-Host "`nTools are missing - run 1_setup.cmd again." -ForegroundColor Red
exit 1
