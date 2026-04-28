import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import type { TikTokPage } from "@remotion/captions";

const OUTLINE_COLOR = "#000000"; // 白抜き用の黒縁
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

const ACTIVE_COLOR = "#fe27c8ff"; // 読み上げる箇所はピンク (DeepPink)
const ACTIVE_OUTLINE = "#FFFFFF"; // ピンクを引き立てる白縁
const ACTIVE_SHADOW = [
  `-3px -3px 0 ${ACTIVE_OUTLINE}`,
  ` 3px -3px 0 ${ACTIVE_OUTLINE}`,
  `-3px  3px 0 ${ACTIVE_OUTLINE}`,
  ` 3px  3px 0 ${ACTIVE_OUTLINE}`,
  ` 0px  3px 0 ${ACTIVE_OUTLINE}`,
  ` 0px -3px 0 ${ACTIVE_OUTLINE}`,
  ` 3px  0px 0 ${ACTIVE_OUTLINE}`,
  `-3px  0px 0 ${ACTIVE_OUTLINE}`,
  "5px 5px 0px #4A0E4E", // 外側の濃い紫の影（立体感用）
  "0 0 15px rgba(254, 39, 200, 0.5)", // 柔らかなピンクの光
].join(",");

export const CaptionPage: React.FC<{ page: TikTokPage }> = ({ page }) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const isVertical = height > width;

  const currentTimeMs = (frame / fps) * 1000;
  const absoluteTimeMs = page.startMs + currentTimeMs;

  const fontSize = isVertical ? 86 : 64;
  const paddingBottom = isVertical ? 160 : 64;

  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom,
      }}
    >
      <div
        style={{
          padding: isVertical ? "16px 30px" : "10px 22px",
          maxWidth: isVertical ? "90%" : "84%",
          textAlign: "center",
        }}
      >
        <span
          style={{
            fontFamily:
              '"Zen Maru Gothic", "Hiragino Maru Gothic ProN", sans-serif',
            fontSize,
            fontWeight: "900",
            color: "#FFFFFF",
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
                    ? { color: ACTIVE_COLOR, textShadow: ACTIVE_SHADOW }
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
