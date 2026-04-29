import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import type { TikTokPage } from "@remotion/captions";
import { THEMES, DEFAULT_THEME_KEY } from "./themes";
import type { ClipTheme } from "./themes";

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

function makeActiveShadow(theme: ClipTheme): string {
  return [
    `-3px -3px 0 #FFFFFF`,
    ` 3px -3px 0 #FFFFFF`,
    `-3px  3px 0 #FFFFFF`,
    ` 3px  3px 0 #FFFFFF`,
    ` 0px  3px 0 #FFFFFF`,
    ` 0px -3px 0 #FFFFFF`,
    ` 3px  0px 0 #FFFFFF`,
    `-3px  0px 0 #FFFFFF`,
    `5px 5px 0px ${theme.titleAccentColor}`,
    `0 0 15px ${theme.captionActiveGlow}`,
  ].join(",");
}

export const CaptionPage: React.FC<{
  page: TikTokPage;
  paddingBottomOverride?: number;
  topOffset?: number;
  captionFontSize?: number;
  theme?: ClipTheme;
}> = ({ page, paddingBottomOverride, topOffset, captionFontSize, theme }) => {
  const t = theme ?? THEMES[DEFAULT_THEME_KEY];
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const isVertical = height > width;

  const currentTimeMs = (frame / fps) * 1000;
  const absoluteTimeMs = page.startMs + currentTimeMs;

  const fontSize = captionFontSize ?? (isVertical ? 96 : 72);
  const paddingBottom = paddingBottomOverride ?? (isVertical ? 160 : 64);

  return (
    <AbsoluteFill
      style={
        topOffset !== undefined
          ? { justifyContent: "flex-start", alignItems: "center", paddingTop: topOffset }
          : { justifyContent: "flex-end", alignItems: "center", paddingBottom }
      }
    >
      <div
        style={{
          padding: isVertical ? "16px 20px" : "10px 16px",
          maxWidth: isVertical ? "96%" : "92%",
          textAlign: "center",
        }}
      >
        <span
          style={{
            fontFamily:
              '"Zen Maru Gothic", "Hiragino Maru Gothic ProN", sans-serif',
            fontSize,
            fontWeight: "900",
            color: t.captionTextColor,
            textShadow: TEXT_SHADOW,
            whiteSpace: "pre-wrap",
            lineHeight: 1.55,
          }}
        >
          {page.tokens.map((token) => {
            const isActive =
              token.fromMs <= absoluteTimeMs && token.toMs > absoluteTimeMs;
            return (
              <span
                key={token.fromMs}
                style={
                  isActive
                    ? { color: t.captionActiveColor, textShadow: makeActiveShadow(t) }
                    : {}
                }
              >
                {token.text}
              </span>
            );
          })}
        </span>
      </div>
    </AbsoluteFill>
  );
};
