<#
.SYNOPSIS
    Stages a self-contained Kirinuki app folder for the Inno Setup installer.

.DESCRIPTION
    Downloads a portable Python + Node.js runtime, installs Python/npm dependencies
    into them, copies the repo source, and downloads ffmpeg/ffprobe/yt-dlp — producing
    dist/Kirinuki/app/ which kirinuki.iss then wraps into KirinukiSetup.exe.

    Must run on Windows (PowerShell 5.1+ or pwsh). Intended to run either locally on a
    Windows machine, or via .github/workflows/build-windows-installer.yml on
    windows-latest — this repo's dev environment cannot execute or verify this script.

.NOTES
    Pinned versions/URLs below are believed-current as of authoring; re-verify them
    (and consider mirroring the downloads) before relying on this for a real release.
#>

$ErrorActionPreference = "Stop"

$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistRoot   = Join-Path $RepoRoot "dist"
$StageRoot  = Join-Path $DistRoot "Kirinuki"
$AppDir     = Join-Path $StageRoot "app"
$DownloadDir = Join-Path $DistRoot "_downloads"

$PythonVersion = "3.12.7"
$PythonZipUrl  = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl     = "https://bootstrap.pypa.io/get-pip.py"

$NodeVersion   = "20.17.0"
$NodeZipUrl    = "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip"

$FfmpegZipUrl  = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$YtDlpExeUrl   = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"

New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
if (Test-Path $StageRoot) { Remove-Item -Recurse -Force $StageRoot }
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null

function Download-File($Url, $OutFile) {
    if (Test-Path $OutFile) { Write-Host "  (cached) $OutFile"; return }
    Write-Host "  downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile
}

# ── 1. Embeddable Python + pip + requirements.txt ────────────────────────────
Write-Host "== Python =="
$pythonDir = Join-Path $AppDir "python"
$pythonZip = Join-Path $DownloadDir "python-embed.zip"
Download-File $PythonZipUrl $pythonZip
Expand-Archive -Path $pythonZip -DestinationPath $pythonDir -Force

# Embeddable Python disables `site` (and thus site-packages) by default via the
# ._pth file — re-enable it so pip-installed packages are importable.
$pthFile = Get-ChildItem -Path $pythonDir -Filter "python3*._pth" | Select-Object -First 1
(Get-Content $pthFile.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pthFile.FullName
Add-Content $pthFile.FullName "`nLib\site-packages"

$getPip = Join-Path $DownloadDir "get-pip.py"
Download-File $GetPipUrl $getPip
& "$pythonDir\python.exe" $getPip --no-warn-script-location

& "$pythonDir\python.exe" -m pip install --no-warn-script-location -r "$RepoRoot\requirements.txt"

# ── 2. Portable Node.js + Remotion npm install ────────────────────────────────
Write-Host "== Node.js =="
$nodeZip = Join-Path $DownloadDir "node.zip"
Download-File $NodeZipUrl $nodeZip
$nodeExtractTmp = Join-Path $DownloadDir "node_extract"
if (Test-Path $nodeExtractTmp) { Remove-Item -Recurse -Force $nodeExtractTmp }
Expand-Archive -Path $nodeZip -DestinationPath $nodeExtractTmp -Force
$nodeInner = Get-ChildItem -Path $nodeExtractTmp -Directory | Select-Object -First 1
Move-Item $nodeInner.FullName (Join-Path $AppDir "node")

# ── 3. Copy repo source ───────────────────────────────────────────────────────
Write-Host "== App source =="
foreach ($item in @("web", "audio-chunking", "remotion", "suggest_clips.py", "requirements.txt")) {
    Copy-Item -Path (Join-Path $RepoRoot $item) -Destination (Join-Path $AppDir $item) -Recurse -Force
}
# Packaged app data lives under %LOCALAPPDATA%\Kirinuki via config.get_data_dir();
# don't ship dev-machine transcriptions/downloads/renders.
foreach ($devOnly in @("web\transcriptions", "downloads", "transcriptions")) {
    $p = Join-Path $AppDir $devOnly
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Copy-Item -Path (Join-Path $PSScriptRoot "launcher.py") -Destination (Join-Path $AppDir "launcher.py") -Force

$nodeExe = Join-Path $AppDir "node\node.exe"
$npmCmd  = Join-Path $AppDir "node\npm.cmd"
Push-Location (Join-Path $AppDir "remotion")
try {
    & $npmCmd install
    # Pre-download Remotion's headless Chrome build so the packaged app doesn't need
    # network access on first render. Verify this exact command against the Remotion
    # CLI version pinned in remotion/package.json — `npx remotion browser --help`.
    & (Join-Path $AppDir "node\npx.cmd") remotion browser ensure
} finally {
    Pop-Location
}

# ── 4. ffmpeg / ffprobe / yt-dlp ──────────────────────────────────────────────
Write-Host "== ffmpeg / yt-dlp =="
$binDir = Join-Path $AppDir "bin"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null

$ffmpegZip = Join-Path $DownloadDir "ffmpeg.zip"
Download-File $FfmpegZipUrl $ffmpegZip
$ffmpegExtractTmp = Join-Path $DownloadDir "ffmpeg_extract"
if (Test-Path $ffmpegExtractTmp) { Remove-Item -Recurse -Force $ffmpegExtractTmp }
Expand-Archive -Path $ffmpegZip -DestinationPath $ffmpegExtractTmp -Force
$ffmpegBinDir = Get-ChildItem -Path $ffmpegExtractTmp -Recurse -Filter "ffmpeg.exe" | Select-Object -First 1 | ForEach-Object { $_.DirectoryName }
Copy-Item (Join-Path $ffmpegBinDir "ffmpeg.exe") $binDir -Force
Copy-Item (Join-Path $ffmpegBinDir "ffprobe.exe") $binDir -Force

$ytDlpExe = Join-Path $binDir "yt-dlp.exe"
Download-File $YtDlpExeUrl $ytDlpExe

Write-Host "`nStaged app folder: $AppDir"
Write-Host "Next: compile packaging\windows\kirinuki.iss with Inno Setup to produce dist\KirinukiSetup.exe"
