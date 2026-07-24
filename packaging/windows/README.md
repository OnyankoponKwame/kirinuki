# Windows installer packaging

Produces `KirinukiSetup.exe` — a double-click installer that lets someone run Kirinuki
on their own Windows machine with **no terminal, no Python/Node install, no `claude`
CLI login**. They only need their own Anthropic + Groq API keys, entered once in the
in-app settings screen (⚙ 設定).

This repo's dev environment is macOS, so none of this has been executed or verified on
real Windows — see the "What's actually verified" section below before relying on it.

## How it fits together

```
packaging/windows/
├── launcher.py     — double-click entry point (run via pythonw.exe, no console window)
├── build.ps1        — stages a self-contained app folder (Python + Node + ffmpeg + yt-dlp)
├── kirinuki.iss      — Inno Setup script that wraps the staged folder into KirinukiSetup.exe
└── README.md        — this file
```

`build.ps1` downloads:
- Python embeddable distribution + `pip install -r requirements.txt`
- Portable Node.js + `npm install` inside `remotion/` (plus a best-effort pre-download
  of Remotion's headless Chrome build so first render doesn't need network access —
  double check the exact `npx remotion browser ...` command against the Remotion
  version pinned in `remotion/package.json`)
- Static ffmpeg/ffprobe builds and the yt-dlp.exe single-file binary

into `dist/Kirinuki/app/`, alongside a copy of this repo's `web/`, `audio-chunking/`,
`remotion/`, and `suggest_clips.py`. `kirinuki.iss` then installs that folder to
`%LOCALAPPDATA%\Kirinuki\app` and adds Start Menu / Desktop shortcuts pointing at
`pythonw.exe launcher.py`.

User data (downloads, transcriptions, rendered clips, `config.json` with API keys)
lives in `%LOCALAPPDATA%\Kirinuki\` **alongside**, not inside, `app\` — see
`web/config.py`'s `get_data_dir()`. Uninstalling the app (removing `app\`) leaves that
data in place.

## Building

### Option A — GitHub Actions (recommended, no Windows machine needed)

```
gh workflow run build-windows-installer.yml
gh run watch
```

Downloads the resulting `KirinukiSetup.exe` from the run's Artifacts tab (or via
`gh run download`).

### Option B — locally on a Windows machine

```powershell
.\packaging\windows\build.ps1
# Install Inno Setup: https://jrsoftware.org/isdl.php
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\kirinuki.iss
```

Output: `dist\KirinukiSetup.exe`.

## What's actually verified vs. what still needs a human on Windows

Verified (cross-platform, tested on this dev machine):
- Anthropic API replacing the `claude` CLI subprocess call
- Settings screen persists keys to `config.json` and blocks job creation with a clear
  message when required keys are missing

Not verified here — needs a real Windows machine or the CI run above:
- `build.ps1` actually downloading/staging everything correctly (URLs, embeddable
  Python `._pth` site-packages fix, Remotion headless Chrome pre-download)
- `launcher.py` launching cleanly via `pythonw.exe` with zero console windows,
  including the console-window suppression (`CREATE_NO_WINDOW`) on the yt-dlp/ffmpeg/
  Remotion subprocess calls it triggers
- The Inno Setup script producing a working installer end to end
- An actual video download → transcribe → suggest → render pass on the installed app

Once `KirinukiSetup.exe` is built, install it and click through: download a short
video, transcribe, get clip suggestions, render one clip. That pass is the real
acceptance test and can only happen on Windows.
