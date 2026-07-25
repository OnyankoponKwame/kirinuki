import themesData from "./themes.json";

export interface ClipTheme {
  label: string;
  titleBackground: string;
  titleTextColor: string;
  titleAccentColor: string;
  captionTextColor: string;
  captionActiveColor: string;
  captionActiveGlow: string;
}

// Built-in presets — canonical source shared with the Python backend (see
// web/theme_store.py), which also serves them via /api/themes alongside
// user-created custom themes.
export const THEMES: Record<string, ClipTheme> = themesData.themes;

export const DEFAULT_THEME_KEY: string = themesData.defaultThemeKey;
