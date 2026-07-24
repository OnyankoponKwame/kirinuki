"""ElevenLabs Speech-to-Text transcription — no file size/duration limit (up to 5GB),
so unlike Groq Whisper this sends the whole file in a single request, no chunking."""
import os
import sys
from pathlib import Path

_AUDIO_DIR = Path(__file__).parent.parent / "audio-chunking"
if str(_AUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(_AUDIO_DIR))

from audio_chunking_code import preprocess_audio  # noqa: E402

_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

# ElevenLabs returns word-level timestamps, not segments. These thresholds group
# words back into Whisper/Groq-like sentence segments for the rest of the pipeline
# (caption splitting, clip suggestion prompt, etc.), which all expect {start,end,text}.
_MAX_SEGMENT_CHARS = 80
_MAX_SEGMENT_DURATION_SEC = 15.0
_SILENCE_GAP_SEC = 1.2
_SENTENCE_END_CHARS = "。！？.!?"


def _words_to_segments(words: list) -> list[dict]:
    segments: list[dict] = []
    cur: list = []
    cur_start: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            text = "".join(w.text for w in cur).strip()
            if text:
                segments.append({
                    "start": cur_start if cur_start is not None else 0.0,
                    "end": cur[-1].end if cur[-1].end is not None else cur_start,
                    "text": text,
                })
        cur = []
        cur_start = None

    for w in words:
        if w.type == "spacing" and not cur:
            continue  # skip leading whitespace at the start of a segment

        if cur and w.start is not None and prev_end is not None:
            gap = w.start - prev_end
            text_len = sum(len(x.text) for x in cur)
            duration = prev_end - cur_start if cur_start is not None else 0.0
            last_char = cur[-1].text.strip()[-1:] if cur[-1].text.strip() else ""
            if (
                gap >= _SILENCE_GAP_SEC
                or text_len >= _MAX_SEGMENT_CHARS
                or duration >= _MAX_SEGMENT_DURATION_SEC
                or last_char in _SENTENCE_END_CHARS
            ):
                flush()

        if cur_start is None and w.start is not None:
            cur_start = w.start
        cur.append(w)
        if w.end is not None:
            prev_end = w.end

    flush()
    return segments


def transcribe_with_elevenlabs(
    video_path: Path,
    language: str = "ja",
    initial_prompt: str | None = None,
    audio_mode: str = "mp3",
) -> dict:
    try:
        from elevenlabs import ElevenLabs
    except ImportError:
        raise RuntimeError("elevenlabs パッケージが必要です: pip install elevenlabs")

    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY environment variable not set")

    client = ElevenLabs(api_key=api_key, timeout=1800)
    audio_path: Path | None = None
    is_temp = False

    try:
        # 動画自体もアップロード可能な API だが、アップロード時間短縮のため音声のみ抽出して送る
        audio_path, _ = preprocess_audio(video_path, audio_mode=audio_mode)
        is_temp = audio_path != video_path
        mime_type = _MIME_BY_SUFFIX.get(audio_path.suffix.lower(), "audio/mpeg")

        size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"▶ ElevenLabs: ファイルをアップロード中 ({size_mb:.1f} MB)...")

        with open(audio_path, "rb") as f:
            response = client.speech_to_text.convert(
                model_id="scribe_v2",
                file=(audio_path.name, f, mime_type),
                language_code=language,
                timestamps_granularity="word",
                tag_audio_events=False,
                diarize=False,
            )
        print("✓ ElevenLabs: 文字起こし完了")

        segments = _words_to_segments(response.words or [])
        return {"text": response.text or "", "segments": segments}
    finally:
        if is_temp and audio_path:
            audio_path.unlink(missing_ok=True)
