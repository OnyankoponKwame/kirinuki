"""Color theme storage — CRUD for clip themes, including in-place edits of
built-in presets and a user-configurable default theme.

Built-in themes ship in remotion/src/themes.json, the canonical source also used
by the Remotion renderer (remotion/src/themes.ts imports it directly). Local
changes made via the web UI's theme editor are persisted to custom_themes.json
under the data dir (see config.get_data_dir()):
  - Editing a built-in theme stores an "override" entry under the same id —
    the shipped defaults in themes.json are never touched, so the override can
    be reverted with reset_theme().
  - "+ 新規テーマ" creates a brand-new entry under a generated id; these can be
    deleted outright (delete_theme()).
  - The preferred default theme id is stored alongside the overrides.
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
    "captionActiveGlow",
    "captionFont",
)


def _custom_data_path() -> Path:
    return cfg.get_data_dir() / "custom_themes.json"


def load_builtin_themes() -> tuple[dict[str, dict], str]:
    with open(BUILTIN_THEMES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data["themes"], data["defaultThemeKey"]


def _load_custom_data() -> dict:
    path = _custom_data_path()
    if not path.exists():
        return {"defaultThemeKey": None, "themes": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"defaultThemeKey": None, "themes": {}}
    if "themes" not in data:  # legacy flat {id: theme, ...} format
        return {"defaultThemeKey": None, "themes": data}
    data.setdefault("defaultThemeKey", None)
    data.setdefault("themes", {})
    return data


def _save_custom_data(data: dict) -> None:
    path = _custom_data_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_custom_themes() -> dict[str, dict]:
    return _load_custom_data()["themes"]


def list_themes() -> dict:
    """All themes (built-in + overrides/custom), each tagged with its `source`:
    "builtin" (unmodified shipped preset), "override" (built-in id with local
    edits), or "custom" (user-created, deletable). `default` is the currently
    effective default theme id."""
    builtin, builtin_default = load_builtin_themes()
    custom_data = _load_custom_data()
    custom = custom_data["themes"]
    merged: dict[str, dict] = {}
    for theme_id, theme in builtin.items():
        merged[theme_id] = {**theme, "source": "builtin"}
    for theme_id, theme in custom.items():
        source = "override" if theme_id in builtin else "custom"
        merged[theme_id] = {**theme, "source": source}
    default_key = custom_data.get("defaultThemeKey") or builtin_default
    if default_key not in merged:
        default_key = builtin_default
    return {"default": default_key, "themes": merged}


def _sanitize(data: dict) -> dict:
    return {field: data[field] for field in THEME_FIELDS if field in data}


def create_theme(data: dict) -> tuple[str, dict]:
    custom_data = _load_custom_data()
    theme_id = f"custom_{uuid.uuid4().hex[:8]}"
    while theme_id in custom_data["themes"]:
        theme_id = f"custom_{uuid.uuid4().hex[:8]}"
    theme = _sanitize(data)
    custom_data["themes"][theme_id] = theme
    _save_custom_data(custom_data)
    return theme_id, theme


def update_theme(theme_id: str, data: dict) -> dict:
    """Edit any existing theme (built-in or custom) in place, including its
    label. Editing a built-in id stores a local override — the shipped preset
    in themes.json is untouched and can be restored via reset_theme(). Raises
    KeyError if theme_id doesn't match any known theme."""
    builtin, _ = load_builtin_themes()
    custom_data = _load_custom_data()
    if theme_id not in builtin and theme_id not in custom_data["themes"]:
        raise KeyError(theme_id)
    theme = _sanitize(data)
    custom_data["themes"][theme_id] = theme
    _save_custom_data(custom_data)
    return theme


def delete_theme(theme_id: str) -> None:
    """Permanently remove a user-created custom theme. Raises ValueError for
    a built-in id (use reset_theme() to revert an override instead), or
    KeyError if no such custom theme exists."""
    builtin, _ = load_builtin_themes()
    if theme_id in builtin:
        raise ValueError(theme_id)
    custom_data = _load_custom_data()
    if theme_id not in custom_data["themes"]:
        raise KeyError(theme_id)
    del custom_data["themes"][theme_id]
    _save_custom_data(custom_data)


def reset_theme(theme_id: str) -> dict:
    """Revert a built-in theme's local override back to the shipped preset.
    Raises ValueError if theme_id isn't a built-in id, or KeyError if it has
    no override to revert."""
    builtin, _ = load_builtin_themes()
    if theme_id not in builtin:
        raise ValueError(theme_id)
    custom_data = _load_custom_data()
    if theme_id not in custom_data["themes"]:
        raise KeyError(theme_id)
    del custom_data["themes"][theme_id]
    _save_custom_data(custom_data)
    return builtin[theme_id]


def set_default_theme(theme_id: str) -> None:
    if theme_id not in list_themes()["themes"]:
        raise KeyError(theme_id)
    custom_data = _load_custom_data()
    custom_data["defaultThemeKey"] = theme_id
    _save_custom_data(custom_data)


def resolve_theme_props(theme_key: str | None) -> dict:
    """Build the `theme` / `themeColors` Remotion render props for a clip's
    theme selection, falling back to the configured default when the clip has
    no theme set. themeColors carries the fully resolved color values so the
    renderer doesn't need to know about custom/override theme ids at all."""
    data = list_themes()
    key = theme_key or data["default"]
    theme = data["themes"].get(key)
    if not theme:
        return {"theme": key} if key else {}
    colors = {field: theme[field] for field in THEME_FIELDS if field in theme}
    return {"theme": key, "themeColors": colors}
