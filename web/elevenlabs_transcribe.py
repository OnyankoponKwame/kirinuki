"""ElevenLabs Speech-to-Text transcription — no file size/duration limit (up to 5GB),
so unlike Groq Whisper this sends the whole file in a single request, no chunking.

Talks to the REST endpoint directly with `requests` rather than the official `elevenlabs`
SDK: that package bundles every ElevenLabs product (dubbing, voices, studio, workspace,
conversational AI, ...) as deeply nested subpackages that `client.py` imports eagerly,
and some of those paths exceed Windows' MAX_PATH once staged into the packaged installer
— Inno Setup's compiler fails with "The system cannot find the path specified." We only
ever call the one speech-to-text endpoint, so a plain HTTP POST avoids all of it.
"""
import os
import sys
from pathlib import Path

import requests

_AUDIO_DIR = Path(__file__).parent.parent / "audio-chunking"
if str(_AUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(_AUDIO_DIR))

from audio_chunking_code import preprocess_audio  # noqa: E402

_API_URL = "https://api.elevenlabs.io/v1/speech-to-text"

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


def _words_to_segments(words: list[dict]) -> list[dict]:
    segments: list[dict] = []
    cur: list[dict] = []
    cur_start: float | None = None
    prev_end: float | None = None

    def flush() -> None:
        nonlocal cur, cur_start
        if cur:
            text = "".join(w.get("text", "") for w in cur).strip()
            if text:
                segments.append({
                    "start": cur_start if cur_start is not None else 0.0,
                    "end": cur[-1].get("end") if cur[-1].get("end") is not None else cur_start,
                    "text": text,
                })
        cur = []
        cur_start = None

    for w in words:
        start = w.get("start")
        end = w.get("end")

        if w.get("type") == "spacing" and not cur:
            continue  # skip leading whitespace at the start of a segment

        if cur and start is not None and prev_end is not None:
            gap = start - prev_end
            text_len = sum(len(x.get("text", "")) for x in cur)
            duration = prev_end - cur_start if cur_start is not None else 0.0
            last_text = cur[-1].get("text", "").strip()
            last_char = last_text[-1:] if last_text else ""
            if (
                gap >= _SILENCE_GAP_SEC
                or text_len >= _MAX_SEGMENT_CHARS
                or duration >= _MAX_SEGMENT_DURATION_SEC
                or last_char in _SENTENCE_END_CHARS
            ):
                flush()

        if cur_start is None and start is not None:
            cur_start = start
        cur.append(w)
        if end is not None:
            prev_end = end

    flush()
    return segments


def transcribe_with_elevenlabs(
    video_path: Path,
    language: str = "ja",
    initial_prompt: str | None = None,
    audio_mode: str = "mp3",
    keyterms: list[str] | None = None,
) -> dict:
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise ValueError("ELEVENLABS_API_KEY environment variable not set")

    audio_path: Path | None = None
    is_temp = False

    try:
        # 動画自体もアップロード可能な API だが、アップロード時間短縮のため音声のみ抽出して送る
        audio_path, _ = preprocess_audio(video_path, audio_mode=audio_mode)
        is_temp = audio_path != video_path
        mime_type = _MIME_BY_SUFFIX.get(audio_path.suffix.lower(), "audio/mpeg")

        size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"▶ ElevenLabs: ファイルをアップロード中 ({size_mb:.1f} MB)...")

        data = {
            "model_id": "scribe_v2",
            "language_code": language,
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
            "diarize": "false",
        }
        if keyterms:
            # requests encodes a list value as repeated multipart fields with the same name
            data["keyterms"] = keyterms

        with open(audio_path, "rb") as f:
            resp = requests.post(
                _API_URL,
                headers={"xi-api-key": api_key},
                data=data,
                files={"file": (audio_path.name, f, mime_type)},
                timeout=1800,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"ElevenLabs API error {resp.status_code}: {resp.text[:500]}")

        print("✓ ElevenLabs: 文字起こし完了")
        payload = resp.json()
        segments = _words_to_segments(payload.get("words") or [])
        return {"text": payload.get("text") or "", "segments": segments}
    finally:
        if is_temp and audio_path:
            audio_path.unlink(missing_ok=True)
