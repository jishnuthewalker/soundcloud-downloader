@echo off
rem ------------------------------------------------------------------
rem  SoundCloud Downloader - Windows setup (setup + app in one file)
rem  To avoid security warnings, before extracting the zip right-click
rem  it, choose Properties, tick "Unblock", OK. Then double-click this.
rem  If Windows shows "Windows protected your PC", click "More info"
rem  then "Run anyway".
rem
rem  It needs no admin rights and installs nothing system-wide. Everything
rem  (a private copy of Python, ffmpeg and scdl) goes in
rem  %LOCALAPPDATA%\SoundCloudDownloader, and a "SoundCloud Downloader"
rem  shortcut is added to the Start menu and Desktop, so you only need
rem  this file once. The app keeps itself up to date.
rem ------------------------------------------------------------------
title SoundCloud Downloader setup
set "SDL_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$t=[IO.File]::ReadAllText($env:SDL_SELF); $a=$t.IndexOf([char]10+'##PS-START'); $b=$t.IndexOf([char]10+'##PS-END'); iex $t.Substring($a, $b-$a)"
rem exit code 3 = a handled error that already paused; anything else unexpected pauses here
if errorlevel 1 if not errorlevel 3 pause
exit /b

##PS-START
# Native-command failures are checked explicitly via $LASTEXITCODE below.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'   # makes downloads much faster in Windows PowerShell
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$scdVersion = '@VERSION@'
$repo       = '@REPO@'
$pyVersion  = '@PYTHON@'
$packages   = @('scdl', 'yt-dlp', 'imageio-ffmpeg')

function Say($m)  { Write-Host ""; Write-Host $m -ForegroundColor Cyan }
function Fail($m) { Write-Host ""; Write-Host "X $m" -ForegroundColor Red; Write-Host ""; Read-Host "Press Enter to close" | Out-Null; exit 3 }
function Get-Text($uri) {
    $c = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $uri).Content
    if ($c -is [byte[]]) { $c = [Text.Encoding]::UTF8.GetString($c) }
    return "$c"
}

$appDir   = Join-Path $env:LOCALAPPDATA 'SoundCloudDownloader'
$uvDir    = Join-Path $appDir 'uv'
$uv       = Join-Path $uvDir 'uv.exe'
$venv     = Join-Path $appDir 'env'
$vpy      = Join-Path $venv 'Scripts\python.exe'
$vpyw     = Join-Path $venv 'Scripts\pythonw.exe'
$app      = Join-Path $appDir 'soundcloud_downloader.py'
$pyDir    = Join-Path $appDir 'python'
$setupBat = Join-Path $appDir 'setup.bat'
$releases = if ($env:SCD_RELEASE_URL) { $env:SCD_RELEASE_URL } else { "https://github.com/$repo/releases/latest/download" }

# Keep everything uv does inside the app folder (no PATH changes, no registry entries).
$env:UV_PYTHON_INSTALL_DIR = $pyDir
$env:UV_PYTHON_BIN_DIR = Join-Path $pyDir 'bin'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_PYTHON_INSTALL_REGISTRY = '0'
$env:UV_NO_CACHE = '1'
$env:UV_NO_CONFIG = '1'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_NO_PROGRESS = '1'

Write-Host ""
Write-Host "SoundCloud Downloader setup (version $scdVersion)" -ForegroundColor White
New-Item -ItemType Directory -Force -Path $appDir | Out-Null

# 0. If a newer version of this setup file has been published, switch to it.
if (-not $env:SCD_SKIP_SELF_UPDATE) {
    $manifest = $null
    try { $manifest = (Get-Text "$releases/latest.json") | ConvertFrom-Json } catch {}
    $newer = $false
    try { $newer = $manifest -and ([version]$manifest.version -gt [version]$scdVersion) } catch {}
    if ($newer) {
        Say "Getting the newest version ($($manifest.version))..."
        $tmpBat = "$setupBat.new"
        try {
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri "$releases/setup-windows.bat" -OutFile $tmpBat
            $want = $manifest.files.'setup-windows.bat'
            if ($want -and ((Get-FileHash -Algorithm SHA256 -Path $tmpBat).Hash -eq $want)) {
                Move-Item -Force $tmpBat $setupBat
                $env:SCD_SKIP_SELF_UPDATE = '1'
                & $setupBat
                exit 0
            }
        } catch {}
        Remove-Item $tmpBat -ErrorAction SilentlyContinue
        Say "(Couldn't download it; continuing with this version.)"
    }
}

# 1. uv: a small tool that fetches a private Python (no admin rights needed)
if (-not (Test-Path $uv)) {
    $arch = 'x86_64'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { $arch = 'aarch64' }
    $name = "uv-$arch-pc-windows-msvc.zip"
    $url  = "https://github.com/astral-sh/uv/releases/latest/download/$name"
    $tmp  = Join-Path ([IO.Path]::GetTempPath()) ("scd-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip  = Join-Path $tmp $name
    Say "Downloading setup helper..."
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri $url -OutFile $zip
        $want = ((Get-Text "$url.sha256").Trim() -split '\s+')[0]
    } catch {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        Fail "Download failed. Check your internet connection and try again."
    }
    $got = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash
    if (-not $want -or ($got -ne $want)) {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        Fail "The download was damaged. Please try again."
    }
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'x') -Force
    $found = Get-ChildItem (Join-Path $tmp 'x') -Recurse -Filter 'uv.exe' | Select-Object -First 1
    if ($found) {
        New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
        Move-Item -Force $found.FullName $uv
    }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    if (-not (Test-Path $uv)) { Fail "Couldn't install the setup helper." }
}

# 2. Private Python, newest patch release. The environment points at uv's
#    "3.12" link, so later Python upgrades apply to it automatically.
Say "Setting up Python (the first time takes about a minute)..."
& $uv python install --quiet $pyVersion
if ($LASTEXITCODE -eq 0) { & $uv python upgrade --quiet $pyVersion }
if ($LASTEXITCODE -ne 0) { Fail "Couldn't set up Python. Check your internet connection and try again." }
$venvOk = $false
$cfg = Join-Path $venv 'pyvenv.cfg'
if ((Test-Path $vpy) -and (Test-Path $cfg) -and ((Get-Content -Raw $cfg).Contains((Join-Path $pyDir "cpython-$pyVersion-")))) {
    & $vpy -c 'import tkinter' 2>$null
    $venvOk = ($LASTEXITCODE -eq 0)
}
if (-not $venvOk) {
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $uv venv --quiet --python $pyVersion $venv
    if ($LASTEXITCODE -ne 0) { Fail "Couldn't set up Python." }
    & $vpy -c 'import tkinter' 2>$null
    if ($LASTEXITCODE -ne 0) { Fail "Python was installed but its window toolkit doesn't work." }
}

# 3. Newest scdl, yt-dlp, ffmpeg and everything they depend on
Say "Installing the newest downloader and ffmpeg..."
& $uv pip install --quiet --upgrade --python $vpy @packages
if ($LASTEXITCODE -ne 0) { Fail "Couldn't install the downloader. Check your internet connection and try again." }

# 4. Unpack the app (everything after the ##PY-START line), keep a copy of this
#    setup file for repairs, and add shortcuts
$t = [IO.File]::ReadAllText($env:SDL_SELF)
$marker = [char]10 + '##PY-START'
$i = $t.IndexOf($marker)
if ($i -lt 0) { Fail "This setup file looks damaged. Download it again." }
$code = $t.Substring($t.IndexOf([char]10, $i + 1) + 1)
[IO.File]::WriteAllText("$app.new", $code, (New-Object System.Text.UTF8Encoding $false))
Move-Item -Force "$app.new" $app
if ((Resolve-Path $env:SDL_SELF).Path -ne $setupBat) { Copy-Item -Force $env:SDL_SELF $setupBat }

& $vpy $app --setup | Out-Null
$icon = Join-Path $appDir 'icon.ico'
$made = @()
try {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
        if (-not $dir) { continue }
        $lnk = $shell.CreateShortcut((Join-Path $dir 'SoundCloud Downloader.lnk'))
        $lnk.TargetPath = $vpyw
        $lnk.Arguments = '"' + $app + '"'
        $lnk.WorkingDirectory = $appDir
        $lnk.Description = 'Download SoundCloud tracks and playlists as MP3'
        if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
        $lnk.Save()
        $made += $dir
    }
} catch {}

Start-Process -FilePath $vpyw -ArgumentList ('"' + $app + '"') -WorkingDirectory $appDir
Write-Host ""
Write-Host "All set! SoundCloud Downloader is opening." -ForegroundColor Green
if ($made.Count -gt 0) {
    Write-Host ""
    Write-Host "From now on, open it from the 'SoundCloud Downloader' icon on your Desktop"
    Write-Host "or in the Start menu. It updates itself, so you can delete this setup file."
}
Write-Host ""
Start-Sleep -Seconds 8
exit 0
##PS-END
##PY-START
