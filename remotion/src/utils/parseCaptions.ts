import type { Caption } from "@remotion/captions";

type WhisperSegment = {
  start: number;
  end: number;
  text: string;
};

export const parseWhisperToCaptions = (segments: WhisperSegment[]): Caption[] => {
  return segments.map((segment) => ({
    startMs: Math.round(segment.start * 1000),
    endMs: Math.round(segment.end * 1000),
    text: " " + segment.text.trim(),
    timestampMs: null,
    confidence: null,
  }));
};
