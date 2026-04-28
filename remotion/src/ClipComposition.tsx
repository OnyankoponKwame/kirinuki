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
  cropX: z.number().min(0).max(100).describe("横クロップ位置 % (0=左端 50=中央 100=右端) ※縦動画のみ有効"),
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

// ── Metadata — drives output resolution ───────────────────────────────────────

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

// ── Component ─────────────────────────────────────────────────────────────────

const COMBINE_TOKENS_MS = 0;

type StudioSegment = { start?: number; end?: number; text?: string };

const TITLE_OUTLINE = [
  "-1px -1px 0 rgba(0,0,0,0.75)",
  " 1px -1px 0 rgba(0,0,0,0.75)",
  "-1px  1px 0 rgba(0,0,0,0.75)",
  " 1px  1px 0 rgba(0,0,0,0.75)",
].join(",");

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

  const clipDurationFrames = Math.round((endSec - startSec) * fps);

  const lines = title ? title.split("\n") : [];
  const manualLines = lines.length;
  const longestLineLength = lines.reduce((max, l) => Math.max(max, l.length), 0);
  
  // 1行あたり約1000pxの幅に収まるよう計算（longestLineLength基準）
  const fontSizeBasedOnWidth = longestLineLength > 0 ? 1000 / longestLineLength : 110;
  // 全体で2行分（2000px）に収まるよう計算（改行なしの場合の縮小用）
  const fontSizeBasedOnTotal = title ? 2000 / title.length : 110;

  // 両方の制約を満たす最小のサイズを採用（最大110px）
  const titleFontSize = Math.min(110, Math.floor(fontSizeBasedOnWidth), Math.floor(fontSizeBasedOnTotal));

  // タイトルバーの実際の高さを推定し、隙間ができないように動画を下げる
  const SAFE_AREA_TOP = 40; // 上部の余白を狭める
  const VERTICAL_PADDING = 12; // 上下のパディングを狭める
  
  // 推定される最終的な行数（手動改行数 vs 自動折り返し想定数）
  const autoWrapLines = title ? Math.ceil((title.length * titleFontSize) / 1000) : 0;
  const estimatedLines = Math.max(manualLines, autoWrapLines);
  
  const titleBarHeight = title ? Math.round(titleFontSize * 1.3 * estimatedLines + (VERTICAL_PADDING * 2)) : 0;
  
  // 動画のオフセット（セーフエリア＋タイトルバー高さ）
  // タイトルバー全体（背景色部分）の高さは SAFE_AREA_TOP + titleBarHeight
  const topOffset = isVertical && title ? SAFE_AREA_TOP + titleBarHeight - 10 : 0;
  const videoHeight = height - topOffset;

  // For vertical: scale the 16:9 source so its height fills the videoHeight,
  // then shift horizontally to crop the desired column.
  const scaledVideoWidth = Math.round(videoHeight * (16 / 9));
  const leftOffset = -Math.round((scaledVideoWidth - width) * (cropX / 100));

  return (
    <AbsoluteFill style={{ backgroundColor: "#111" }}>
      {/* 縦動画で隙間ができる場合、背景にぼかした動画を敷いてリッチにする */}
      {isVertical && (
        <AbsoluteFill>
          <Video
            src={staticFile(videoSrc)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              filter: "blur(40px)",
              opacity: 0.5,
              transform: "scale(1.1)", // 端のぼかし漏れを防ぐ
            }}
            trimBefore={startSec * fps}
          />
        </AbsoluteFill>
      )}
      <div
        style={{
          width: "100%",
          height: "100%",
          overflow: isVertical ? "hidden" : "visible",
          position: "relative",
        }}
      >
        <Video
          src={staticFile(videoSrc)}
          style={
            isVertical
              ? {
                position: "absolute",
                width: scaledVideoWidth,
                height: videoHeight,
                top: topOffset,
                left: leftOffset,
                boxShadow: "0 -4px 30px rgba(0,0,0,0.5)", // 動画の上に影をつけて立体感を出す
              }
              : { width: "100%", height: "100%", objectFit: "contain" }
          }
          trimBefore={startSec * fps}
        />
      </div>
      <style>
        {`
          @import url('https://fonts.googleapis.com/css2?family=Zen+Maru+Gothic:wght@700;900&display=swap');
        `}
      </style>
      {isVertical && title && (
        <>
          <Audio src={popSound} />
          <AbsoluteFill
            style={{
              justifyContent: "flex-start", // 上寄せ
              alignItems: "center",
              pointerEvents: "none",
            }}
          >
            <div
              style={{
                width: "100%",
                background: "linear-gradient(to right, #FF1493 0%, #B200FF 100%)",
                padding: `${SAFE_AREA_TOP + VERTICAL_PADDING}px 20px ${VERTICAL_PADDING}px`,
                textAlign: "center",
                boxShadow: "0 15px 30px rgba(178, 0, 255, 0.5)",
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
                  display: "block",
                  width: "100%",
                }}
              >
                {title.split("\n").map((line, i) => (
                  <div key={i}>{line}</div>
                ))}
              </div>
            </div>
          </AbsoluteFill>
        </>
      )}
      <AbsoluteFill>
        {pages.map((page, i) => {
          const startFrame = Math.round((page.startMs / 1000) * fps);
          const lastToken = page.tokens[page.tokens.length - 1];
          
          // 前の字幕が残り続けないよう、次の字幕の開始時刻ではなく、この字幕の「発話終了時刻」を終了フレームにする
          // 少しだけ余韻を残したい場合は + 10 フレーム程度しても良いが、一旦ピッタリ消えるようにする
          const endFrame = lastToken 
            ? Math.round((lastToken.toMs / 1000) * fps)
            : startFrame + fps; // フォールバック
            
          const durationInFrames = endFrame - startFrame;
          if (durationInFrames <= 0) return null;

          return (
            <Sequence key={i} from={startFrame} durationInFrames={durationInFrames}>
              <CaptionPage page={page} />
            </Sequence>
          );
        })}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
