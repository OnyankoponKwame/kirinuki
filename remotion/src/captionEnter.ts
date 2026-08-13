import { interpolate, spring } from "remotion";
import type { EnterAnimation } from "./captionStyles";

/**
 * 字幕ページが出現する瞬間のアニメーション。
 *
 * ショート動画で完走率に効くのは「表示中ずっと動かすこと」ではなく
 * 「出た瞬間に一度だけ強くアクセントを付けること」なので、ここに演出を集中させる。
 * 戻り値の progress は入りが終わったかどうか（0→1）で、CaptionPage 側で
 * ループアニメーションを入りの後から立ち上げるために使う。
 */

export type EnterState = {
  transform: string;
  opacity: number;
  filter?: string;
  progress: number;
};

/** 入りアニメーションの想定尺（フレーム / 30fps 基準）。progress の分母になる */
export const ENTER_FRAMES: Record<EnterAnimation, number> = {
  pop: 10,
  slam: 9,
  drop: 13,
  rise: 10,
  fade: 8,
  blurZoom: 12,
  flicker: 9,
  none: 0,
};

const IDLE: EnterState = { transform: "", opacity: 1, progress: 1 };

// デジタル機器が点灯するときのような明滅パターン（frame 番号でそのまま引く）
const FLICKER_PATTERN = [0, 1, 0, 1, 1, 0, 1, 1, 1];

export function getEnterState(
  kind: EnterAnimation,
  strength: number,
  frame: number,
  fps: number,
): EnterState {
  if (kind === "none" || strength <= 0) return IDLE;

  const frames = ENTER_FRAMES[kind];
  const progress = frames > 0 ? Math.min(1, frame / frames) : 1;
  if (progress >= 1) return IDLE;

  const fadeIn = (overFrames: number) =>
    interpolate(frame, [0, overFrames], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });

  switch (kind) {
    case "pop": {
      // ばね（低めのダンピング）で 1.0 をわずかに超えてから収まる
      const s = spring({ frame, fps, config: { damping: 10.5, stiffness: 190, mass: 0.55 } });
      const scale = interpolate(s, [0, 1], [1 - 0.34 * strength, 1]);
      return { transform: `scale(${scale.toFixed(4)})`, opacity: fadeIn(2.5), progress };
    }
    case "slam": {
      // 大きい状態から一気に叩きつけ、反動で少し沈んでから戻る
      const s = spring({ frame, fps, config: { damping: 13, stiffness: 340, mass: 0.7 } });
      const scale = interpolate(s, [0, 1], [1 + 0.75 * strength, 1]);
      const deg = interpolate(s, [0, 1], [-2.4 * strength, 0]);
      return {
        transform: `scale(${scale.toFixed(4)}) rotate(${deg.toFixed(3)}deg)`,
        opacity: fadeIn(2),
        progress,
      };
    }
    case "drop": {
      const s = spring({ frame, fps, config: { damping: 8.5, stiffness: 170, mass: 0.7 } });
      const y = interpolate(s, [0, 1], [-80 * strength, 0]);
      return { transform: `translateY(${y.toFixed(2)}px)`, opacity: fadeIn(3), progress };
    }
    case "rise": {
      // damping を大きく取ってオーバーシュートさせない（穏やかな感情向け）
      const s = spring({ frame, fps, config: { damping: 200, stiffness: 90, mass: 0.6 } });
      const y = interpolate(s, [0, 1], [46 * strength, 0]);
      return { transform: `translateY(${y.toFixed(2)}px)`, opacity: fadeIn(7), progress };
    }
    case "fade": {
      const s = spring({ frame, fps, config: { damping: 200, stiffness: 120, mass: 0.5 } });
      const scale = interpolate(s, [0, 1], [1 + 0.06 * strength, 1]);
      return { transform: `scale(${scale.toFixed(4)})`, opacity: fadeIn(6), progress };
    }
    case "blurZoom": {
      const s = spring({ frame, fps, config: { damping: 200, stiffness: 70, mass: 0.7 } });
      const scale = interpolate(s, [0, 1], [1 + 0.35 * strength, 1]);
      const blur = interpolate(s, [0, 1], [16 * strength, 0]);
      return {
        transform: `scale(${scale.toFixed(4)})`,
        opacity: fadeIn(5),
        filter: blur > 0.4 ? `blur(${blur.toFixed(2)}px)` : undefined,
        progress,
      };
    }
    case "flicker": {
      const lit = frame < FLICKER_PATTERN.length ? FLICKER_PATTERN[frame] : 1;
      const x = (frame % 2 === 0 ? 1 : -1) * 6 * strength * (1 - progress);
      return {
        transform: `translateX(${x.toFixed(2)}px)`,
        opacity: lit,
        progress,
      };
    }
    default:
      return IDLE;
  }
}
