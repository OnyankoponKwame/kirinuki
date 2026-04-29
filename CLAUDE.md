# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Kirinuki is a tool that automatically generates clip videos from YouTube live streams. It downloads a video, transcribes audio with Groq Whisper, uses Claude CLI to suggest interesting segments, and renders MP4 clips with subtitles via Remotion (React-based video renderer).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # set GROQ_API_KEY
cd remotion && npm install
```

Required system tools: `yt-dlp`, `ffmpeg`, `ffprobe`, `claude` (CLI), `npx`

## Running the app

```bash
cd web
uvicorn app:app --reload
# Open http://localhost:8000
```

## Architecture

The project has three layers that communicate through the filesystem and subprocesses:

### Python backend (`web/`)
- `web/app.py` — FastAPI server. Manages in-memory jobs (dict keyed by UUID). Each job runs `pipeline.py` in a thread pool executor. The frontend polls `/api/jobs/{jid}/events` via SSE.
- `web/pipeline.py` — All heavy lifting: `download_video` (yt-dlp), `run_transcription` (delegates to `audio_chunking_code.py`), `suggest_clips_from_result` (calls `claude -p ... --output-format text` as subprocess), `render_clip` (calls `npx remotion render`).

### Remotion renderer (`remotion/`)
- `remotion/src/ClipComposition.tsx` — The main Remotion composition. Accepts `ClipProps` (validated via Zod schema). Supports three layouts: horizontal (16:9), vertical crop mode, and vertical split mode (top panel + face-cam circle).
- `remotion/src/CaptionPage.tsx` — Renders one TikTok-style caption page with karaoke-style active-word highlighting (white text, pink active token).
- `remotion/src/Root.tsx` — Registers `ClipComposition` (used for CLI renders) and `StudioCompositions` (auto-generated for Remotion Studio preview).
- `remotion/src/studioCompositions.tsx` — **Auto-generated** by `app.py`'s `_generate_studio_compositions()` when "Studio で確認" is clicked. Do not edit manually.

### Transcription (`audio-chunking/`)
- `audio_chunking_code.py` — Converts video to 16kHz mono FLAC via ffmpeg, then sends to Groq Whisper in chunks (handles rate limits).

## Key data flow

```
URL → yt-dlp → .mp4 (downloads/)
            ↓
      Groq Whisper → *_full.json (transcriptions/)
            ↓
      Claude CLI → clips_*.json (transcriptions/)
            ↓
      npx remotion render → *.mp4 (clips/)
```

## Clip data schema

Clips are JSON objects stored in `transcriptions/clips_*.json`. Key fields:
- `start_sec`, `end_sec` — absolute video timestamps
- `vertical` / `verticalMode` — `"crop"` (full-height video cropped to portrait) or `"split"` (full-width video on top + zoomed face-cam circle below)
- `cropX` — horizontal crop position 0–100%
- `faceCamZoom`, `faceCamY` — for split mode face-cam
- `keepIntervals` — list of `{startSec, endSec}` for silence-cut clips (gaps are jump-cut out)
- `captions` — list of `{text, startMs, endMs}` relative to clip start

Old-format split clips using `_concat_group` / `_concat_index` are automatically migrated to `keepIntervals` by `pipeline.merge_split_clips()`.

## Important conventions

- Caption text splitting: segments ≥18 characters are split at `、` (Japanese comma) if present, otherwise at the midpoint. Both halves get proportional timestamps.
- Transcriptions are saved to `transcriptions/` (root), not `web/transcriptions/`.
- The `web/transcriptions/` directory contains legacy files; new files go to project root `transcriptions/`.
- Remotion renders use `--public-dir` pointing to the video's parent directory so `staticFile(videoSrc)` resolves correctly.
- The `claude` CLI must be authenticated and available in PATH — `suggest_clips_from_result()` calls it directly as a subprocess.
- Job state is in-memory only; server restart loses all jobs.
