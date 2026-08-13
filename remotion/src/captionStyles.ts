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

// ── 効果音セット ──────────────────────────────────────────────────────────────
// remotion/public/sfx/*.mp3 の実体は効果音ラボの素材（tools/fetch_caption_sfx.py が取得する）。
// ショート動画では「テロップと効果音はセット」（文字の出現と同時に鳴らす）が定石で、
// 効果音は視聴者に「ここを読め」と知らせる聴覚的な箇条書きとして働く。
//
// durMs は実ファイルの長さ = 再生 Sequence の長さ。これより長い音は途中で切れるので、
// 音を差し替えたら必ず実測値に合わせること（tools/fetch_caption_sfx.py が値を表示する）。
// gain は知覚音量をそろえるための係数。素材はピーク -3dBFS に正規化済みなので、
// 声にかぶらない音量まで下げる役割を担う。
// leadMs は「字幕が出る何ms前から鳴らし始めるか」。現在の素材はすべて頭にアタックが来る
// 単発音なので 0 だが、盛り上がり音（riser）に差し替えるならクレッシェンドの頂点が
// 字幕の出現と一致するよう leadMs = 全長 - 200ms 程度を入れる。

export const SFX_KEYS = [
  "pop",
  "impact",
  "boom",
  "ding",
  "sparkle",
  "boing",
  "swell",
  "glitch",
  "sad",
  "alarm",
  "levelup",
  "wonder",
] as const;

export type SfxKey = (typeof SFX_KEYS)[number];

export const SFX: Record<SfxKey, { label: string; durMs: number; gain: number; leadMs: number }> = {
  pop: { label: "パッ", durMs: 1003, gain: 0.26, leadMs: 0 },
  impact: { label: "ドンッ", durMs: 1739, gain: 0.17, leadMs: 0 },
  boom: { label: "ジャジャーン", durMs: 1846, gain: 0.28, leadMs: 0 },
  ding: { label: "キラッ", durMs: 990, gain: 0.27, leadMs: 0 },
  sparkle: { label: "キラーン", durMs: 2263, gain: 0.65, leadMs: 0 },
  boing: { label: "ビヨン", durMs: 685, gain: 0.27, leadMs: 0 },
  swell: { label: "ホラーテロップ", durMs: 2203, gain: 0.27, leadMs: 0 },
  glitch: { label: "レコードスクラッチ", durMs: 1165, gain: 0.16, leadMs: 0 },
  sad: { label: "ぽちゃん", durMs: 1742, gain: 0.45, leadMs: 0 },
  alarm: { label: "警告音", durMs: 1118, gain: 0.26, leadMs: 0 },
  levelup: { label: "レベルアップ", durMs: 2198, gain: 0.25, leadMs: 0 },
  wonder: { label: "ピコン？", durMs: 686, gain: 0.18, leadMs: 0 },
};

// ── 字幕モーション ────────────────────────────────────────────────────────────
// プロのショート動画では「出現の瞬間」にアニメーションを集中させ、
// 表示中は動かしすぎない（常時揺らすと視覚疲労で完走率が落ちる）のが定石。
// このプロジェクトは元々ループの揺れしか持っていなかったので、
// enter（入り）を主役に据え、loop はその倍率として弱めに掛け直している。

export type EnterAnimation =
  | "pop" // 少し小さい状態から 1.0 をわずかにオーバーシュート（標準）
  | "slam" // 大きい状態から叩きつける（衝撃・断言）
  | "drop" // 上から落ちて弾む
  | "rise" // 下からふわっと上がる（穏やか）
  | "fade" // フェードのみ
  | "blurZoom" // ぼけた拡大から収束（不穏）
  | "flicker" // 明滅してから定着（デジタル）
  | "none";

export type EffectMotion = {
  /** 出現アニメーションの種別 */
  enter: EnterAnimation;
  /** 出現アニメーションの振れ幅（1.0 が基準） */
  enterStrength: number;
  /** 表示中のループアニメーションの振幅倍率（0 で静止） */
  loop: number;
  /** 字幕出現に合わせた映像のデジタルパンチイン倍率（1 で無効） */
  punchIn: number;
  /** 同時に鳴らす効果音（null で無音） */
  sfx: SfxKey | null;
  /** 単語を発話タイミングどおりに1つずつ出す（先の単語は透明にして位置は固定） */
  revealTokens?: boolean;
};

export const EFFECT_MOTION: Record<CaptionEffect, EffectMotion> = {
  // エフェクト指定なしの通常字幕にも、控えめなポップインだけは掛ける
  "": { enter: "pop", enterStrength: 0.55, loop: 0, punchIn: 1, sfx: null },
  anger: { enter: "slam", enterStrength: 1.15, loop: 0.85, punchIn: 1.1, sfx: "impact" },
  scary: { enter: "blurZoom", enterStrength: 1.0, loop: 0.8, punchIn: 1.03, sfx: "swell" },
  panic: { enter: "slam", enterStrength: 0.9, loop: 1.0, punchIn: 1.06, sfx: "alarm" },
  laugh: { enter: "drop", enterStrength: 1.0, loop: 0.8, punchIn: 1.04, sfx: "boing" },
  hype: { enter: "pop", enterStrength: 1.15, loop: 0.8, punchIn: 1.06, sfx: "ding" },
  pop: { enter: "pop", enterStrength: 1.0, loop: 0, punchIn: 1, sfx: "pop" },
  punch: { enter: "slam", enterStrength: 1.05, loop: 0, punchIn: 1.08, sfx: "impact" },
  pill: { enter: "pop", enterStrength: 0.9, loop: 0, punchIn: 1, sfx: "pop" },
  neon: { enter: "fade", enterStrength: 0.8, loop: 0.7, punchIn: 1, sfx: "sparkle" },
  glitch: { enter: "flicker", enterStrength: 1.0, loop: 0.9, punchIn: 1.03, sfx: "glitch" },
  gaming: { enter: "pop", enterStrength: 1.0, loop: 0.8, punchIn: 1.05, sfx: "levelup" },
  cute: { enter: "drop", enterStrength: 0.9, loop: 0.85, punchIn: 1, sfx: "pop" },
  news: { enter: "fade", enterStrength: 0.6, loop: 0, punchIn: 1, sfx: "ding", revealTokens: true },
  // 小声はあえて音を置かない（急な静寂そのものが演出になる）
  whisper: { enter: "fade", enterStrength: 0.7, loop: 0.7, punchIn: 1, sfx: null },
  question: { enter: "rise", enterStrength: 0.85, loop: 0.8, punchIn: 1, sfx: "wonder" },
  shock: { enter: "slam", enterStrength: 1.3, loop: 0.75, punchIn: 1.12, sfx: "boom" },
  sad: { enter: "rise", enterStrength: 0.8, loop: 0.9, punchIn: 1, sfx: "sad" },
};

export const DEFAULT_MOTION: EffectMotion = EFFECT_MOTION[""];

export function getEffectMotion(effect: CaptionEffect | undefined): EffectMotion {
  return (effect && EFFECT_MOTION[effect]) || DEFAULT_MOTION;
}

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
