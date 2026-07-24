"""Persistent app settings (API keys, data dir) — separate from the dev-only .env file.

Packaged/distributed installs have no terminal to export env vars in, so API keys
are entered through the in-app settings screen and persisted to config.json under
the user's data dir. .env (dev convenience) loads first; config.json fills in and
overrides on top of it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent

_SETTINGS_KEYS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY")


def get_data_dir() -> Path:
    override = os.getenv("KIRINUKI_DATA_DIR")
    if override:
        return Path(override)
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Kirinuki"
    return PROJECT_DIR


def _config_path() -> Path:
    return get_data_dir() / "config.json"


def _read_config() -> dict[str, str]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_settings() -> dict[str, str]:
    """Load persisted API keys from config.json and apply them to os.environ,
    overriding anything already loaded from .env. Returns the persisted dict."""
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


def settings_status() -> dict[str, bool]:
    """Which keys are currently configured (via .env or config.json). Never leaks values."""
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "groq": bool(os.environ.get("GROQ_API_KEY")),
        "gemini": bool(os.environ.get("GEMINI_API_KEY")),
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
