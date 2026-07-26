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

# PowerShell does NOT turn a failing native command's exit code into a terminating
# error by itself — `$ErrorActionPreference = "Stop"` only covers cmdlets. Every `&`
# invocation of pip/npm/npx below must be wrapped in this, or a failed install (e.g.
# a dependency that didn't land in site-packages/node_modules) silently reports the
# whole step as successful while shipping a broken app.
function Invoke-Checked {
    param(
        [Parameter(Mandatory)] [string] $Description,
        [Parameter(Mandatory)] [scriptblock] $ScriptBlock
    )
    Write-Host "  -> $Description"
    & $ScriptBlock
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

$RepoRoot   = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$DistRoot   = Join-Path $RepoRoot "dist"
$StageRoot  = Join-Path $DistRoot "Kirinuki"
$AppDir     = Join-Path $StageRoot "app"
$DownloadDir = Join-Path $DistRoot "_downloads"

$PythonVersion = "3.12.7"
$PythonZipUrl  = "https://www.nuget.org/api/v2/package/python/$PythonVersion"
$GetPipUrl     = "https://bootstrap.pypa.io/get-pip.py"

$NodeVersion   = "24.18.0"
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

# ── 1. Full Python Runtime + pip + requirements.txt ───────────────────────────
Write-Host "== Python (Full Runtime) =="
$pythonDir = Join-Path $AppDir "python"
$pythonZip = Join-Path $DownloadDir "python-full.zip"
Download-File $PythonZipUrl $pythonZip

$pythonExtractTmp = Join-Path $DownloadDir "python_extract"
if (Test-Path $pythonExtractTmp) { Remove-Item -Recurse -Force $pythonExtractTmp }
Expand-Archive -Path $pythonZip -DestinationPath $pythonExtractTmp -Force

# Extract tools contents to app/python
New-Item -ItemType Directory -Force -Path $pythonDir | Out-Null
Copy-Item (Join-Path $pythonExtractTmp "tools\*") $pythonDir -Recurse -Force

# Ensure site-packages path is set if ._pth file exists
Get-ChildItem -Path $pythonDir -Filter "python3*._pth" -ErrorAction SilentlyContinue | ForEach-Object {
    (Get-Content $_.FullName) -replace '^#\s*import site', 'import site' | Set-Content $_.FullName
    Add-Content $_.FullName "`nLib\site-packages"
}

$getPip = Join-Path $DownloadDir "get-pip.py"
Download-File $GetPipUrl $getPip
Invoke-Checked "bootstrap pip" { & "$pythonDir\python.exe" $getPip --no-warn-script-location }


Invoke-Checked "pip install -r requirements.txt" {
    & "$pythonDir\python.exe" -m pip install --no-warn-script-location --no-cache-dir -r "$RepoRoot\requirements.txt"
}

# Cleanup __pycache__ and unused compilations to save space
Write-Host "== Cleaning up Python dependencies =="
if (Test-Path (Join-Path $pythonDir "Lib\site-packages")) {
    Get-ChildItem -Path (Join-Path $pythonDir "Lib\site-packages") -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path (Join-Path $pythonDir "Lib\site-packages") -Recurse -Filter "*.pyc" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
}

# pip succeeding is not proof the packages actually landed where Python will look for
# them (embeddable-Python site-packages wiring is easy to get subtly wrong) — verify.
$uvicornCheck = Join-Path $pythonDir "Lib\site-packages\uvicorn"
if (-not (Test-Path $uvicornCheck)) {
    throw "uvicorn missing from $uvicornCheck after pip install — site-packages wiring or the install itself failed"
}

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
# don't ship dev-machine transcriptions/downloads/renders, and clean up temporary dev assets.
foreach ($devOnly in @("web\transcriptions", "downloads", "transcriptions", "clips", "web\__pycache__", "audio-chunking\__pycache__", "remotion\node_modules", "remotion\.next", "remotion\out", "remotion\build")) {
    $p = Join-Path $AppDir $devOnly
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Copy-Item -Path (Join-Path $PSScriptRoot "launcher.py") -Destination (Join-Path $AppDir "launcher.py") -Force
Copy-Item -Path (Join-Path $PSScriptRoot "icon.ico") -Destination (Join-Path $AppDir "icon.ico") -Force

$npmCmd = Join-Path $AppDir "node\npm.cmd"
$npxCmd = Join-Path $AppDir "node\npx.cmd"
Push-Location (Join-Path $AppDir "remotion")
try {
    Invoke-Checked "npm install (remotion)" { & $npmCmd install --omit=dev }

    $remotionCheck = Join-Path $AppDir "remotion\node_modules\remotion"
    if (-not (Test-Path $remotionCheck)) {
        throw "remotion missing from $remotionCheck after npm install"
    }


    # Headless Chromium (approx. 150MB-250MB) is omitted from the installer for size reduction.
    # Remotion will automatically download it on the first video render if internet is available.
    # & $npxCmd remotion browser ensure
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

# ── 5. Bundled default API keys ───────────────────────────────────────────────
# Optional: bake GEMINI_API_KEY / ELEVENLABS_API_KEY in as defaults so end users don't
# need to configure anything (see .github/workflows/build-windows-installer.yml, which
# sources these from GitHub Actions secrets — never from a checked-in file). Read by
# config.py's load_settings() as the lowest-priority source; the settings screen still
# overrides it. Silently produces no file (i.e. no defaults) if the env vars are unset,
# e.g. for a local build.ps1 run without them exported.
$bundledDefaults = @{}
if ($env:GEMINI_API_KEY)     { $bundledDefaults["GEMINI_API_KEY"] = $env:GEMINI_API_KEY }
if ($env:ELEVENLABS_API_KEY) { $bundledDefaults["ELEVENLABS_API_KEY"] = $env:ELEVENLABS_API_KEY }
if ($bundledDefaults.Count -gt 0) {
    Write-Host "== Bundling default API keys ($($bundledDefaults.Count) key(s)) =="
    $bundledDefaults | ConvertTo-Json | Set-Content (Join-Path $AppDir "web\default_config.json") -Encoding utf8
}

Write-Host "`nStaged app folder: $AppDir"
Write-Host "Next: compile packaging\windows\kirinuki.iss with Inno Setup to produce dist\KirinukiSetup.exe"
