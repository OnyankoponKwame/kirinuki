import {
  AbsoluteFill,
  CalculateMetadataFunction,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  Audio,
  delayRender,
  continueRender,
  getRemotionEnvironment,
  Solid,
} from "remotion";
import { Video } from "@remotion/media";
import { createTikTokStyleCaptions } from "@remotion/captions";
import { useMemo, useState, useEffect } from "react";
import { z } from "zod";
import { CaptionPage } from "./CaptionPage";
import { makeCaptions } from "./utils";
import { THEMES, DEFAULT_THEME_KEY } from "./themes";
import type { ClipTheme } from "./themes";
import {
  CAPTION_EFFECTS,
  CAPTION_FONT_KEYS,
  CAPTION_FONT_PRESETS,
  type CaptionEffect,
} from "./captionStyles";
import studioData from "./studioData.json";

const popSound = staticFile("Onoma-Pop04.mp3");

// ── Schema ────────────────────────────────────────────────────────────────────

const captionSchema = z.object({
  text: z.string().describe("字幕テキスト（先頭スペースでページ区切り）"),
  startMs: z.number().describe("開始時間 ms（クリップ先頭基準）"),
  endMs: z.number().describe("終了時間 ms（クリップ先頭基準）"),
  effect: z.enum([...CAPTION_EFFECTS, "emphasis"])
    .optional()
    .describe("エフェクト種別（emphasis は旧データ互換のみ）"),
  isComment: z
    .boolean()
    .default(false)
    .describe("コメント風吹き出し表示（ユーザーアイコン付き）"),
});

const cutIntervalSchema = z.object({
  startSec: z.number().describe("カット開始秒（動画絶対時間）"),
  endSec: z.number().describe("カット終了秒（動画絶対時間）"),
});

const themeColorsSchema = z.object({
  label: z.string(),
  titleBackground: z.string(),
  titleTextColor: z.string(),
  titleAccentColor: z.string(),
  captionTextColor: z.string(),
  captionActiveColor: z.string(),
  captionActiveGlow: z.string(),
  captionFont: z.enum(CAPTION_FONT_KEYS).optional(),
  titleFont: z.enum(CAPTION_FONT_KEYS).optional(),
  titleBarMinHeight: z.number().min(150).max(600).optional(),
  titleTopMargin: z.number().min(0).max(20).optional(),
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
    .describe("顔 cam ズーム率（crop: デフォルト 1.0 / split: デフォルト 1.5）"),
  faceCamY: z
    .number()
    .min(0)
    .max(100)
    .optional()
    .describe("顔 cam 縦位置 % (0=上端 50=中央 100=下端、デフォルト 50)"),
  mainZoom: z
    .number()
    .min(0.5)
    .max(8)
    .optional()
    .describe("split上段ズーム率（デフォルト 1.0）"),
  mainCropX: z
    .number()
    .min(0)
    .max(100)
    .optional()
    .describe("split上段水平位置 % (0=左端 50=中央 100=右端、デフォルト 50)"),
  mainCropY: z
    .number()
    .min(0)
    .max(100)
    .optional()
    .describe("split上段垂直位置 % (0=上端 50=中央 100=下端、デフォルト 50)"),
  splitTopRatio: z
    .number()
    .min(1)
    .max(9)
    .optional()
    .describe("二段構成の上段高さ割合 (1〜9、0.5刻み、下段は 10-この値、デフォルト 4.5)"),
  title: z.string().describe("クリップタイトル（縦動画上部に常時表示）"),
  captions: z.array(captionSchema).describe("字幕リスト"),
  cutIntervals: z
    .array(cutIntervalSchema)
    .optional()
    .describe("カット区間リスト（指定区間を動画から除去する）"),
  srcAspect: z
    .number()
    .min(0.1)
    .max(10)
    .optional()
    .describe("ソース動画アスペクト比（幅÷高さ、デフォルト 16/9）"),
  captionFontSize: z
    .number()
    .min(24)
    .max(200)
    .optional()
    .describe("字幕フォントサイズ（px）"),
  captionFont: z
    .enum(CAPTION_FONT_KEYS)
    .optional()
    .describe("字幕フォントプリセット"),
  captionEffect: z
    .enum(CAPTION_EFFECTS)
    .optional()
    .describe("クリップ提案時に選ばれた字幕基本エフェクト"),
  theme: z.string().optional().describe("カラーテーマ名"),
  themeColors: themeColorsSchema
    .optional()
    .describe("カラーテーマの色定義を直接指定（カスタムテーマ用。指定時は theme キーより優先）"),
});

export type ClipProps = z.infer<typeof clipSchema>;
type EffectRange = { startMs: number; endMs: number; type: string };

// ── Metadata ──────────────────────────────────────────────────────────────────

export const calculateMetadata: CalculateMetadataFunction<ClipProps> = ({
  props,
}) => {
  const fps = 30;
  const totalSec = props.endSec - props.startSec;
  const cutSec = props.cutIntervals?.length
    ? props.cutIntervals.reduce((sum, iv) => {
        const dur = iv.endSec - iv.startSec;
        return sum + (Number.isFinite(dur) && dur > 0 ? dur : 0);
      }, 0)
    : 0;
  const durationSec = Math.max(0.1, totalSec - cutSec);
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
const TITLE_H_PADDING = 4;
const CROP_CAPTION_PADDING_BOTTOM = 260;
const DEFAULT_SRC_ASPECT = 16 / 9;

const GOOGLE_FONT_URL =
  "https://fonts.googleapis.com/css2?family=BIZ+UDPGothic:wght@700&family=Dela+Gothic+One&family=Kaisei+Decol:wght@700&family=M+PLUS+1p:wght@800;900&family=M+PLUS+Rounded+1c:wght@700;900&family=Mochiy+Pop+One&family=Noto+Sans+JP:wght@700;900&family=RocknRoll+One&family=Zen+Maru+Gothic:wght@700;900&display=swap";

function useFontsReady() {
  const [handle] = useState(() => delayRender("Loading Google Fonts"));
  useEffect(() => {
    let cancelled = false;
    fetch(GOOGLE_FONT_URL)
      .then((r) => r.text())
      .then((css) => {
        if (cancelled) return;
        const style = document.createElement("style");
        style.textContent = css;
        document.head.appendChild(style);
        return Promise.all(
          Object.values(CAPTION_FONT_PRESETS).map((preset) =>
            document.fonts.load(`${preset.weight} 1em ${preset.primaryFamily}`),
          ),
        );
      })
      .catch(() => { })
      .finally(() => {
        if (!cancelled) continueRender(handle);
      });
    return () => { cancelled = true; };
  }, [handle]);
}

type StudioSegment = { start?: number; end?: number; text?: string };

// ── Panic effect helpers ──────────────────────────────────────────────────────

function getPanicIntensity(currentMs: number, ranges: EffectRange[]): number {
  const RAMP_MS = 300;
  for (const r of ranges) {
    if (r.type !== "panic" || currentMs < r.startMs || currentMs >= r.endMs) continue;
    const raw = Math.min(1, (currentMs - r.startMs) / RAMP_MS, (r.endMs - currentMs) / RAMP_MS);
    // cosine ease-in-out for smooth ramp
    return (1 - Math.cos(raw * Math.PI)) / 2;
  }
  return 0;
}

// ── Jump cut helper ───────────────────────────────────────────────────────────

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
      return Math.max(0, Math.round(iv.endSec * fps) - summedUpDurations);
    }
  }
  const last = intervals[intervals.length - 1];
  return Math.max(0, Math.round(last.startSec * fps));
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// CJK = 1.0em, ASCII (codepoint < 256) = 0.55em
function getEffectiveWidth(s: string): number {
  let w = 0;
  for (const ch of s) {
    w += (ch.codePointAt(0) ?? 0) < 256 ? 0.55 : 1.0;
  }
  return w;
}

// 1行に収まらない場合、2行分割または3行分割で最長行のemが最小になる分割点を探す
function autoSplitTitle(title: string, usableWidth: number, maxFontPx: number): string {
  if (title.includes("\n")) return title;
  const totalEm = getEffectiveWidth(title);
  if (totalEm * maxFontPx <= usableWidth) return title;

  // 2行分割の最適な分割点を探す
  let best2Idx = 1;
  let best2Max = Infinity;
  for (let i = 1; i < title.length; i++) {
    const leftEm = getEffectiveWidth(title.slice(0, i));
    const maxEm = Math.max(leftEm, totalEm - leftEm);
    if (maxEm < best2Max) {
      best2Max = maxEm;
      best2Idx = i;
    }
  }

  // 2行分割でフォントサイズを縮小せずに収まる場合、または文字数が3文字未満の場合は2行分割を採用
  if (best2Max * maxFontPx <= usableWidth || title.length < 3) {
    return `${title.slice(0, best2Idx)}\n${title.slice(best2Idx)}`;
  }

  // 2行分割でも収まらない場合は 3行分割の最適な分割点を探す
  let best3Idx1 = 1;
  let best3Idx2 = 2;
  let best3Max = Infinity;
  for (let i = 1; i < title.length - 1; i++) {
    for (let j = i + 1; j < title.length; j++) {
      const line1Em = getEffectiveWidth(title.slice(0, i));
      const line2Em = getEffectiveWidth(title.slice(i, j));
      const line3Em = getEffectiveWidth(title.slice(j));
      const maxEm = Math.max(line1Em, line2Em, line3Em);
      if (maxEm < best3Max) {
        best3Max = maxEm;
        best3Idx1 = i;
        best3Idx2 = j;
      }
    }
  }

  return `${title.slice(0, best3Idx1)}\n${title.slice(best3Idx1, best3Idx2)}\n${title.slice(best3Idx2)}`;
}

const MIN_TITLE_BAR_H = 280;

function calcTitleBar(title: string, containerWidth: number, minBarHeight: number = MIN_TITLE_BAR_H) {
  if (!title) return { titleBarHeight: 0, titleFontSize: 0 };
  const lines = title.split("\n");
  const usableWidth = containerWidth - TITLE_H_PADDING * 2;
  const effectiveLongest = lines.reduce((m, l) => Math.max(m, getEffectiveWidth(l)), 0);
  const fsByWidth = effectiveLongest > 0 ? usableWidth / effectiveLongest : 140;
  const titleFontSize = Math.min(140, Math.floor(fsByWidth));
  const autoWrapLines = Math.ceil((effectiveLongest * titleFontSize) / usableWidth);
  const estimatedLines = Math.max(lines.length, autoWrapLines, 2);
  const titleBarHeight = Math.max(
    minBarHeight,
    Math.round(titleFontSize * 1.2 * estimatedLines + VERTICAL_PADDING * 2),
  );
  return { titleBarHeight, titleFontSize };
}

function renderCaptionPages(
  pages: ReturnType<typeof createTikTokStyleCaptions>["pages"],
  fps: number,
  captions: ClipProps["captions"],
  options?: {
    paddingBottomOverride?: number;
    topOffset?: number;
    captionFontSize?: number;
    captionFont?: ClipProps["captionFont"];
    theme?: ClipTheme;
  },
) {
  return pages.map((page, i) => {
    const startFrame = Math.round((page.startMs / 1000) * fps);
    const lastToken = page.tokens[page.tokens.length - 1];
    const endFrame = lastToken
      ? Math.round((lastToken.toMs / 1000) * fps)
      : startFrame + fps;
    const dur = endFrame - startFrame;
    if (dur <= 0) return null;
    // ページ開始時刻と重なるキャプションのエフェクトを採用
    const matchCaption = captions.find(c => c.startMs <= page.startMs && c.endMs > page.startMs);
    const effect =
      matchCaption?.effect && matchCaption.effect !== "emphasis"
        ? (matchCaption.effect as CaptionEffect)
        : undefined;
    const isLastPage = i === pages.length - 1;
    const suffix = effect === "sad" && isLastPage ? "😭" : undefined;
    return (
      <Sequence
        key={i}
        from={startFrame}
        durationInFrames={dur}
        style={{
          translate: "0px -92.7px"
        }}>
        <CaptionPage
          page={page}
          paddingBottomOverride={options?.paddingBottomOverride}
          topOffset={options?.topOffset}
          captionFontSize={options?.captionFontSize}
          captionFont={options?.captionFont}
          theme={options?.theme}
          effect={effect}
          suffix={suffix}
          isComment={matchCaption?.isComment ?? false}
        />
      </Sequence>
    );
  });
}

function TitleBar({
  title,
  titleFontSize,
  titleBarHeight,
  theme,
  topOffset = 0,
}: {
  title: string;
  titleFontSize: number;
  titleBarHeight: number;
  theme: ClipTheme;
  topOffset?: number;
}) {
  const fontPreset = CAPTION_FONT_PRESETS[theme.titleFont ?? "rounded"];
  return (
    <AbsoluteFill
      style={{ justifyContent: "flex-start", alignItems: "center", pointerEvents: "none" }}
    >
      <Audio src={popSound} />
      <div
        style={{
          marginTop: topOffset,
          width: "100%",
          height: titleBarHeight,
          background: theme.titleBackground,
          padding: `${VERTICAL_PADDING}px ${TITLE_H_PADDING}px`,
          textAlign: "center",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          boxSizing: "border-box",
        }}
      >
        <div
          style={{
            fontFamily: fontPreset.family,
            fontSize: titleFontSize,
            fontWeight: fontPreset.weight,
            color: theme.titleTextColor,
            WebkitTextStroke: `5px ${theme.titleAccentColor}`,
            textShadow: `8px 8px 0px ${theme.titleAccentColor}`,
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
  faceCamZoom,
  faceCamY,
  mainZoom,
  mainCropX,
  mainCropY,
  splitTopRatio,
  title,
  captions,
  cutIntervals,
  srcAspect,
  captionFontSize,
  captionFont,
  theme: themeKey,
  themeColors,
}) => {
  useFontsReady();

  const SRC_ASPECT = srcAspect ?? DEFAULT_SRC_ASPECT;
  const theme =
    themeColors ?? THEMES[themeKey ?? DEFAULT_THEME_KEY] ?? THEMES[DEFAULT_THEME_KEY];

  const { fps, width, height } = useVideoConfig();
  const frame = useCurrentFrame();
  const isVertical = height > width;

  const intervals = useMemo(() => {
    if (!cutIntervals?.length) return [{ startSec, endSec }];
    const sorted = [...cutIntervals]
      .filter((iv) => Number.isFinite(iv.startSec) && Number.isFinite(iv.endSec) && iv.endSec > iv.startSec)
      .sort((a, b) => a.startSec - b.startSec);
    if (!sorted.length) return [{ startSec, endSec }];
    const keeps: { startSec: number; endSec: number }[] = [];
    let cursor = startSec;
    for (const cut of sorted) {
      // Clamp to clip range so Studio's default {0,0} or out-of-range values can't move cursor backward
      const cutStart = Math.max(cursor, Math.min(endSec, cut.startSec));
      const cutEnd = Math.max(cutStart, Math.min(endSec, cut.endSec));
      if (cutStart > cursor + 0.01) keeps.push({ startSec: cursor, endSec: cutStart });
      cursor = cutEnd;
    }
    if (cursor < endSec - 0.01) keeps.push({ startSec: cursor, endSec });
    return keeps.length > 0 ? keeps : [{ startSec, endSec }];
  }, [cutIntervals, startSec, endSec]);

  const trimBefore = useMemo(
    () => computeTrimBefore(frame, intervals, fps),
    [frame, intervals, fps],
  );

  const rawCaptions = useMemo((): ClipProps["captions"] => {
    if (captions.length > 0) return captions;
    const segs = (studioData as { segments: StudioSegment[] }).segments;
    return makeCaptions(segs, startSec, endSec) as ClipProps["captions"];
  }, [captions, startSec, endSec]);

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

  // キャプションのエフェクトフィールドからvignette用レンジを導出
  const effectiveEffectRanges = useMemo<EffectRange[]>(
    () =>
      effectiveCaptions
        .filter((c) => !!c.effect)
        .map((c) => ({ startMs: c.startMs, endMs: c.endMs, type: c.effect! })),
    [effectiveCaptions],
  );

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

  const displayTitle = useMemo(
    () => (title ? autoSplitTitle(title, width - TITLE_H_PADDING * 2, 140) : title),
    [title, width],
  );
  const { titleBarHeight, titleFontSize } = calcTitleBar(
    displayTitle,
    width,
    theme?.titleBarMinHeight ?? MIN_TITLE_BAR_H,
  );

  // ショート動画のセーフエリア（サムネイルクロップ対策、既定 5%）。テーマで調整可能。
  const shortsSafeTop = isVertical
    ? Math.round(height * ((theme?.titleTopMargin ?? 5) / 100))
    : 0;

  // ── Panic vignette (text effects are handled in CaptionPage) ─────────────
  const currentMs = (frame / fps) * 1000;
  const panicIntensity = getPanicIntensity(currentMs, effectiveEffectRanges);

  const captionOptions = {
    captionFontSize,
    captionFont: themeColors?.captionFont ?? theme?.captionFont ?? captionFont ?? "mochiy",
    theme,
  };

  // ── Build layout content ──────────────────────────────────────────────────
  const hasVideo = Boolean(videoSrc);
  let content: React.ReactNode;

  if (!isVertical) {
    // 横動画
    content = (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        {hasVideo && <Video
          src={staticFile(videoSrc)}
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
          trimBefore={trimBefore}
        />}
        <AbsoluteFill>{renderCaptionPages(pages, fps, effectiveCaptions, captionOptions)}</AbsoluteFill>
      </AbsoluteFill>
    );
  } else if (verticalMode === "crop") {
    // 縦動画: クロップモード
    const cropZoom = faceCamZoom ?? 1.0;
    const scaledH = Math.round(height * cropZoom);
    const scaledW = Math.round(scaledH * SRC_ASPECT);
    const topOffset = -Math.round((scaledH - height) * ((faceCamY ?? 50) / 100));
    const leftOffset = -Math.round((scaledW - width) * (cropX / 100));

    content = (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        <div style={{ width: "100%", height: "100%", overflow: "hidden", position: "relative" }}>
          {hasVideo && <Video
            src={staticFile(videoSrc)}
            style={{
              position: "absolute",
              width: scaledW,
              height: scaledH,
              top: topOffset,
              left: leftOffset,
            }}
            trimBefore={trimBefore}
          />}
        </div>
        {displayTitle && <TitleBar title={displayTitle} titleFontSize={titleFontSize} titleBarHeight={titleBarHeight} theme={theme} topOffset={shortsSafeTop} />}
        <AbsoluteFill>
          {renderCaptionPages(pages, fps, effectiveCaptions, { ...captionOptions, paddingBottomOverride: CROP_CAPTION_PADDING_BOTTOM })}
        </AbsoluteFill>
      </AbsoluteFill>
    );
  } else {
    // 縦動画: 二段構成モード
    const mainVideoTop = shortsSafeTop + titleBarHeight;
    const topRatio = splitTopRatio ?? 4.5;
    const mainVideoH = Math.round((height - mainVideoTop) * topRatio / 10);
    const bottomTop = mainVideoTop + mainVideoH;
    const bottomH = height - bottomTop;

    // 上段: ズーム＆位置制御
    const mainInnerH = Math.round(mainVideoH * (mainZoom ?? 1.0));
    const mainInnerW = Math.round(mainInnerH * SRC_ASPECT);
    const mainVideoVideoTop = -Math.round((mainInnerH - mainVideoH) * ((mainCropY ?? 50) / 100));
    const rawMainLeft = Math.round(width / 2 - mainInnerW * ((mainCropX ?? 50) / 100));
    const mainVideoVideoLeft = Math.max(-(mainInnerW - width), Math.min(0, rawMainLeft));

    // 下部は全幅で埋めて黒枠をなくす
    const faceCamInnerH = Math.round(bottomH * (faceCamZoom ?? 1.5));
    const faceCamInnerW = Math.round(faceCamInnerH * SRC_ASPECT);
    const faceCamVideoTop = -Math.round((faceCamInnerH - bottomH) * ((faceCamY ?? 50) / 100));
    const rawFaceLeft = Math.round(width / 2 - faceCamInnerW * (cropX / 100));
    const faceCamVideoLeft = Math.max(-(faceCamInnerW - width), Math.min(0, rawFaceLeft));

    const captionTopOffset = bottomTop + 24;

    content = (
      <AbsoluteFill style={{ backgroundColor: "#111" }}>
        <div
          style={{
            position: "absolute",
            top: mainVideoTop,
            left: 0,
            width,
            height: mainVideoH,
            overflow: "hidden",
          }}
        >
          {hasVideo && <Video
            src={staticFile(videoSrc)}
            style={{
              position: "absolute",
              width: mainInnerW,
              height: mainInnerH,
              top: mainVideoVideoTop,
              left: mainVideoVideoLeft
            }}
            trimBefore={trimBefore} />}
        </div>
        {displayTitle && <TitleBar title={displayTitle} titleFontSize={titleFontSize} titleBarHeight={titleBarHeight} theme={theme} topOffset={shortsSafeTop} />}
        <div
          style={{
            position: "absolute",
            top: bottomTop,
            left: 0,
            width,
            height: bottomH,
            overflow: "hidden",
          }}
        >
          <div style={{ position: "relative", width, height: bottomH }}>
            {hasVideo && <Video
              src={staticFile(videoSrc)}
              style={{
                position: "absolute",
                width: faceCamInnerW,
                height: faceCamInnerH,
                top: faceCamVideoTop,
                left: faceCamVideoLeft
              }}
              trimBefore={trimBefore}
              muted />}
          </div>
        </div>
        <AbsoluteFill>
          {renderCaptionPages(pages, fps, effectiveCaptions, { ...captionOptions, topOffset: captionTopOffset })}
        </AbsoluteFill>
      </AbsoluteFill>
    );
  }

  const absoluteSec = ((frame / fps) + startSec).toFixed(1);
  const relativeMs = Math.round((frame / fps) * 1000);
  const isStudio = getRemotionEnvironment().isStudio;

  // ── Wrap with vignette (panic only, subtle) ──────────────────────────────
  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {content}
      {isStudio && (
        <div style={{
          position: "absolute",
          top: 18,
          right: 22,
          background: "rgba(0,0,0,0.55)",
          color: "#fff",
          fontFamily: "monospace",
          fontSize: isVertical ? 28 : 20,
          padding: "4px 12px",
          borderRadius: 6,
          pointerEvents: "none",
          letterSpacing: "0.03em",
          lineHeight: 1.4,
        }}>
          <div style={{ fontSize: "0.9em", opacity: 0.75 }}>startSec / endSec</div>
          <div>{absoluteSec}</div>
          <div style={{ fontSize: "0.9em", opacity: 0.75, marginTop: 4 }}>captions startMs</div>
          <div>{relativeMs}</div>
        </div>
      )}
      {panicIntensity > 0 && (
        <AbsoluteFill
          style={{
            background: `radial-gradient(ellipse at center, transparent 40%, rgba(180,0,0,${(0.35 * panicIntensity).toFixed(3)}) 100%)`,
            pointerEvents: "none",
          }}
        />
      )}
    </AbsoluteFill>
  );
};
