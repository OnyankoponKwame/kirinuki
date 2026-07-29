import themesData from "./themes.json";
import { CaptionFontKey } from "./captionStyles";

export interface ClipTheme {
  label: string;
  titleBackground: string;
  titleTextColor: string;
  titleAccentColor: string;
  captionTextColor: string;
  captionActiveColor: string;
  captionFont?: CaptionFontKey;
  titleFont?: CaptionFontKey;
  /** タイトルバーの最低高さ（px、1080幅基準）。本文が収まらない場合は自動で拡大される。デフォルト 280 */
  titleBarMinHeight?: number;
  /** タイトルバー上部の余白（フレーム高さに対する%）。タイトル非表示時も安全マージンとして
   * 適用される。デフォルト 5 */
  titleTopMargin?: number;
  /** タイトルバーの最大行数（0=非表示, 2, 3）。デフォルト 2 */
  titleMaxLines?: 0 | 2 | 3;
  /** 字幕フォントサイズ（px、1080幅基準）。デフォルト 縦96 / 横72 */
  captionFontSize?: number;
  /** 二段構成の上段高さ割合（1〜9、下段は 10-この値）。デフォルト 4.5 */
  splitTopRatio?: number;
  /** 字幕の縦位置（画面上端からの% 40=上寄り 100=下端）。デフォルト 50 */
  captionPositionY?: number;
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
