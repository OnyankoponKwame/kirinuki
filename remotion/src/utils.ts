export type ClipCaption = {
  text: string;
  startMs: number;
  endMs: number;
};

type Segment = { start?: number; end?: number; text?: string };

export function makeCaptions(
  segments: Segment[],
  startSec: number,
  endSec: number,
): ClipCaption[] {
  const captions: ClipCaption[] = [];
  for (const seg of segments) {
    const s = seg.start ?? 0;
    const e = seg.end ?? 0;
    if (e <= startSec || s >= endSec) continue;
    const startMs = Math.max(0, (s - startSec) * 1000);
    const endMs = (Math.min(e, endSec) - startSec) * 1000;
    let text = (seg.text ?? "").trim();
    if (!text) continue;

    // 長すぎる文章（18文字以上）は前半と後半に2分割する
    if (text.length >= 18) {
      // 読点「、」があればそこで分割、なければ真ん中で分割
      let splitIdx = text.indexOf("、");
      if (splitIdx === -1 || splitIdx < 5 || splitIdx > text.length - 5) {
        splitIdx = Math.floor(text.length / 2);
      } else {
        splitIdx += 1; // 「、」を含めて分割
      }

      const halfTime = startMs + (endMs - startMs) * (splitIdx / text.length);

      captions.push({
        text: " " + text.slice(0, splitIdx).trim(),
        startMs,
        endMs: halfTime,
      });
      captions.push({
        text: " " + text.slice(splitIdx).trim(),
        startMs: halfTime,
        endMs,
      });
    } else {
      captions.push({
        text: " " + text,
        startMs,
        endMs,
      });
    }
  }
  return captions;
}
