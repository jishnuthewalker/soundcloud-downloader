# SoundCloud Downloader: one-line installer for Windows. Press Win+R, paste this, press Enter:
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/jishnuthewalker/soundcloud-downloader/main/install.ps1 | iex"
#
# It downloads the newest setup file from the latest release, checks it against the
# published checksum, and runs it. Nothing downloaded this way triggers SmartScreen.

& {   # wrapped so a partly downloaded script can't run halfway
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

    $repo     = 'jishnuthewalker/soundcloud-downloader'
    $releases = if ($env:SCD_RELEASE_URL) { $env:SCD_RELEASE_URL } else { "https://github.com/$repo/releases/latest/download" }
    $dir      = Join-Path $env:LOCALAPPDATA 'SoundCloudDownloader'
    $bat      = Join-Path $dir 'setup.bat'

    Write-Host ""
    Write-Host "Downloading SoundCloud Downloader setup..." -ForegroundColor White
    try {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $raw = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri "$releases/latest.json").Content
        if ($raw -is [byte[]]) { $raw = [Text.Encoding]::UTF8.GetString($raw) }
        $want = ("$raw" | ConvertFrom-Json).files.'setup-windows.bat'
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri "$releases/setup-windows.bat" -OutFile "$bat.new"
        if (-not $want -or ((Get-FileHash -Algorithm SHA256 -Path "$bat.new").Hash -ne $want)) {
            Remove-Item "$bat.new" -ErrorAction SilentlyContinue
            throw "The download was damaged. Please try again."
        }
        Move-Item -Force "$bat.new" $bat
    } catch {
        Write-Host ""
        Write-Host "X Couldn't download the setup: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  Check your internet connection and try again."
        Write-Host ""
        Read-Host "Press Enter to close" | Out-Null
        return
    }

    $env:SCD_SKIP_SELF_UPDATE = '1'   # it's already the newest version
    $ErrorActionPreference = 'Continue'
    & $bat
}
