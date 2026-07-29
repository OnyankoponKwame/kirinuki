# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

必ず日本語で回答すること。コード中のコメントも日本語で書くこと。

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

### Editing-material export (`web/premiere_export.py`)
A second, independent output path alongside the Remotion renderer, for editors who want to do
the captioning themselves in Premiere Pro (or any other NLE).
`POST /api/jobs/{jid}/export-premiere` writes a package (a plain folder under `exports/`, opened
in the OS file manager via `POST /api/exports/{name}/open` — not zipped, since the app runs
locally and the folder is already right there on disk) containing, per selected clip:
- an **mp4 with the vertical framing and `cutIntervals` baked in but no captions, title bar,
  or effects** — encoded by ffmpeg (H.264 CRF 18), never by Remotion
- an `.srt` whose timings line up with that mp4 exactly

plus a Japanese `README.txt`. Deliberately no project/interchange file: an earlier version
emitted an FCP7 XML, but once framing and cuts were baked into the mp4 that file did nothing
but place one whole clip on one sequence — which dragging the mp4 into Premiere already does —
so it was dropped rather than maintained. Baking the framing in also means the package works
the same in Resolve or Final Cut, and nothing depends on how an importer reads an interchange
format. The trade-off is that crop position and cuts cannot be adjusted downstream; re-export
from the app instead.

Unlike `/render`, this endpoint is long-running: it starts a background task, reports progress
over the job's SSE stream, publishes the result in the state message's `export` field, and
honours `/cancel` (status `exporting`).

Not reproducible downstream, and therefore Remotion-only: karaoke active-word highlighting,
`captionEffect` animations, the title bar text, and theme colours.

One thing to know before touching it: `compute_keep_intervals()` / `remap_captions_to_cuts()`
in `pipeline.py` are **line-by-line ports of the `intervals` and `effectiveCaptions` useMemos
in `ClipComposition.tsx`**, and `premiere_export.compute_layers()` mirrors that file's layout
math (including the title-bar height calculation). `compute_layers()` is the single source for
both the ffmpeg filtergraph
and the sequence dimensions, so exports stay self-consistent — but changing the TSX without
mirroring it here makes a Remotion render and a Premiere export disagree. The two were verified
to agree to the pixel (SSIM 0.98 against a Remotion still of the same frame; ±1px of crop
offset drops it to 0.97).

`split_geometry()` (the safe-area / title-bar / two-panel split) is factored out of
`compute_layers()` because the position picker needs the panel heights *and* `mainTop` too —
see the next section.

### Position picker (both vertical modes)
Framing values are hard to set blind, so the clip card has a
「🎯 静止画から位置・ズームを指定」 button (any vertical clip). It opens a still from the source
video and the user drags a box on it; the picker solves ClipComposition's equations backwards
for that surface's three values, choosing the tightest zoom that still contains the box and
never one that would leave a black edge.

What can be picked depends on the clip's `verticalMode`. In **split mode** the dialog shows a
tab per panel; in **crop mode** there is a single surface and the tab row is hidden:

| tab | props | surface |
|---|---|---|
| 上段（全体映像） | `mainZoom` / `mainCropX` / `mainCropY` | panel of height `mainH` |
| 下段（顔カメラ） | `faceCamZoom` / `cropX` / `faceCamY` | panel of height `bottomH` |
| 表示範囲 (crop) | `faceCamZoom` / `cropX` / `faceCamY` | the whole 9:16 frame |

The two split panels are the *same* equations differing only in the panel height, so
`fitPanel()` / `panelWindow()` take `panelH` and return generic `{zoom, x, y}`. **Crop mode is
not**: its zoom is relative to the full `seqH`, and its `left` is "fraction of the horizontal
overflow hidden on the left" rather than split mode's "source x that lands at the panel
centre" — hence the separate `fitCrop()` / `cropWindow()`. The mapping from generic
`{zoom, x, y}` back to prop names, slider selectors and labels, plus which fit/window pair to
use, lives in the `FACE_PANELS` table; the picker itself knows nothing else about the three
surfaces. Each tab keeps its own selection, so one still can set both split panels, and
「適用」 writes whichever tabs were drawn on.

Crop mode's extra wrinkle is that the **title bar is drawn over the video**, so a box fitted to
the full frame could land behind it. `fitCrop()` therefore fits the box into the frame *below*
`mainTop` (safe area + title bar), which also gives zoom a lower bound: the top of the box can
only clear the bar at `zoom ≥ mainTop / (rect.y * seqH)`, so boxing something high in the frame
legitimately forces a tighter zoom (at `rect.y → 0` it is unreachable and the zoom pins to 8).
When a constraint stops the box from fitting exactly, the dialog says so under the values, the
band the title bar covers is drawn on both the preview and the still, and the visible window is
aligned to the box's top rather than centred so the subject's head is not the part that gets
cut.

Because `cropX` / `faceCamZoom` / `faceCamY` mean different things in the two modes,
「すべてのクリップに適用」 only writes to cards in the *same* mode as the one the picker was
opened from.

Two endpoints back it:
- `GET /api/frame?video=&t=` — one JPEG via ffmpeg, downscaled to ≤1280px wide.
- `POST /api/split-geometry` — `{title, splitTopRatio}` → panel bounds, from
  `premiere_export.split_geometry()`. The browser cannot derive `mainH` / `bottomH` / `mainTop`
  itself: they depend on the title bar, whose height only that module's port of `calcTitleBar()`
  knows. (Crop mode only needs `mainTop`, which does not depend on `splitTopRatio`.)

`panelWindow()` / `cropWindow()` next to the fit functions are a **fourth copy** of the framing
formulas, deliberately kept so the picker's preview and dashed overlay show exactly what will be
rendered. Both are verified to agree with `compute_layers()` bit-for-bit for all three surfaces,
so they inherit that function's pixel-level agreement with Remotion — but they are one more
place to update when the TSX's 二段構成モード / クロップモード blocks change.

Two things the fits deliberately do *not* do: return a zoom below 1 (that would letterbox the
surface) even though the sliders allow down to 0.5 — a manual-only setting the preview still
displays faithfully — and chase sub-percent accuracy, since `cropX` and the rest are integer
percent sliders (the box can land up to half a step off, ~0.5% of the frame).

### Remotion renderer (`remotion/`)
- `remotion/src/ClipComposition.tsx` — The main Remotion composition. Accepts `ClipProps` (validated via Zod schema). Supports three layouts: horizontal (16:9), vertical crop mode, and vertical split mode (top panel + face-cam circle).
- `remotion/src/CaptionPage.tsx` — Renders one TikTok-style caption page with karaoke-style active-word highlighting (white text, pink active token).
- `remotion/src/Root.tsx` — Registers `ClipComposition` (used for CLI renders) and `StudioCompositions` (a stub in `src/`; see below for where the real per-clip compositions live).
- `remotion/src/studioCompositions.tsx` — a **stub** (`() => null`) that only exists so
  `Root.tsx` compiles for CLI renders. The real per-clip compositions are generated into
  `remotion/studio-src/StudioRoot.tsx` — see below.
- `remotion/src/studioViewReset.ts` — Studio keeps the preview canvas' zoom and pan in
  `localStorage`, so an accidental ctrl+scroll survives a Studio restart with no obvious way
  back. Every "Studio で確認" press stamps a fresh token into the generated file (which is why
  it is rewritten unconditionally), and this module clears that view state when it sees a new
  one. It relies on the user bundle being evaluated *before* `@remotion/studio`'s preview entry
  — see `getStudioEntryPoints()` — so on a normal page load clearing the keys is enough; it
  reloads only when the file arrived via hot reload into an already-open tab.

#### Why Studio runs on a throwaway copy (`remotion/studio-src/`)
Remotion Studio is not read-only: editing a Sequence's **Offset**, an element's
**Scale / Translate / Rotate**, or saving from the Props editor **rewrites the source file that
declares that JSX** — i.e. `ClipComposition.tsx` itself (it also inlines `defaultProps` into
`Root.tsx` on the first props save). A stray drag therefore silently changes what every
subsequent CLI render produces, and the only undo is right-click → Reset per field.

So `/api/studio/open` mirrors `remotion/src` into `remotion/studio-src` (`_sync_studio_src()`)
and starts Studio with `studio-src/studio-entry.ts` as its entry point. Studio's write-backs land
in the copy; every "Studio で確認" press overwrites the copy from `remotion/src` again, which is
what makes the button reset the layout. Files that had to be restored come back in the
response's `reverted` field and are logged in the phase-4 console.

- `remotion/src` stays the single source of truth, and is what `pipeline.render_clip()` bundles.
- `studio-src/` must sit directly under `remotion/` (sibling of `src/`): `ClipComposition.tsx`
  imports `../public/Onoma-Pop04.mp3`, and node module resolution walks up to
  `remotion/node_modules`. It is gitignored and excluded from `tsconfig.json`.
- Tweaking a value *in Studio* is fine for experimenting, but to keep it you must copy it into
  `remotion/src` yourself before the next press.
- `npm start` in `remotion/` still runs Studio on `src/` directly — that instance **can** write
  to the real sources, and shows no clip compositions (the stub).
- The per-clip `<Composition>` elements are generated with their `id` and `defaultProps` inline
  (not through a named component like the old `StudioCompositions`), directly into
  `studio-src/StudioRoot.tsx` — required for Remotion's Props-editor "save default props" to
  work at all, per
  [its troubleshooting doc](https://www.remotion.dev/docs/troubleshooting/cannot-save-default-props):
  that feature parses only whatever single file it resolves as "the root file" and never follows
  imports, so a `<StudioCompositions />` reference wouldn't do — the compositions must be
  literal JSX in that file. Getting Remotion to resolve *that* file (rather than the real
  `remotion/src/Root.tsx`, which is otherwise what its `known root paths` search always finds,
  since `remotionRoot` is fixed to `remotion/` regardless of which entry point Studio was
  started with) is what the `studio-entry.ts` → `StudioRoot.tsx` naming accomplishes: an entry
  point whose name ends in `-entry` makes Remotion look for a matching PascalCase root file
  *next to that entry point* instead (see `_STUDIO_ENTRY_TS` in `web/app.py`). Without this,
  Props-editor saves on a per-clip composition fail with "Could not find defaultProps for
  composition ...".

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
            ↓ (別経路・任意)
      premiere_export → ffmpeg → 字幕なしmp4 + SRT (exports/premiere_*/)
```

## Clip data schema

Clips are JSON objects stored in `transcriptions/clips_*.json`. Key fields:
- `start_sec`, `end_sec` — absolute video timestamps
- `vertical` / `verticalMode` — `"crop"` (full-height video cropped to portrait) or `"split"` (full-width video on top + zoomed face-cam circle below)
- `cropX` — horizontal crop position 0–100%
- `faceCamZoom`, `faceCamY` — for split mode face-cam
- `cutIntervals` — list of `{startSec, endSec}` of segments to remove (silence cuts, jump cuts)
- `captions` — list of `{text, startMs, endMs, effect?, isComment?}` relative to clip start. Not stored
  on the clip itself — `pipeline.make_captions()` rebuilds this fresh from the transcript segments
  (`*_full.json`) on every render. `effect` is auto-detected per segment
  (`detect_effect_for_segment()`); `isComment` comes from that segment's `is_comment: true` flag and
  switches `CaptionPage` to the icon+bubble "コメント" rendering instead of the normal caption style —
  set it by hand-editing the segment in `*_full.json`, there is no UI for it yet.

Old-format split clips using `_concat_group` / `_concat_index` are automatically migrated to `cutIntervals` by `pipeline.merge_split_clips()`.

## Important conventions

- Caption text splitting: segments ≥18 characters are split at `、` (Japanese comma) if present, otherwise at the midpoint. Both halves get proportional timestamps.
- Transcriptions are saved to `transcriptions/` (root), not `web/transcriptions/`.
- The `web/transcriptions/` directory contains legacy files; new files go to project root `transcriptions/`.
- Remotion renders use `--public-dir` pointing to the video's parent directory so `staticFile(videoSrc)` resolves correctly.
- `suggest_clips_from_result()` requires `GEMINI_API_KEY` (via `.env` or the settings screen) regardless of which transcription backend is selected — no `claude` CLI, login, or Anthropic key is needed anywhere in the app.
- Job state is in-memory only; server restart loses all jobs.
- On Windows, subprocess calls (yt-dlp/ffmpeg/ffprobe/npx/npm) pass `creationflags=subprocess.CREATE_NO_WINDOW` (see `config.no_window_kwargs()`) so a packaged install never flashes a console window.
