import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Sequence,
  staticFile,
  useVideoConfig,
  useCurrentFrame,
  Audio,
} from "remotion";
import { Video } from "@remotion/media";
import { createTikTokStyleCaptions } from "@remotion/captions";
import { useMemo } from "react";
import { z } from "zod";
import { CaptionPage } from "./CaptionPage";
import { makeCaptions } from "./utils";
import studioData from "./studioData.json";
import popSound from "../public/Onoma-Pop04.mp3";

// ── Schema ────────────────────────────────────────────────────────────────────

const captionSchema = z.object({
  text: z.string().describe("字幕テキスト（先頭スペースでページ区切り）"),
  startMs: z.number().describe("開始時間 ms（クリップ先頭基準）"),
  endMs: z.number().describe("終了時間 ms（クリップ先頭基準）"),
});

export const clipSchema = z.object({
  videoSrc: z.string().describe("動画ファイル名"),
  startSec: z.number().min(0).describe("クリップ開始秒"),
  endSec: z.number().min(0).describe("クリップ終了秒"),
  vertical: z.boolean().describe("縦動画 9:16"),
  cropX: z.number().min(0).max(100).describe("顔の位置 % (0=左端 50=中央 100=右端) ※縦動画の顔 cam に使用"),
  title: z.string().describe("クリップタイトル（縦動画上部に常時表示）"),
  captions: z.array(captionSchema).describe("字幕リスト"),
});

export type ClipProps = z.infer<typeof clipSchema>;

export const defaultClipProps: ClipProps = {
  videoSrc: "n7gHr7vBc08_first15min.mp4",
  startSec: 0,
  endSec: 10,
  vertical: false,
  cropX: 85,
  title: "",
  captions: [],
};

// ── Metadata ──────────────────────────────────────────────────────────────────

export const calculateMetadata: CalculateMetadataFunction<ClipProps> = ({
  props,
}) => {
  const fps = 30;
  return {
    durationInFrames: Math.max(
      1,
      Math.round((props.endSec - props.startSec) * fps),
    ),
    fps,
    width: props.vertical ? 1080 : 1920,
    height: props.vertical ? 1920 : 1080,
  };
};

// ── Shared constants ──────────────────────────────────────────────────────────

const COMBINE_TOKENS_MS = 0;
const SAFE_AREA_TOP = 40;
const VERTICAL_PADDING = 12;
const FACE_CAM_SIZE = 560;
const FACE_CAM_BORDER = 8;
const GOOGLE_FONT = `@import url('https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@700;900&display=swap');`;

type StudioSegment = { start?: number; end?: number; text?: string };

// ── Title bar height calculation ──────────────────────────────────────────────

function calcTitleBar(title: string, containerWidth: number) {
  if (!title) return { titleBarHeight: 0, titleFontSize: 0 };

  const lines = title.split("\n");
  const longestLineLength = lines.reduce((max, l) => Math.max(max, l.length), 0);
  const fontSizeBasedOnWidth =
    longestLineLength > 0 ? (containerWidth * 0.93) / longestLineLength : 110;
  const fontSizeBasedOnTotal = 2000 / title.length;
  const titleFontSize = Math.min(
    110,
    Math.floor(fontSizeBasedOnWidth),
    Math.floor(fontSizeBasedOnTotal),
  );

  const autoWrapLines = Math.ceil((title.length * titleFontSize) / (containerWidth * 0.93));
  const estimatedLines = Math.max(lines.length, autoWrapLines);
  const titleBarHeight = Math.round(
    titleFontSize * 1.3 * estimatedLines + VERTICAL_PADDING * 2 + SAFE_AREA_TOP,
  );

  return { titleBarHeight, titleFontSize };
}

// ── Caption page renderer (shared timing logic) ───────────────────────────────

function renderCaptionPages(
  pages: ReturnType<typeof createTikTokStyleCaptions>["pages"],
  fps: number,
  paddingBottomOverride?: number,
) {
  return pages.map((page, i) => {
    const startFrame = Math.round((page.startMs / 1000) * fps);
    const lastToken = page.tokens[page.tokens.length - 1];
    const endFrame = lastToken
      ? Math.round((lastToken.toMs / 1000) * fps)
      : startFrame + fps;
    const durationInFrames = endFrame - startFrame;
    if (durationInFrames <= 0) return null;
    return (
      <Sequence key={i} from={startFrame} durationInFrames={durationInFrames}>
        <CaptionPage page={page} paddingBottomOverride={paddingBottomOverride} />
      </Sequence>
    );
  });
}

// ── Component ─────────────────────────────────────────────────────────────────

export const ClipComposition: React.FC<ClipProps> = ({
  videoSrc,
  startSec,
  endSec,
  vertical,
  cropX,
  title,
  captions,
}) => {
  const { fps, width, height } = useVideoConfig();
  const isVertical = height > width;

  const effectiveCaptions = useMemo(() => {
    if (captions.length > 0) return captions;
    const segs = (studioData as { segments: StudioSegment[] }).segments;
    return makeCaptions(segs, startSec, endSec);
  }, [captions, startSec, endSec]);

  const { pages } = useMemo(
    () =>
      createTikTokStyleCaptions({
        captions: effectiveCaptions.map((c) => ({
          ...c,
          timestampMs: null,
          confidence: null,
        })),
        combineTokensWithinMilliseconds: COMBINE_TOKENS_MS,
      }),
    [effectiveCaptions],
  );

  const trimBefore = startSec * fps;

  // ── 縦動画レイアウト ────────────────────────────────────────────────────────
  if (isVertical) {
    const { titleBarHeight, titleFontSize } = calcTitleBar(title, width);

    // 横動画 (16:9) をそのまま全幅表示した高さ
    const mainVideoH = Math.round(width * (9 / 16));

    // 顔 cam をボトムエリアの中央に配置
    const bottomTop = titleBarHeight + mainVideoH;
    const bottomH = height - bottomTop;
    const faceTop = Math.round(bottomTop + (bottomH - FACE_CAM_SIZE) / 2);
    const faceLeft = Math.round((width - FACE_CAM_SIZE) / 2);

    // 字幕: メイン動画の下端に合わせる
    const captionPaddingBottom = bottomH + 32;

    return (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        <style>{GOOGLE_FONT}</style>

        {/* ── 背景: ぼかした動画 ── */}
        <AbsoluteFill>
          <Video
            src={staticFile(videoSrc)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              filter: "blur(50px)",
              opacity: 0.45,
              transform: "scale(1.12)",
            }}
            trimBefore={trimBefore}
          />
        </AbsoluteFill>

        {/* ── メイン動画 (16:9 全幅) ── */}
        <div
          style={{
            position: "absolute",
            top: titleBarHeight,
            left: 0,
            width,
            height: mainVideoH,
            overflow: "hidden",
            boxShadow: "0 6px 40px rgba(0,0,0,0.6)",
          }}
        >
          <Video
            src={staticFile(videoSrc)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
            trimBefore={trimBefore}
          />
        </div>

        {/* ── タイトルバー ── */}
        {title && (
          <AbsoluteFill
            style={{
              justifyContent: "flex-start",
              alignItems: "center",
              pointerEvents: "none",
            }}
          >
            <Audio src={popSound} />
            <div
              style={{
                width: "100%",
                background:
                  "linear-gradient(to right, #FF1493 0%, #B200FF 100%)",
                padding: `${SAFE_AREA_TOP + VERTICAL_PADDING}px 20px ${VERTICAL_PADDING}px`,
                textAlign: "center",
                boxShadow: "0 15px 30px rgba(178,0,255,0.5)",
              }}
            >
              <div
                style={{
                  fontFamily:
                    '"Zen Maru Gothic", "Hiragino Maru Gothic ProN", sans-serif',
                  fontSize: titleFontSize,
                  fontWeight: 900,
                  color: "#FFFFFF",
                  WebkitTextStroke: "5px #4A0E4E",
                  textShadow: "8px 8px 0px #4A0E4E",
                  lineHeight: 1.2,
                  whiteSpace: "pre-wrap",
                }}
              >
                {title.split("\n").map((line, i) => (
                  <div key={i}>{line}</div>
                ))}
              </div>
            </div>
          </AbsoluteFill>
        )}

        {/* ── 顔 cam (クリップ円形) ── */}
        <div
          style={{
            position: "absolute",
            top: faceTop,
            left: faceLeft,
            width: FACE_CAM_SIZE,
            height: FACE_CAM_SIZE,
            borderRadius: "50%",
            overflow: "hidden",
            // グラデーションボーダー (疑似的に外側のdivで実現)
            boxShadow: [
              `0 0 0 ${FACE_CAM_BORDER}px rgba(255,255,255,0.85)`,
              "0 0 0 12px rgba(255,20,147,0.35)",
              "0 12px 48px rgba(0,0,0,0.65)",
            ].join(", "),
          }}
        >
          <Video
            src={staticFile(videoSrc)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              // cropX% の位置を顔 cam の中央に映す
              objectPosition: `${cropX}% center`,
            }}
            trimBefore={trimBefore}
          />
        </div>

        {/* ── 字幕 (メイン動画下部にオーバーレイ) ── */}
        <AbsoluteFill>
          {renderCaptionPages(pages, fps, captionPaddingBottom)}
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  // ── 横動画レイアウト (変更なし) ──────────────────────────────────────────────
  return (
    <AbsoluteFill style={{ backgroundColor: "#111" }}>
      <style>{GOOGLE_FONT}</style>
      <Video
        src={staticFile(videoSrc)}
        style={{ width: "100%", height: "100%", objectFit: "contain" }}
        trimBefore={trimBefore}
      />
      <AbsoluteFill>
        {renderCaptionPages(pages, fps)}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
