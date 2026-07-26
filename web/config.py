"""Persistent app settings (API keys, data dir) — separate from the dev-only .env file.

Packaged/distributed installs have no terminal to export env vars in, so API keys
are entered through the in-app settings screen and persisted to config.json under
the user's data dir. Priority, lowest to highest:
  1. bundled defaults (default_config.json, shipped next to this file — only present
     in a packaged build; baked in at build time from CI secrets, never committed)
  2. .env (dev convenience)
  3. config.json (settings screen) — always wins
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent

_SETTINGS_KEYS = ("GROQ_API_KEY", "GEMINI_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_KEYTERMS")


def get_data_dir() -> Path:
    override = os.getenv("KIRINUKI_DATA_DIR")
    if override:
        return Path(override)
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Kirinuki"
    return PROJECT_DIR


def find_cookies_file(data_dir: Path | None = None) -> Path | None:
    """Locate a cookies.txt-like file in the data dir.

    Browser cookie-export extensions (e.g. "Get cookies.txt LOCALLY") often name the
    exported file after the domain/host — "127.0.0.1_cookies.txt",
    "www.youtube.com_cookies.txt" — rather than plain "cookies.txt". Accept any
    filename ending in that suffix so users don't have to rename the export, still
    preferring an exact "cookies.txt" match, then the most recently modified one.
    """
    d = data_dir if data_dir is not None else get_data_dir()
    exact = d / "cookies.txt"
    if exact.exists():
        return exact
    candidates = sorted(d.glob("*cookies.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _config_path() -> Path:
    return get_data_dir() / "config.json"


def _bundled_defaults_path() -> Path:
    # Written by packaging/windows/build.ps1 into the staged app's web/ dir; absent
    # in a source checkout / dev environment.
    return Path(__file__).parent / "default_config.json"


def _read_json(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _read_config() -> dict[str, str]:
    return _read_json(_config_path())


def load_settings() -> dict[str, str]:
    """Load bundled defaults (packaged builds only, lowest priority) and persisted
    config.json (settings screen, always wins) and apply them to os.environ on top
    of .env. Returns the persisted (settings screen) dict."""
    bundled = _read_json(_bundled_defaults_path())
    for key in _SETTINGS_KEYS:
        value = bundled.get(key)
        if value and not os.environ.get(key):
            os.environ[key] = value

    settings = _read_config()
    for key in _SETTINGS_KEYS:
        value = settings.get(key)
        if value:
            os.environ[key] = value
    return settings


def save_settings(updates: dict[str, str]) -> None:
    """Persist non-blank fields to config.json and apply them to os.environ immediately."""
    existing = _read_config()
    for key in _SETTINGS_KEYS:
        value = updates.get(key)
        if value:
            existing[key] = value
            os.environ[key] = value

    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)


def settings_status() -> dict[str, bool | str]:
    """Which keys are currently configured (via .env or config.json). Never leaks values."""
    keyterms = os.environ.get("ELEVENLABS_KEYTERMS")
    if keyterms is None:
        keyterms = "飴白, 飴白なび"
    return {
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
        "elevenlabs_keyterms": keyterms,
    }


def no_window_kwargs() -> dict:
    """Extra subprocess.Popen/run kwargs to suppress a flashing console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def bootstrap_bin_path() -> None:
    """Prepend a bundled-binaries dir (ffmpeg/yt-dlp/node in a packaged install) to PATH."""
    bin_dir = os.getenv("KIRINUKI_BIN_DIR")
    if not bin_dir:
        return
    current = os.environ.get("PATH", "")
    if bin_dir not in current.split(os.pathsep):
        os.environ["PATH"] = bin_dir + os.pathsep + current


def get_npx_cmd() -> list[str]:
    """Returns base command list for executing npx cross-platform.
    On Windows, npx is a batch script (npx.cmd) which cannot be directly executed
    by CreateProcess when shell=False. Using ['cmd', '/c', 'npx'] ensures execution.
    """
    if sys.platform == "win32":
        return ["cmd", "/c", "npx"]
    return ["npx"]

