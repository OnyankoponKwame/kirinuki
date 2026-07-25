export const CAPTION_EFFECTS = [
  "",
  "anger",
  "scary",
  "panic",
  "laugh",
  "hype",
  "pop",
  "punch",
  "pill",
  "neon",
  "glitch",
  "gaming",
  "cute",
  "news",
  "whisper",
  "question",
  "shock",
  "sad",
] as const;

export type CaptionEffect = (typeof CAPTION_EFFECTS)[number];

export const CAPTION_FONT_KEYS = [
  "mochiy",
  "rounded",
  "gothic",
  "pop",
  "impact",
  "retro",
  "mincho",
  "comment",
] as const;

export type CaptionFontKey = (typeof CAPTION_FONT_KEYS)[number];

export const CAPTION_FONT_PRESETS: Record<
  CaptionFontKey,
  {
    label: string;
    primaryFamily: string;
    family: string;
    weight: 400 | 700 | 800 | 900;
    lineHeight: number;
  }
> = {
  mochiy: {
    label: "Mochiy Pop",
    primaryFamily: '"Mochiy Pop One"',
    family: '"Mochiy Pop One", "Zen Maru Gothic", sans-serif',
    weight: 400,
    lineHeight: 1.48,
  },
  rounded: {
    label: "丸ゴ太字",
    primaryFamily: '"Zen Maru Gothic"',
    family: '"Zen Maru Gothic", "Hiragino Maru Gothic ProN", sans-serif',
    weight: 900,
    lineHeight: 1.55,
  },
  gothic: {
    label: "Noto極太",
    primaryFamily: '"Noto Sans JP"',
    family: '"Noto Sans JP", "Hiragino Sans", sans-serif',
    weight: 900,
    lineHeight: 1.48,
  },
  pop: {
    label: "M PLUSポップ",
    primaryFamily: '"M PLUS 1p"',
    family: '"M PLUS 1p", "M PLUS Rounded 1c", sans-serif',
    weight: 900,
    lineHeight: 1.5,
  },
  impact: {
    label: "デラ極太",
    primaryFamily: '"Dela Gothic One"',
    family: '"Dela Gothic One", "Noto Sans JP", sans-serif',
    weight: 400,
    lineHeight: 1.42,
  },
  retro: {
    label: "ロックンロール",
    primaryFamily: '"RocknRoll One"',
    family: '"RocknRoll One", "Noto Sans JP", sans-serif',
    weight: 400,
    lineHeight: 1.48,
  },
  mincho: {
    label: "明朝デコ",
    primaryFamily: '"Kaisei Decol"',
    family: '"Kaisei Decol", "Yu Mincho", serif',
    weight: 700,
    lineHeight: 1.5,
  },
  comment: {
    label: "コメント風",
    primaryFamily: '"M PLUS Rounded 1c"',
    family: '"M PLUS Rounded 1c", "Zen Maru Gothic", sans-serif',
    weight: 900,
    lineHeight: 1.55,
  },
};
