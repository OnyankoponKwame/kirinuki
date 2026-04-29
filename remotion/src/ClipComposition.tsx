import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
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

const keepIntervalSchema = z.object({
  startSec: z.number().describe("区間開始秒（動画絶対時間）"),
  endSec: z.number().describe("区間終了秒（動画絶対時間）"),
});

export const clipSchema = z.object({
  videoSrc: z.string().describe("動画ファイル名"),
  startSec: z.number().min(0).describe("クリップ開始秒"),
  endSec: z.number().min(0).describe("クリップ終了秒"),
  vertical: z.boolean().describe("縦動画 9:16"),
  verticalMode: z
    .enum(["crop", "split"])
    .optional()
    .describe("縦動画レイアウト: crop=人物クロップ, split=全体+顔cam二段構成"),
  cropX: z
    .number()
    .min(0)
    .max(100)
    .describe("顔の位置 % (0=左端 50=中央 100=右端)"),
  faceCamZoom: z
    .number()
    .min(0.5)
    .max(8)
    .optional()
    .describe("顔 cam ズーム率（split モード用、デフォルト 1.5）"),
  faceCamY: z
    .number()
    .min(0)
    .max(100)
    .optional()
    .describe("顔 cam 縦位置 % (0=上端 50=中央 100=下端、デフォルト 50)"),
  title: z.string().describe("クリップタイトル（縦動画上部に常時表示）"),
  captions: z.array(captionSchema).describe("字幕リスト"),
  keepIntervals: z
    .array(keepIntervalSchema)
    .optional()
    .describe("有音区間リスト（無音カット用）"),
  srcAspect: z
    .number()
    .min(0.1)
    .max(10)
    .optional()
    .describe("ソース動画アスペクト比（幅÷高さ、デフォルト 16/9）"),
});

export type ClipProps = z.infer<typeof clipSchema>;

export const defaultClipProps: ClipProps = {
  videoSrc: "n7gHr7vBc08_first15min.mp4",
  startSec: 0,
  endSec: 10,
  vertical: false,
  verticalMode: "crop",
  cropX: 85,
  faceCamZoom: 1.5,
  faceCamY: 50,
  title: "",
  captions: [],
};

// ── Metadata ──────────────────────────────────────────────────────────────────

export const calculateMetadata: CalculateMetadataFunction<ClipProps> = ({
  props,
}) => {
  const fps = 30;
  const durationSec = props.keepIntervals?.length
    ? props.keepIntervals.reduce((sum, iv) => sum + (iv.endSec - iv.startSec), 0)
    : props.endSec - props.startSec;
  return {
    durationInFrames: Math.max(1, Math.round(durationSec * fps)),
    fps,
    width: props.vertical ? 1080 : 1920,
    height: props.vertical ? 1920 : 1080,
  };
};

// ── Constants ─────────────────────────────────────────────────────────────────

const COMBINE_TOKENS_MS = 0;
const SAFE_AREA_TOP = 40;
const VERTICAL_PADDING = 12;
// クロップモードの字幕下マージン（概要欄オーバーレイを避けるため大きめに）
const CROP_CAPTION_PADDING_BOTTOM = 260;
const DEFAULT_SRC_ASPECT = 16 / 9;

const GOOGLE_FONT = `@import url('https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@700;900&display=swap');`;

type StudioSegment = { start?: number; end?: number; text?: string };

// ── Jump cut helper (Remotion snippet approach) ───────────────────────────────

function computeTrimBefore(
  frame: number,
  intervals: { startSec: number; endSec: number }[],
  fps: number,
): number {
  let summedUpDurations = 0;
  for (const iv of intervals) {
    const sectionFrames = Math.round((iv.endSec - iv.startSec) * fps);
    summedUpDurations += sectionFrames;
    if (summedUpDurations > frame) {
      // trimBefore so that (trimBefore + compositionFrame) = correct video frame
      return Math.round(iv.endSec * fps) - summedUpDurations;
    }
  }
  // Past the end — hold at last frame
  const last = intervals[intervals.length - 1];
  return Math.round(last.startSec * fps);
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function calcTitleBar(title: string, containerWidth: number) {
  if (!title) return { titleBarHeight: 0, titleFontSize: 0 };
  const lines = title.split("\n");
  const longestLen = lines.reduce((m, l) => Math.max(m, l.length), 0);
  // Single-line titles: treat as 2-line baseline so font wraps rather than spanning full width
  const charsPerLine = lines.length >= 2 ? longestLen : title.length / 2;
  const fsByWidth = charsPerLine > 0 ? (containerWidth * 0.93) / charsPerLine : 110;
  const fsByTotal = 2000 / title.length;
  const titleFontSize = Math.min(110, Math.floor(fsByWidth), Math.floor(fsByTotal));
  const autoWrapLines = Math.ceil((title.length * titleFontSize) / (containerWidth * 0.93));
  const estimatedLines = Math.max(lines.length, autoWrapLines, 2);
  const titleBarHeight = Math.round(
    titleFontSize * 1.3 * estimatedLines + VERTICAL_PADDING * 2 + SAFE_AREA_TOP,
  );
  return { titleBarHeight, titleFontSize };
}

function renderCaptionPages(
  pages: ReturnType<typeof createTikTokStyleCaptions>["pages"],
  fps: number,
  options?: { paddingBottomOverride?: number; topOffset?: number },
) {
  return pages.map((page, i) => {
    const startFrame = Math.round((page.startMs / 1000) * fps);
    const lastToken = page.tokens[page.tokens.length - 1];
    const endFrame = lastToken
      ? Math.round((lastToken.toMs / 1000) * fps)
      : startFrame + fps;
    const dur = endFrame - startFrame;
    if (dur <= 0) return null;
    return (
      <Sequence key={i} from={startFrame} durationInFrames={dur}>
        <CaptionPage
          page={page}
          paddingBottomOverride={options?.paddingBottomOverride}
          topOffset={options?.topOffset}
        />
      </Sequence>
    );
  });
}

function TitleBar({
  title,
  titleFontSize,
  titleBarHeight,
}: {
  title: string;
  titleFontSize: number;
  titleBarHeight: number;
}) {
  return (
    <AbsoluteFill
      style={{ justifyContent: "flex-start", alignItems: "center", pointerEvents: "none" }}
    >
      <Audio src={popSound} />
      <div
        style={{
          width: "100%",
          height: titleBarHeight,
          background: "linear-gradient(to right, #FF1493 0%, #B200FF 100%)",
          padding: `${SAFE_AREA_TOP + VERTICAL_PADDING}px 20px ${VERTICAL_PADDING}px`,
          textAlign: "center",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxSizing: "border-box",
        }}
      >
        <div
          style={{
            fontFamily: '"Zen Maru Gothic", "Hiragino Maru Gothic ProN", sans-serif',
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
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

export const ClipComposition: React.FC<ClipProps> = ({
  videoSrc,
  startSec,
  endSec,
  vertical,
  verticalMode = "crop",
  cropX,
  faceCamZoom = 1.5,
  faceCamY = 50,
  title,
  captions,
  keepIntervals,
  srcAspect,
}) => {
  const SRC_ASPECT = srcAspect ?? DEFAULT_SRC_ASPECT;

  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const isVertical = height > width;

  // Normalise intervals: when keepIntervals is absent, treat the whole clip as one interval
  const intervals = useMemo(
    () => (keepIntervals?.length ? keepIntervals : [{ startSec, endSec }]),
    [keepIntervals, startSec, endSec],
  );

  // Jump cut: single trimBefore value for the current frame (Remotion snippet approach)
  const trimBefore = useMemo(
    () => computeTrimBefore(frame, intervals, fps),
    [frame, intervals, fps],
  );

  // Raw captions are always relative to startSec (full clip range)
  const rawCaptions = useMemo(() => {
    if (captions.length > 0) return captions;
    const segs = (studioData as { segments: StudioSegment[] }).segments;
    return makeCaptions(segs, startSec, endSec);
  }, [captions, startSec, endSec]);

  // Remap captions onto the output timeline when keepIntervals collapses silent gaps
  const effectiveCaptions = useMemo(() => {
    if (intervals.length <= 1) return rawCaptions;
    const result: typeof rawCaptions = [];
    let outputOffsetMs = 0;
    for (const iv of intervals) {
      const ivStartMs = (iv.startSec - startSec) * 1000;
      const ivEndMs = (iv.endSec - startSec) * 1000;
      for (const cap of rawCaptions) {
        if (cap.endMs <= ivStartMs || cap.startMs >= ivEndMs) continue;
        result.push({
          ...cap,
          startMs: outputOffsetMs + Math.max(0, cap.startMs - ivStartMs),
          endMs: outputOffsetMs + Math.min(ivEndMs - ivStartMs, cap.endMs - ivStartMs),
        });
      }
      outputOffsetMs += ivEndMs - ivStartMs;
    }
    return result;
  }, [rawCaptions, intervals, startSec]);

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

  const { titleBarHeight, titleFontSize } = calcTitleBar(title, width);

  // ── 横動画 ────────────────────────────────────────────────────────────────────
  if (!isVertical) {
    return (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        <style>{GOOGLE_FONT}</style>
        <Video
          src={staticFile(videoSrc)}
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
          trimBefore={trimBefore}
        />
        <AbsoluteFill>{renderCaptionPages(pages, fps)}</AbsoluteFill>
      </AbsoluteFill>
    );
  }

  // ── 縦動画: クロップモード ────────────────────────────────────────────────────
  // 動画をフレーム全体に広げ、タイトルバーは上端にオーバーレイ（アスペクト比を保持）
  if (verticalMode === "crop") {
    // タイトルバーを動画の上に重ねるため topOffset は 0 — 動画はフル高さを使う
    const scaledW = Math.round(height * SRC_ASPECT);
    const leftOffset = -Math.round((scaledW - width) * (cropX / 100));

    return (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        <style>{GOOGLE_FONT}</style>

        {/* 背景: ぼかし動画 */}
        <AbsoluteFill>
          <Video
            src={staticFile(videoSrc)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              filter: "blur(40px)",
              opacity: 0.5,
              transform: "scale(1.1)",
            }}
            trimBefore={trimBefore}
          />
        </AbsoluteFill>

        {/* メイン動画: クロップ（フル高さ） */}
        <div style={{ width: "100%", height: "100%", overflow: "hidden", position: "relative" }}>
          <Video
            src={staticFile(videoSrc)}
            style={{
              position: "absolute",
              width: scaledW,
              height,
              top: 0,
              left: leftOffset,
            }}
            trimBefore={trimBefore}
          />
        </div>

        {/* タイトルバー: 動画の上にオーバーレイ */}
        {title && <TitleBar title={title} titleFontSize={titleFontSize} titleBarHeight={titleBarHeight} />}

        <AbsoluteFill>
          {renderCaptionPages(pages, fps, { paddingBottomOverride: CROP_CAPTION_PADDING_BOTTOM })}
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  // ── 縦動画: 二段構成モード ────────────────────────────────────────────────────
  // 上段: 16:9 動画そのまま全幅
  // 中段: 字幕ストリップ
  // 下段: cropX% を中心にズームした顔 cam 円形（最大サイズ）
  const mainVideoH = Math.round(width * (9 / 16)); // 1080 × 9/16 = 607
  const bottomTop = titleBarHeight + mainVideoH;
  const bottomH = height - bottomTop;

  // 顔 cam サイズ: ボトムエリア全体を使用（字幕はオーバーレイ）
  const faceCamSize = Math.min(bottomH - 8, width - 8);
  const faceLeft = Math.round((width - faceCamSize) / 2);
  const faceTop = bottomTop + Math.round((bottomH - faceCamSize) / 2);

  // 顔 cam 内部の動画: faceCamZoom 率で縦横拡大
  const faceCamInnerH = Math.round(faceCamSize * faceCamZoom);
  const faceCamInnerW = Math.round(faceCamInnerH * (16 / 9));
  // faceCamY% の行が円の中央に来るよう縦オフセット
  const faceCamVideoTop = -Math.round((faceCamInnerH - faceCamSize) * (faceCamY / 100));
  // cropX% の位置を顔 cam 横中央に揃える
  const rawFaceLeft = Math.round(faceCamSize / 2 - faceCamInnerW * (cropX / 100));
  const faceCamVideoLeft = Math.max(-(faceCamInnerW - faceCamSize), Math.min(0, rawFaceLeft));

  // 字幕: 顔 cam エリアの上端に上から固定（複数行でも上動画に重ならない）
  const captionTopOffset = faceTop + 24;

  return (
    <AbsoluteFill style={{ backgroundColor: "#111" }}>
      <style>{GOOGLE_FONT}</style>

      {/* 上段: 16:9 動画全幅 */}
      <div
        style={{
          position: "absolute",
          top: titleBarHeight,
          left: 0,
          width,
          height: mainVideoH,
          overflow: "hidden",
        }}
      >
        <Video
          src={staticFile(videoSrc)}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
          trimBefore={trimBefore}
        />
      </div>

      {/* タイトルバー */}
      {title && <TitleBar title={title} titleFontSize={titleFontSize} titleBarHeight={titleBarHeight} />}

      {/* 下段: 顔 cam (絶対配置でズーム) */}
      <div
        style={{
          position: "absolute",
          top: faceTop,
          left: faceLeft,
          width: faceCamSize,
          height: faceCamSize,
          overflow: "hidden",
          boxShadow: "0 12px 48px rgba(0,0,0,0.65)",
        }}
      >
        {/* position:relative なコンテナの中で Video を絶対配置してズーム */}
        <div style={{ position: "relative", width: faceCamSize, height: faceCamSize }}>
          <Video
            src={staticFile(videoSrc)}
            style={{
              position: "absolute",
              width: faceCamInnerW,
              height: faceCamInnerH,
              top: faceCamVideoTop,
              left: faceCamVideoLeft,
            }}
            trimBefore={trimBefore}
          />
        </div>
      </div>

      {/* 字幕: 顔 cam エリア上端からオーバーレイ */}
      <AbsoluteFill>
        {renderCaptionPages(pages, fps, { topOffset: captionTopOffset })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
