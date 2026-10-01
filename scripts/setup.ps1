# setup.ps1 - laedt alle benoetigten Programme portabel nach .\tools\
# Einmalig auf einem PC mit Internet ausfuehren (1_setup.cmd).
# Danach laeuft alles offline, auf jedem Windows-PC, direkt von der SSD.
#
#   tools\python\     Python embeddable + Pakete aus app\requirements.txt
#   tools\exiftool\   exiftool.exe + exiftool_files\
#   tools\ffmpeg\     ffmpeg.exe, ffprobe.exe
#
# Bereits vorhandene Tools werden uebersprungen. Neu installieren:  -Force

param([switch]$Force)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"     # Invoke-WebRequest sonst extrem langsam
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

. (Join-Path $PSScriptRoot "env.ps1")

$PythonVersion = "3.13.15"
$Downloads = Join-Path $Tools "_downloads"

function Get-File([string]$Url, [string]$Name) {
    New-Item -ItemType Directory -Force $Downloads | Out-Null
    $dest = Join-Path $Downloads $Name
    Write-Host "  lade $Url"
    # SourceForge liefert Browsern eine HTML-Zwischenseite, daher neutraler User-Agent
    Invoke-WebRequest -Uri $Url -OutFile $dest -UseBasicParsing -UserAgent "Wget/1.21"
    $bytes = [System.IO.File]::ReadAllBytes($dest)[0..1]
    if ($Name.EndsWith(".zip") -and -not ($bytes[0] -eq 0x50 -and $bytes[1] -eq 0x4B)) {
        throw "Download von $Url ist kein ZIP (evtl. Fehlerseite)."
    }
    Unblock-File $dest
    return $dest
}

function Reset-Dir([string]$Path) {
    if (Test-Path $Path) { Remove-Item -Recurse -Force $Path }
    New-Item -ItemType Directory -Force $Path | Out-Null
}

New-Item -ItemType Directory -Force $Tools, (Join-Path $Root "export"), (Join-Path $Root "output") | Out-Null

# --- Python -----------------------------------------------------------------
if ($Force -or -not (Test-Path $Python)) {
    Write-Host "`n[1/3] Python $PythonVersion (embeddable)"
    $zip = Get-File "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" "python.zip"
    Reset-Dir $PythonDir
    Expand-Archive $zip -DestinationPath $PythonDir -Force

    # site aktivieren, sonst findet Python keine per pip installierten Pakete
    $pth = Get-ChildItem $PythonDir -Filter "python3*._pth" | Select-Object -First 1
    $lines = Get-Content $pth.FullName | Where-Object { $_ -notmatch "^#?\s*import site" }
    $lines += "Lib\site-packages"
    $lines += "import site"
    Set-Content -Path $pth.FullName -Value $lines -Encoding ascii

    $getPip = Get-File "https://bootstrap.pypa.io/get-pip.py" "get-pip.py"
    & $Python $getPip --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip fehlgeschlagen" }
} else {
    Write-Host "`n[1/3] Python vorhanden - uebersprungen"
}

Write-Host "  installiere Pakete aus app\requirements.txt"
# timezonefinder gibt es nur als Quellpaket. Die isolierte Build-Umgebung von
# pip funktioniert mit dem embeddable Python nicht -> Build-Tools direkt
# installieren und ohne Isolation bauen.
& $Python -m pip install --no-warn-script-location --disable-pip-version-check setuptools wheel
& $Python -m pip install --no-warn-script-location --disable-pip-version-check `
    --no-build-isolation -r (Join-Path $Root "app\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install fehlgeschlagen" }

# --- exiftool ---------------------------------------------------------------
if ($Force -or -not (Test-Path (Join-Path $ExiftoolDir "exiftool.exe"))) {
    Write-Host "`n[2/3] exiftool"
    $ver = (Invoke-WebRequest "https://exiftool.org/ver.txt" -UseBasicParsing).Content.Trim()
    $zip = Get-File "https://downloads.sourceforge.net/project/exiftool/exiftool-${ver}_64.zip" "exiftool.zip"
    $tmp = Join-Path $Downloads "exiftool_x"
    Reset-Dir $tmp
    Expand-Archive $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem $tmp -Recurse -Filter "exiftool*.exe" | Select-Object -First 1
    Reset-Dir $ExiftoolDir
    Copy-Item (Join-Path $exe.DirectoryName "*") $ExiftoolDir -Recurse -Force
    # "exiftool(-k).exe" wartet nach jedem Aufruf auf Enter -> umbenennen
    Get-ChildItem $ExiftoolDir -Filter "exiftool*.exe" |
        Rename-Item -NewName "exiftool.exe" -Force
    Remove-Item -Recurse -Force $tmp
} else {
    Write-Host "`n[2/3] exiftool vorhanden - uebersprungen"
}

# --- ffmpeg -----------------------------------------------------------------
if ($Force -or -not (Test-Path (Join-Path $FfmpegDir "ffmpeg.exe"))) {
    Write-Host "`n[3/3] ffmpeg (gyan.dev release essentials, ~100 MB)"
    $zip = Get-File "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" "ffmpeg.zip"
    $tmp = Join-Path $Downloads "ffmpeg_x"
    Reset-Dir $tmp
    Expand-Archive $zip -DestinationPath $tmp -Force
    $bin = (Get-ChildItem $tmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1).DirectoryName
    Reset-Dir $FfmpegDir
    # Nur was das Tool braucht - ffplay.exe (Videoplayer, ~100 MB) wird nicht benoetigt
    foreach ($exe in "ffmpeg.exe", "ffprobe.exe") {
        Copy-Item (Join-Path $bin $exe) $FfmpegDir -Force
    }
    Remove-Item -Recurse -Force $tmp
} else {
    Write-Host "`n[3/3] ffmpeg vorhanden - uebersprungen"
}

Remove-Item -Recurse -Force $Downloads -ErrorAction SilentlyContinue

# --- Selbsttest ---------------------------------------------------------------
& (Join-Path $PSScriptRoot "selftest.ps1")
