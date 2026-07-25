"""Gemini transcription — extracts audio via preprocess_audio, uploads whole file, no chunking."""
import json
import os
import re
import sys
from pathlib import Path

_AUDIO_DIR = Path(__file__).parent.parent / "audio-chunking"
if str(_AUDIO_DIR) not in sys.path:
    sys.path.insert(0, str(_AUDIO_DIR))

from audio_chunking_code import preprocess_audio  # noqa: E402

DEFAULT_MODEL_ID = "gemini-3.5-flash-lite"

_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

# Matches a single complete segment object, including escaped characters in text.
_SEG_RE = re.compile(
    r'\{\s*"start"\s*:\s*(\d+(?:\.\d+)?)\s*,\s*"end"\s*:\s*(\d+(?:\.\d+)?)\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
    re.DOTALL,
)


def _parse_response(raw: str) -> list[dict]:
    """Parse JSON from Gemini response; fall back to regex extraction if truncated."""
    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.splitlines()
        inner_end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        raw = "\n".join(lines[1:inner_end])

    try:
        data = json.loads(raw)
        return data.get("segments", [])
    except json.JSONDecodeError:
        # Response was cut off — recover whatever complete segments exist
        matches = _SEG_RE.findall(raw)
        if not matches:
            raise
        segments = [
            {"start": float(s), "end": float(e), "text": json.loads(f'"{t}"')}
            for s, e, t in matches
        ]
        print(f"⚠ Gemini: 出力が途中で切れました。{len(segments)} セグメントを回収しました")
        return segments


def transcribe_with_gemini(
    video_path: Path,
    language: str = "ja",
    initial_prompt: str | None = None,
    audio_mode: str = "mp3",
) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise RuntimeError("google-genai パッケージが必要です: pip install google-genai")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable not set")

    client = genai.Client(api_key=api_key)
    audio_path: Path | None = None
    is_temp = False
    uploaded_file = None

    try:
        # Extract audio from video (reuse existing logic — no chunking after this)
        audio_path, _ = preprocess_audio(video_path, audio_mode=audio_mode)
        is_temp = audio_path != video_path
        mime_type = _MIME_BY_SUFFIX.get(audio_path.suffix.lower(), "audio/mpeg")

        # Upload full audio to Gemini Files API
        size_mb = audio_path.stat().st_size / (1024 * 1024)
        print(f"▶ Gemini: ファイルをアップロード中 ({size_mb:.1f} MB)...")
        with open(audio_path, "rb") as f:
            uploaded_file = client.files.upload(
                file=f,
                config=types.UploadFileConfig(
                    mime_type=mime_type,
                    display_name=video_path.stem,
                ),
            )
        print("✓ Gemini: アップロード完了")

        # Build prompt — encourage longer segments to keep output tokens down
        lang_label = {"ja": "日本語", "en": "English", "zh": "中文", "ko": "한국어"}.get(language, language)
        prompt = (
            f"以下の音声を {lang_label} で文字起こしし、"
            "次のJSON形式のみで返してください（マークダウン記法は不要）:\n\n"
            '{"segments": [{"start": 0.0, "end": 15.0, "text": "テキスト"}, ...]}\n\n'
            "- start/end は秒単位の浮動小数点数\n"
            "- 1セグメントは文単位（15〜30秒程度）にまとめて、セグメント数を最小限に抑える\n"
            "- テキストの先頭・末尾に空白は入れない"
        )
        if initial_prompt:
            prompt += f"\n\n追加コンテキスト: {initial_prompt}"

        print(f"▶ Gemini ({DEFAULT_MODEL_ID}): 文字起こし中...")
        response = client.models.generate_content(
            model=DEFAULT_MODEL_ID,
            contents=[prompt, uploaded_file],
            config=types.GenerateContentConfig(
                max_output_tokens=65536,
            ),
        )

        raw = response.text.strip()

        # Warn if output was cut off by token limit
        try:
            finish_reason = response.candidates[0].finish_reason
            if "MAX_TOKENS" in str(finish_reason):
                print("⚠ Gemini: 出力トークン上限 (65536) に達しました。一部のセグメントが欠落する可能性があります")
        except Exception:
            pass

        segments = _parse_response(raw)
        full_text = " ".join(s.get("text", "").strip() for s in segments)
        print(f"✓ Gemini: 文字起こし完了 ({len(segments)} セグメント)")

        return {"text": full_text, "segments": segments}

    finally:
        if is_temp and audio_path:
            audio_path.unlink(missing_ok=True)
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass
