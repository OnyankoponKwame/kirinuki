# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Kirinuki is a tool that automatically generates clip videos from YouTube live streams. It downloads a video, transcribes audio (ElevenLabs Scribe by default; Groq Whisper is also selectable — Gemini is not used for transcription), uses Gemini to suggest interesting segments, and renders MP4 clips with subtitles via Remotion (React-based video renderer).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # set GEMINI_API_KEY and ELEVENLABS_API_KEY
cd remotion && npm install
```

Required system tools: `yt-dlp`, `ffmpeg`, `ffprobe`, `npx`

API keys (`GEMINI_API_KEY` for clip suggestion, `ELEVENLABS_API_KEY` for the default
transcription backend, optionally `GROQ_API_KEY` for the alternate one) can also be set from
the in-app settings screen (⚙ 設定) instead of `.env` — see `web/config.py`. That's the path
packaged/distributed installs use (see `packaging/windows/`); `.env` remains the dev-machine
convenience.

The Windows installer build can also bake in default `GEMINI_API_KEY`/`ELEVENLABS_API_KEY`
values so end users need zero setup — `.github/workflows/build-windows-installer.yml` passes
GitHub Actions secrets of the same names into `build.ps1`'s step 5, which writes them to
`web/default_config.json` in the staged app (never committed — see `.gitignore`). `config.py`
loads this as the lowest-priority source; the settings screen still overrides it per-install.

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
- `web/pipeline.py` — All heavy lifting: `download_video` (yt-dlp), `run_transcription` (delegates to `elevenlabs_transcribe.py` / `audio_chunking_code.py` depending on `transcription_model` — never Gemini), `suggest_clips_from_result` (calls Gemini via `google-genai`, model `pipeline.GEMINI_MODEL_ID`, regardless of which transcription backend was used), `render_clip` (calls `npx remotion render`).
- `web/config.py` — Persists API keys entered via the settings screen to `config.json` under `get_data_dir()` (repo root in dev, `%LOCALAPPDATA%\Kirinuki` on a packaged Windows install), and applies them to `os.environ` on top of `.env`.

### Remotion renderer (`remotion/`)
- `remotion/src/ClipComposition.tsx` — The main Remotion composition. Accepts `ClipProps` (validated via Zod schema). Supports three layouts: horizontal (16:9), vertical crop mode, and vertical split mode (top panel + face-cam circle).
- `remotion/src/CaptionPage.tsx` — Renders one TikTok-style caption page with karaoke-style active-word highlighting (white text, pink active token).
- `remotion/src/Root.tsx` — Registers `ClipComposition` (used for CLI renders) and `StudioCompositions` (auto-generated for Remotion Studio preview).
- `remotion/src/studioCompositions.tsx` — **Auto-generated** by `app.py`'s `_generate_studio_compositions()` when "Studio で確認" is clicked. Do not edit manually.

### Transcription (`audio-chunking/`, `web/`) — Gemini is not used here, only for clip suggestion
- `web/elevenlabs_transcribe.py` — **Default** backend. ElevenLabs Speech-to-Text has no file size/duration limit, so the whole preprocessed audio file is sent in a single request (no chunking). Talks to the REST endpoint directly with `requests` rather than the official `elevenlabs` SDK — that package's `client.py` eagerly imports every product line (dubbing, voices, studio, conversational AI, ...), and some of those paths are deep enough to exceed Windows' MAX_PATH once staged into the installer. Its response only has word-level timestamps, not segments, so `_words_to_segments()` regroups words into Whisper/Groq-like sentence segments (splits on silence gaps, sentence-ending punctuation, or length) for the rest of the pipeline.
- `audio_chunking_code.py` — Groq Whisper backend. Converts video to 16kHz mono FLAC via ffmpeg, then sends to Groq Whisper in chunks (handles rate limits, needed since Groq has tighter per-request size limits).

## Key data flow

```
URL → yt-dlp → .mp4 (downloads/)
            ↓
      ElevenLabs Scribe (既定) / Groq Whisper → *_full.json (transcriptions/)
            ↓
      Gemini → clips_*.json (transcriptions/)
            ↓
      npx remotion render → *.mp4 (clips/)
```

## Clip data schema

Clips are JSON objects stored in `transcriptions/clips_*.json`. Key fields:
- `start_sec`, `end_sec` — absolute video timestamps
- `vertical` / `verticalMode` — `"crop"` (full-height video cropped to portrait) or `"split"` (full-width video on top + zoomed face-cam circle below)
- `cropX` — horizontal crop position 0–100%
- `faceCamZoom`, `faceCamY` — for split mode face-cam
- `cutIntervals` — list of `{startSec, endSec}` of segments to remove (silence cuts, jump cuts)
- `captions` — list of `{text, startMs, endMs}` relative to clip start

Old-format split clips using `_concat_group` / `_concat_index` are automatically migrated to `cutIntervals` by `pipeline.merge_split_clips()`.

## Important conventions

- Caption text splitting: segments ≥18 characters are split at `、` (Japanese comma) if present, otherwise at the midpoint. Both halves get proportional timestamps.
- Transcriptions are saved to `transcriptions/` (root), not `web/transcriptions/`.
- The `web/transcriptions/` directory contains legacy files; new files go to project root `transcriptions/`.
- Remotion renders use `--public-dir` pointing to the video's parent directory so `staticFile(videoSrc)` resolves correctly.
- `suggest_clips_from_result()` requires `GEMINI_API_KEY` (via `.env` or the settings screen) regardless of which transcription backend is selected — no `claude` CLI, login, or Anthropic key is needed anywhere in the app.
- Job state is in-memory only; server restart loses all jobs.
- On Windows, subprocess calls (yt-dlp/ffmpeg/ffprobe/npx/npm) pass `creationflags=subprocess.CREATE_NO_WINDOW` (see `config.no_window_kwargs()`) so a packaged install never flashes a console window.
