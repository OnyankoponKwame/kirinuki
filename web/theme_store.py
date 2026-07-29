"""Color theme storage — CRUD for clip themes.

Every theme (the presets the app ships with, and anything created or edited via
the web UI's theme editor) lives as one record in a single central store,
`themes/themes_store.json` under the data dir (see config.get_data_dir()) — kept
in its own subfolder rather than flat in the data dir root, alongside the same
pattern used for downloads/, transcriptions/, etc. (see web/app.py). There is no
separate "shipped defaults" copy to fall back to — editing a theme overwrites
its record in place, permanently. Only "custom"-created themes can be deleted;
the presets the app ships with (marked "builtin" in the store) are protected
from deletion since there is nothing left to recreate them from once gone.

remotion/src/themes.json (the canonical preset source also imported directly
by the Remotion renderer, see remotion/src/themes.ts) is only ever read once,
on first run, to seed this store — see _migrate_from_legacy(). After that it
is not consulted again; Remotion's copy only matters as a Studio-side fallback
for when a clip's `themeColors` render prop is absent (see resolve_theme_props()
below, which always resolves and passes full color values).
"""

import json
import uuid
from pathlib import Path

import config as cfg

PROJECT_DIR = Path(__file__).parent.parent
BUILTIN_THEMES_PATH = PROJECT_DIR / "remotion" / "src" / "themes.json"

THEME_FIELDS = (
    "label",
    "titleBackground",
    "titleTextColor",
    "titleAccentColor",
    "captionTextColor",
    "captionActiveColor",
    "captionFont",
    "titleFont",
    "titleBarMinHeight",
    "titleTopMargin",
    "titleMaxLines",
    "captionFontSize",
    "splitTopRatio",
    "captionPositionY",
)


def _store_path() -> Path:
    return cfg.get_data_dir() / "themes" / "themes_store.json"


def _legacy_store_path() -> Path:
    # Pre-reorg location of the central store itself (same format as
    # _store_path(), just flat in the data dir root) — see _load_store().
    return cfg.get_data_dir() / "themes_store.json"


def _legacy_custom_path() -> Path:
    # Pre-migration override/custom-theme file — see _migrate_from_legacy().
    return cfg.get_data_dir() / "custom_themes.json"


def _sanitize(data: dict) -> dict:
    return {field: data[field] for field in THEME_FIELDS if field in data}


def _save_store(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _migrate_from_flat_location() -> dict | None:
    """One-time migration for installs that already have a central store from
    before it moved into its own themes/ subfolder — same format, just at
    _legacy_store_path() instead of _store_path(). Copies it into the new
    location as-is (already-resolved theme records, including any custom
    ones) and removes the old copy so it doesn't linger as stale duplicate
    data. Returns None (falling through to _migrate_from_legacy()) if there's
    nothing there, e.g. a fresh install."""
    legacy_path = _legacy_store_path()
    if not legacy_path.exists():
        return None
    try:
        with open(legacy_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    data.setdefault("defaultThemeKey", None)
    data.setdefault("themes", {})
    _save_store(data)
    try:
        legacy_path.unlink()
    except OSError:
        pass
    return data


def _migrate_from_legacy() -> dict:
    """One-time migration into the central store, run the first time it's read
    and found missing. Seeds with today's *effective* values — built-in
    presets merged with whatever local overrides already existed in the old
    custom_themes.json — so nothing a user already customized gets reset back
    to the shipped defaults; it just becomes the new baseline."""
    with open(BUILTIN_THEMES_PATH, encoding="utf-8") as f:
        builtin_data = json.load(f)
    builtin, builtin_default = builtin_data["themes"], builtin_data["defaultThemeKey"]

    legacy = {"defaultThemeKey": None, "themes": {}}
    legacy_path = _legacy_custom_path()
    if legacy_path.exists():
        try:
            with open(legacy_path, encoding="utf-8") as f:
                data = json.load(f)
            if "themes" not in data:  # even older flat {id: theme, ...} format
                legacy = {"defaultThemeKey": None, "themes": data}
            else:
                data.setdefault("defaultThemeKey", None)
                data.setdefault("themes", {})
                legacy = data
        except (json.JSONDecodeError, OSError):
            pass

    themes: dict[str, dict] = {}
    for theme_id, theme in builtin.items():
        themes[theme_id] = {**_sanitize(theme), "builtin": True}
    for theme_id, theme in legacy["themes"].items():
        themes[theme_id] = {**_sanitize(theme), "builtin": theme_id in builtin}

    store = {
        "defaultThemeKey": legacy.get("defaultThemeKey") or builtin_default,
        "themes": themes,
    }
    _save_store(store)
    return store


def _load_store() -> dict:
    path = _store_path()
    if not path.exists():
        moved = _migrate_from_flat_location()
        if moved is not None:
            return moved
        return _migrate_from_legacy()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _migrate_from_legacy()
    data.setdefault("defaultThemeKey", None)
    data.setdefault("themes", {})
    return data


def list_themes() -> dict:
    """All themes, each tagged with its `source`: "builtin" (ships with the
    app, protected from deletion) or "custom" (user-created, deletable).
    `default` is the currently effective default theme id."""
    store = _load_store()
    themes = {
        theme_id: {**_sanitize(theme), "source": "builtin" if theme.get("builtin") else "custom"}
        for theme_id, theme in store["themes"].items()
    }
    default_key = store.get("defaultThemeKey")
    if default_key not in themes:
        default_key = next(iter(themes), None)
    return {"default": default_key, "themes": themes}


def create_theme(data: dict) -> tuple[str, dict]:
    store = _load_store()
    theme_id = f"custom_{uuid.uuid4().hex[:8]}"
    while theme_id in store["themes"]:
        theme_id = f"custom_{uuid.uuid4().hex[:8]}"
    theme = {**_sanitize(data), "builtin": False}
    store["themes"][theme_id] = theme
    _save_store(store)
    return theme_id, {**_sanitize(theme), "source": "custom"}


def update_theme(theme_id: str, data: dict) -> dict:
    """Edit any existing theme in place — built-in-origin or custom, no
    distinction: this store holds each theme's live values directly, so an
    edit just overwrites them (permanently — there is no original to revert
    to). Raises KeyError if theme_id doesn't match any known theme."""
    store = _load_store()
    if theme_id not in store["themes"]:
        raise KeyError(theme_id)
    is_builtin = store["themes"][theme_id].get("builtin", False)
    theme = {**_sanitize(data), "builtin": is_builtin}
    store["themes"][theme_id] = theme
    _save_store(store)
    return {**_sanitize(theme), "source": "builtin" if is_builtin else "custom"}


def delete_theme(theme_id: str) -> None:
    """Permanently remove a user-created theme. Raises ValueError for a
    built-in-origin id (protected — not deletable, there's no shipped copy
    left to fall back to), or KeyError if no such theme exists."""
    store = _load_store()
    if theme_id not in store["themes"]:
        raise KeyError(theme_id)
    if store["themes"][theme_id].get("builtin", False):
        raise ValueError(theme_id)
    del store["themes"][theme_id]
    _save_store(store)


def set_default_theme(theme_id: str) -> None:
    store = _load_store()
    if theme_id not in store["themes"]:
        raise KeyError(theme_id)
    store["defaultThemeKey"] = theme_id
    _save_store(store)


def resolve_theme_props(theme_key: str | None) -> dict:
    """Build the `theme` / `themeColors` Remotion render props for a clip's
    theme selection, falling back to the configured default when the clip has
    no theme set. themeColors carries the fully resolved color values so the
    renderer doesn't need to know about custom theme ids at all."""
    data = list_themes()
    key = theme_key or data["default"]
    theme = data["themes"].get(key)
    if not theme:
        return {"theme": key} if key else {}
    colors = {field: theme[field] for field in THEME_FIELDS if field in theme}
    return {"theme": key, "themeColors": colors}
