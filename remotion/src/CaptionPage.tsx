import { AbsoluteFill, Audio, Img, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import type { TikTokPage } from "@remotion/captions";
import { THEMES, DEFAULT_THEME_KEY } from "./themes";
import type { ClipTheme } from "./themes";
import {
  CAPTION_FONT_PRESETS,
  type CaptionEffect,
  type CaptionFontKey,
} from "./captionStyles";

const userIconSrc = staticFile("kkrn_icon_user_2.png");
const popSound = staticFile("Onoma-Pop04.mp3");

const OUTLINE_COLOR = "#000000";
const TEXT_SHADOW = [
  `-3px -3px 0 ${OUTLINE_COLOR}`,
  ` 3px -3px 0 ${OUTLINE_COLOR}`,
  `-3px  3px 0 ${OUTLINE_COLOR}`,
  ` 3px  3px 0 ${OUTLINE_COLOR}`,
  ` 0px  3px 0 ${OUTLINE_COLOR}`,
  ` 0px -3px 0 ${OUTLINE_COLOR}`,
  ` 3px  0px 0 ${OUTLINE_COLOR}`,
  `-3px  0px 0 ${OUTLINE_COLOR}`,
  `0 4px 10px rgba(0,0,0,0.6)`,
].join(",");

function outline(color: string, size = 3): string[] {
  return [
    `-${size}px -${size}px 0 ${color}`,
    ` ${size}px -${size}px 0 ${color}`,
    `-${size}px  ${size}px 0 ${color}`,
    ` ${size}px  ${size}px 0 ${color}`,
    ` 0px  ${size}px 0 ${color}`,
    ` 0px -${size}px 0 ${color}`,
    ` ${size}px  0px 0 ${color}`,
    `-${size}px  0px 0 ${color}`,
  ];
}

function makeActiveShadow(theme: ClipTheme): string {
  return [
    ...outline("#FFFFFF"),
    `5px 5px 0px ${theme.titleAccentColor}`,
    `0 0 15px ${theme.captionActiveGlow}`,
  ].join(",");
}

type ActiveBlock = "pill" | "box";
type EffectStyle = {
  color: string;
  textColor: string;
  shadow: string;
  block?: ActiveBlock;
  background?: string;
  border?: string;
  borderRadius?: number;
  padding?: string;
  opacity?: number;
};

const EFFECT_ACTIVE: Partial<Record<CaptionEffect, EffectStyle>> = {
  anger: {
    color: "#FF1F1F",
    textColor: "#FFB0A0",
    shadow: [
      ...outline("#FFFFFF", 3),
      ...outline("#5A0000", 5),
      `8px 8px 0 #8B0000`,
      `0 0 22px rgba(255,0,0,1)`,
      `0 0 52px rgba(255,60,0,0.7)`,
    ].join(","),
  },
  scary: {
    color: "#D8FFEA",
    textColor: "#C8D6FF",
    shadow: [
      ...outline("#090014", 4),
      `5px 5px 0 #2A0A4A`,
      `0 0 18px rgba(76,255,176,0.75)`,
      `0 0 46px rgba(87,30,160,0.85)`,
    ].join(","),
  },
  panic: {
    color: "#FF2200",
    textColor: "#FF8080",
    shadow: [
      ...outline("#FFFFFF"),
      `5px 5px 0px #7B0000`,
      `0 0 18px rgba(255,40,0,0.9)`,
      `0 0 40px rgba(255,0,0,0.45)`,
    ].join(","),
  },
  laugh: {
    color: "#31FF45",
    textColor: "#DFFFDF",
    shadow: [
      ...outline("#06380A", 4),
      `5px 5px 0px #0B7A16`,
      `0 0 18px rgba(49,255,69,0.9)`,
      `0 0 36px rgba(49,255,69,0.45)`,
    ].join(","),
  },
  hype: {
    color: "#FF9500",
    textColor: "#FFB347",
    shadow: [
      ...outline("#FFFFFF"),
      `5px 5px 0px #7B4000`,
      `0 0 20px rgba(255,149,0,0.9)`,
      `0 0 40px rgba(255,100,0,0.4)`,
    ].join(","),
  },
  pop: {
    color: "#FFFFFF",
    textColor: "#FFFFFF",
    shadow: [...outline("#000000", 4), `0 8px 0 #FF2F92`, `0 0 22px rgba(255,255,255,0.55)`].join(","),
  },
  punch: {
    color: "#F7C204",
    textColor: "#FFFFFF",
    shadow: [...outline("#000000", 4), `7px 7px 0 #E73131`, `0 0 18px rgba(247,194,4,0.6)`].join(","),
  },
  pill: {
    color: "#0B0B0B",
    textColor: "#FFFFFF",
    shadow: "none",
    block: "pill",
    background: "#FFE500",
    border: "4px solid #000000",
    borderRadius: 18,
    padding: "0.02em 0.18em 0.08em",
  },
  neon: {
    color: "#67F7FF",
    textColor: "#DFFBFF",
    shadow: [
      ...outline("#09131A", 2),
      `0 0 10px rgba(103,247,255,0.95)`,
      `0 0 26px rgba(255,58,196,0.75)`,
      `0 0 44px rgba(103,247,255,0.5)`,
    ].join(","),
  },
  glitch: {
    color: "#FFFFFF",
    textColor: "#FFFFFF",
    shadow: [
      ...outline("#020202", 3),
      `-5px 0 #00E5FF`,
      `5px 0 #FF2F92`,
      `0 0 18px rgba(255,255,255,0.35)`,
    ].join(","),
  },
  gaming: {
    color: "#7CFF00",
    textColor: "#E9FFD8",
    shadow: [...outline("#102000", 4), `6px 6px 0 #255D00`, `0 0 22px rgba(124,255,0,0.75)`].join(","),
  },
  cute: {
    color: "#FF7AC8",
    textColor: "#FFE3F3",
    shadow: [...outline("#FFFFFF", 3), `5px 5px 0 #A0186E`, `0 0 18px rgba(255,122,200,0.9)`].join(","),
  },
  news: {
    color: "#FFFFFF",
    textColor: "#FFFFFF",
    shadow: [...outline("#062A70", 3), `5px 5px 0 #0D4DCE`, `0 0 12px rgba(255,255,255,0.45)`].join(","),
    block: "box",
    background: "rgba(7, 39, 112, 0.86)",
    border: "4px solid rgba(255,255,255,0.92)",
    borderRadius: 8,
    padding: "0.02em 0.14em 0.07em",
  },
  whisper: {
    color: "#E8F6FF",
    textColor: "#D9ECFF",
    opacity: 0.82,
    shadow: [...outline("rgba(0,0,0,0.72)", 2), `0 0 16px rgba(142,211,255,0.5)`].join(","),
  },
  question: {
    color: "#C8A5FF",
    textColor: "#F4ECFF",
    shadow: [...outline("#1B063A", 3), `5px 5px 0 #4B1597`, `0 0 20px rgba(200,165,255,0.8)`].join(","),
  },
  shock: {
    color: "#FFF200",
    textColor: "#FFF7A0",
    shadow: [...outline("#000000", 5), `8px 8px 0 #00B7FF`, `0 0 28px rgba(255,242,0,0.9)`].join(","),
  },
  sad: {
    color: "#18B0E0",
    textColor: "#A0D8F8",
    opacity: 0.92,
    shadow: [
      ...outline("#061420", 3),
      `0 7px 0 #0A4A70`,
      `0 0 20px rgba(24,176,224,0.80)`,
      `0 0 44px rgba(0,140,210,0.40)`,
    ].join(","),
  },
};

function clamp01(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function getTextTransform(type: CaptionEffect, timeMs: number, intensity: number): string {
  if (intensity === 0) return "";
  const t = timeMs / 1000;
  const tau = Math.PI * 2;

  switch (type) {
    case "anger": {
      const x = Math.sin(t * tau * 2.4) * 5.5 * intensity;
      const y = Math.cos(t * tau * 2.0) * 1.8 * intensity;
      const deg = Math.sin(t * tau * 1.8) * 2.4 * intensity;
      const s = 1 + Math.max(0, Math.sin(t * tau * 1.7)) * 0.065 * intensity;
      return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) rotate(${deg.toFixed(3)}deg) scale(${s.toFixed(4)})`;
    }
    case "scary": {
      const x = Math.sin(t * tau * 1.7) * 1.8 * intensity;
      const y = Math.cos(t * tau * 2.1) * 1.4 * intensity;
      const deg = Math.sin(t * tau * 0.9) * 1.2 * intensity;
      const s = 1 + Math.sin(t * tau * 1.1) * 0.018 * intensity;
      return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) rotate(${deg.toFixed(3)}deg) scale(${s.toFixed(4)})`;
    }
    case "panic": {
      const x = (Math.sin(t * tau * 3.1) * 0.7 + Math.sin(t * tau * 7.3) * 0.3) * 5 * intensity;
      const y = (Math.cos(t * tau * 4.7) * 0.7 + Math.cos(t * tau * 9.1) * 0.3) * 3 * intensity;
      return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
    }
    case "laugh":
      {
        const x = Math.sin(t * tau * 5.2) * 1.8 * intensity;
        const y = Math.cos(t * tau * 4.6) * 1.2 * intensity;
        const deg = Math.sin(t * tau * 3.8) * 0.9 * intensity;
        return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) rotate(${deg.toFixed(3)}deg)`;
      }
    case "cute": {
      const y = Math.sin(t * tau * 2.5) * (type === "cute" ? 6 : 8) * intensity;
      const deg = type === "cute" ? Math.sin(t * tau * 1.8) * 1.5 * intensity : 0;
      return `translateY(${y.toFixed(2)}px) rotate(${deg.toFixed(3)}deg)`;
    }
    case "hype":
    case "shock": {
      const s = 1 + Math.sin(t * tau * 3.0) * (type === "shock" ? 0.06 : 0.045) * intensity;
      return `scale(${s.toFixed(4)})`;
    }
    case "question": {
      const deg = Math.sin(t * tau * 1.1) * 3.0 * intensity;
      return `rotate(${deg.toFixed(3)}deg)`;
    }
    case "neon": {
      const s = 1 + Math.sin(t * tau * 1.7) * 0.018 * intensity;
      return `scale(${s.toFixed(4)})`;
    }
    case "glitch": {
      const x = Math.sin(t * tau * 10.7) * 3 * intensity;
      const y = Math.cos(t * tau * 8.3) * 2 * intensity;
      return `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) skewX(${(x * 0.35).toFixed(2)}deg)`;
    }
    case "gaming": {
      const y = Math.sin(t * tau * 4) > 0 ? -3 * intensity : 2 * intensity;
      return `translateY(${y.toFixed(2)}px)`;
    }
    case "sad": {
      const y = Math.sin(t * tau * 0.45) * 4.5 * intensity;
      const deg = Math.sin(t * tau * 0.3) * 1.4 * intensity;
      return `translate(0, ${y.toFixed(2)}px) rotate(${deg.toFixed(3)}deg)`;
    }
    default:
      return "";
  }
}

function getActiveTokenTransform(type: CaptionEffect, progress: number): string | undefined {
  const pop = Math.sin(clamp01(progress) * Math.PI);
  switch (type) {
    case "":
    case "pill":
    case "news":
      return undefined;
    case "pop":
      return `scale(${(1 + pop * 0.18).toFixed(4)})`;
    case "anger":
      return `translateX(${(Math.sin(progress * Math.PI * 4) * 4).toFixed(2)}px) scale(${(1 + pop * 0.2).toFixed(4)})`;
    case "scary":
      return `translateY(${(-4 * pop).toFixed(2)}px) scale(${(1 + pop * 0.06).toFixed(4)})`;
    case "punch":
    case "shock":
      return `scale(${(1 + pop * 0.24).toFixed(4)}) rotate(${(-2 + pop * 4).toFixed(3)}deg)`;
    case "laugh":
      return `translate(${(Math.sin(progress * Math.PI * 8) * 1.8).toFixed(2)}px, ${(-3 * pop).toFixed(2)}px) rotate(${(Math.sin(progress * Math.PI * 6) * 1.1).toFixed(3)}deg) scale(${(1 + pop * 0.04).toFixed(4)})`;
    case "cute":
      return `translateY(${(-10 * pop).toFixed(2)}px) scale(${(1 + pop * 0.09).toFixed(4)})`;
    case "hype":
    case "gaming":
      return `scale(${(1 + pop * 0.12).toFixed(4)})`;
    case "glitch":
      return `translateX(${(Math.sin(progress * Math.PI * 10) * 3).toFixed(2)}px)`;
    case "whisper":
      return `scale(${(1 + pop * 0.04).toFixed(4)})`;
    case "sad":
      return `translateY(${(5 * pop).toFixed(2)}px) scale(${(1 - pop * 0.04).toFixed(4)})`;
    default:
      return `scale(${(1 + pop * 0.08).toFixed(4)})`;
  }
}

export const CaptionPage: React.FC<{
  page: TikTokPage;
  paddingBottomOverride?: number;
  topOffset?: number;
  captionFontSize?: number;
  captionFont?: CaptionFontKey;
  theme?: ClipTheme;
  effect?: CaptionEffect;
  suffix?: string;
  isComment?: boolean;
}> = ({ page, paddingBottomOverride, topOffset, captionFontSize, captionFont, theme, effect, suffix, isComment }) => {
  const t = theme ?? THEMES[DEFAULT_THEME_KEY];
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const isVertical = height > width;
  const fontPreset = CAPTION_FONT_PRESETS[captionFont ?? "mochiy"];

  const currentTimeMs = (frame / fps) * 1000;
  const absoluteTimeMs = page.startMs + currentTimeMs;

  const lastToken = page.tokens[page.tokens.length - 1];
  const pageDurationMs = lastToken ? lastToken.toMs - page.startMs : 1000;
  const rampMs = 200;
  const effectType = effect ?? "";
  const intensity = effect
    ? (() => {
        const raw = Math.min(
          1,
          currentTimeMs / rampMs,
          (pageDurationMs - currentTimeMs) / rampMs,
        );
        return Math.max(0, (1 - Math.cos(raw * Math.PI)) / 2);
      })()
    : 0;

  const effectStyle = EFFECT_ACTIVE[effectType];
  const textTransform = getTextTransform(effectType, absoluteTimeMs, intensity);

  const fontSize = captionFontSize ?? (isVertical ? 96 : 72);
  const paddingBottom = paddingBottomOverride ?? (isVertical ? 160 : 64);

  const posStyle =
    topOffset !== undefined
      ? { justifyContent: "flex-start", alignItems: "center", paddingTop: topOffset }
      : { justifyContent: "flex-end", alignItems: "center", paddingBottom };

  if (isComment) {
    const bubbleBg = "rgba(255, 255, 255, 0.95)";
    const bubbleFontSize = Math.round(fontSize * 0.88);
    const iconSize = 130;
    const outerPad = 40;
    const iconOverlap = iconSize - outerPad;

    const commentTokens = page.tokens
      .map((token, i) =>
        i === 0 ? { ...token, text: token.text.replace(/^\s+/, "") } : token,
      )
      .filter(token => token.text.length > 0);

    return (
      <AbsoluteFill style={posStyle as React.CSSProperties}>
        <Audio src={popSound} />
        <div style={{ position: "relative", maxWidth: isVertical ? "88%" : "82%", paddingTop: outerPad, paddingLeft: outerPad }}>
          <Img
            src={userIconSrc}
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: iconSize,
              height: iconSize,
              zIndex: 10,
            }}
          />
          <div
            style={{
              background: bubbleBg,
              borderRadius: isVertical ? 48 : 36,
              paddingTop: isVertical ? iconOverlap + 8 : iconOverlap + 6,
              paddingBottom: isVertical ? 20 : 14,
              paddingLeft: isVertical ? 28 : 22,
              paddingRight: isVertical ? 28 : 22,
              textAlign: "center",
            }}
          >
            <span
              style={{
                fontFamily: fontPreset.family,
                fontSize: bubbleFontSize,
                fontWeight: fontPreset.weight,
                color: "#000000",
                whiteSpace: "pre-wrap",
                lineHeight: fontPreset.lineHeight,
              }}
            >
              {commentTokens.map((token) => (
                <span key={token.fromMs} style={{ color: "#000000" }}>{token.text}</span>
              ))}
            </span>
          </div>
          <div
            style={{
              position: "absolute",
              bottom: -14,
              left: "50%",
              transform: "translateX(-50%)",
              width: 0,
              height: 0,
              borderLeft: "16px solid transparent",
              borderRight: "16px solid transparent",
              borderTop: `16px solid ${bubbleBg}`,
            }}
          />
        </div>
      </AbsoluteFill>
    );
  }

  const baseTextColor =
    effectStyle && intensity > 0
      ? effectStyle.textColor
      : t.captionTextColor;
  const baseOpacity = effectStyle?.opacity ?? 1;

  return (
    <AbsoluteFill style={posStyle as React.CSSProperties}>
      <div
        style={{
          padding: isVertical ? "16px 20px" : "10px 16px",
          maxWidth: isVertical ? "96%" : "92%",
          textAlign: "center",
          transform: textTransform || undefined,
          transformOrigin: "center bottom",
          willChange: textTransform ? "transform" : undefined,
        }}
      >
        <span
          style={{
            fontFamily: fontPreset.family,
            fontSize,
            fontWeight: fontPreset.weight,
            color: baseTextColor,
            opacity: baseOpacity,
            textShadow: TEXT_SHADOW,
            whiteSpace: "pre-wrap",
            lineHeight: fontPreset.lineHeight,
          }}
        >
          {page.tokens.map((token) => {
            const isActive =
              token.fromMs <= absoluteTimeMs && token.toMs > absoluteTimeMs;
            if (!isActive) return <span key={token.fromMs} style={{ color: baseTextColor }}>{token.text}</span>;

            const progress = (absoluteTimeMs - token.fromMs) / Math.max(1, token.toMs - token.fromMs);
            const activeTransform = getActiveTokenTransform(effectType, progress);
            const activeStyle: React.CSSProperties = {
              color: effectStyle ? effectStyle.color : t.captionActiveColor,
              textShadow: effectStyle ? effectStyle.shadow : makeActiveShadow(t),
              display: activeTransform || effectStyle?.block ? "inline-block" : undefined,
              transform: activeTransform,
              transformOrigin: "center bottom",
              willChange: activeTransform ? "transform" : undefined,
            };

            if (effectStyle?.block) {
              activeStyle.background = effectStyle.background;
              activeStyle.border = effectStyle.border;
              activeStyle.borderRadius = effectStyle.borderRadius;
              activeStyle.padding = effectStyle.padding;
              activeStyle.boxDecorationBreak = "clone";
              activeStyle.WebkitBoxDecorationBreak = "clone";
            }

            return (
              <span key={token.fromMs} style={activeStyle}>
                {token.text}
              </span>
            );
          })}
        </span>
        {suffix && (
          <div style={{
            fontSize: Math.round(fontSize * 0.72),
            fontFamily: fontPreset.family,
            fontWeight: fontPreset.weight,
            color: effectStyle?.color ?? baseTextColor,
            textShadow: effectStyle?.shadow ?? TEXT_SHADOW,
            opacity: 0.88,
            lineHeight: 1.3,
            marginTop: "0.12em",
            textAlign: "center",
          }}>
            {suffix}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
