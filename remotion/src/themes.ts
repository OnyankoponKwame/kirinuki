import themesData from "./themes.json";
import { CaptionFontKey } from "./captionStyles";

export interface ClipTheme {
  label: string;
  titleBackground: string;
  titleTextColor: string;
  titleAccentColor: string;
  captionTextColor: string;
  captionActiveColor: string;
  captionActiveGlow: string;
  captionFont?: CaptionFontKey;
  titleFont?: CaptionFontKey;
  /** タイトルバーの最低高さ（px、1080幅基準）。本文が収まらない場合は自動で拡大される。デフォルト 280 */
  titleBarMinHeight?: number;
  /** タイトルバー上部の余白（フレーム高さに対する%）。デフォルト 5 */
  titleTopMargin?: number;
}

// Built-in presets — canonical source shared with the Python backend (see
// web/theme_store.py), which also serves them via /api/themes alongside
// user-created custom themes.
// Asserted rather than structurally checked: resolveJsonModule widens
// captionFont to `string`, losing the CaptionFontKey literal union.
export const THEMES: Record<string, ClipTheme> = themesData.themes as Record<
  string,
  ClipTheme
>;

export const DEFAULT_THEME_KEY: string = themesData.defaultThemeKey;
