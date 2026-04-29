#!/usr/bin/env python3
"""Kirinuki Web — FastAPI backend."""

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

PROJECT_DIR = Path(__file__).parent.parent
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
TRANSCRIPTIONS_DIR = PROJECT_DIR / "transcriptions"
REMOTION_DIR = PROJECT_DIR / "remotion"

app = FastAPI(title="Kirinuki Web")

# ── In-memory job store ───────────────────────────────────────────────────────

jobs: dict[str, dict[str, Any]] = {}
_active_job: str | None = None
_studio_proc: subprocess.Popen | None = None


def _new_job(req: "StartReq") -> str:
    jid = uuid.uuid4().hex[:8]
    jobs[jid] = {
        "id": jid,
        "status": "running",
        "stage": "pending",
        "logs": [],
        "clips": req.clips,
        "video_path": None,
        "transcription_path": None,
        "chat_path": None,
        "clips_path": None,
        "rendered": [],
        "error": None,
        # inputs (for pipeline logic)
        "_url": req.url,
        "_language": req.language,
        "_stop_after": req.stop_after,
        "_chat_only": req.chat_only,
        "_in_video": req.video_path,
        "_in_transcription": req.transcription_path,
        "_transcription_prompt": req.transcription_prompt,
        "_in_clips": req.clips_path,
        "_audio_mode": req.audio_mode,
        "_transcription_model": req.transcription_model,
        "_trim_start_sec": req.trim_start_min * 60 if req.trim_start_min is not None else None,
        "_trim_end_sec": req.trim_end_min * 60 if req.trim_end_min is not None else None,
        "_extra_prompt": req.extra_prompt,
        "_silence_cut": req.silence_cut,
        "_silence_threshold": req.silence_threshold,
        "_src_aspect_override": req.src_aspect,
        "src_aspect": req.src_aspect,  # None until detected; avoids premature dropdown pre-selection
    }
    return jid


# ── Path resolution ───────────────────────────────────────────────────────────

def _resolve(name: str, base: Path) -> Path:
    p = Path(name)
    return p if p.is_absolute() else base / name


# ── Background pipeline ───────────────────────────────────────────────────────

def _run_pipeline(job_id: str) -> None:
    import pipeline as pl

    job = jobs[job_id]

    def log(text: str) -> None:
        job["logs"].append(text)

    try:
        video_path: Path
        chat_path: Path | None = None
        result: dict | None = None

        # ── Phase 1: Download ──────────────────────────────────────────────────
        if job["_chat_only"] and job["_url"]:
            job["stage"] = "downloading"
            log(f"▶ チャットのみダウンロード: {job['_url']}")
            chat_path = pl.download_chat_only(job["_url"], DOWNLOADS_DIR, log)
            if chat_path:
                job["chat_path"] = str(chat_path)
                log(f"✓ Chat: {chat_path.name}")
            else:
                log("⚠ チャットファイルが見つかりませんでした（ライブ配信のアーカイブのみ対応）")
            job["stage"] = "ready"
            job["status"] = "ready"
            return

        if job["_in_video"]:
            video_path = _resolve(job["_in_video"], DOWNLOADS_DIR)
            job["video_path"] = str(video_path)
            chat_candidate = video_path.with_suffix("").with_suffix(".live_chat.json")
            chat_path = chat_candidate if chat_candidate.exists() else None
            log(f"▶ 動画ファイル使用: {video_path.name}")
        elif job["_url"]:
            job["stage"] = "downloading"
            log(f"▶ Downloading: {job['_url']}")
            video_path, chat_path = pl.download_video(
                job["_url"], DOWNLOADS_DIR, log,
            )
            job["video_path"] = str(video_path)
            log(f"✓ Video: {video_path.name}")
            if chat_path:
                log(f"✓ Chat: {chat_path.name}")
        else:
            # transcription_path provided — no video needed for suggest-only flow
            video_path = Path("__no_video__")
            chat_path = None
            log("▶ 動画スキップ（文字起こしから開始）")

        # Auto-detect source dimensions unless caller overrode
        if not job.get("_src_aspect_override") and video_path.exists():
            try:
                vw, vh = pl.get_video_dimensions(video_path)
                job["src_aspect"] = vw / vh if vh else 16 / 9
                log(f"✓ ソース動画: {vw}×{vh} (aspect {job['src_aspect']:.4f})")
            except Exception:
                pass

        if job["_stop_after"] == "download":
            job["stage"] = "ready"
            job["status"] = "ready"
            return

        # ── Phase 2: Transcribe ────────────────────────────────────────────────
        if job["_in_transcription"]:
            t_path = _resolve(job["_in_transcription"], TRANSCRIPTIONS_DIR)
            job["transcription_path"] = str(t_path)
            with open(t_path, encoding="utf-8") as f:
                result = json.load(f)
            log(f"▶ 文字起こし使用: {t_path.name}")
        else:
            job["stage"] = "transcribing"
            trim_start = job.get("_trim_start_sec")
            trim_end = job.get("_trim_end_sec")
            trimmed_path = None
            try:
                if trim_start is not None or trim_end is not None:
                    start_s = trim_start or 0.0
                    start_label = f"{start_s / 60:.1f}分"
                    end_label = f"{trim_end / 60:.1f}分" if trim_end is not None else "末尾"
                    log(f"▶ 指定範囲をクリップ中: {start_label} 〜 {end_label}")
                    with pl.with_logging(log):
                        trimmed_path = pl.trim_video(video_path, start_s, trim_end)
                    log(f"✓ クリップ完了: {trimmed_path.name}")
                    transcribe_src = trimmed_path
                else:
                    transcribe_src = video_path

                log("▶ Transcribing audio...")
                with pl.with_logging(log):
                    result = pl.run_transcription(
                        transcribe_src,
                        job["_language"],
                        job.get("_transcription_prompt"),
                        job.get("_audio_mode", "mp3"),
                        job.get("_transcription_model", "groq"),
                    )
            finally:
                if trimmed_path:
                    trimmed_path.unlink(missing_ok=True)

            if trim_start:
                pl.offset_timestamps(result, trim_start)
                log(f"✓ タイムスタンプを {trim_start:.1f}秒 オフセット済み")

            t_path = pl.save_transcription(result, video_path)
            job["transcription_path"] = str(t_path)
            log(f"✓ Transcription: {t_path.name}")

        # ── Phase 3: Suggest clips ─────────────────────────────────────────────
        if job["clips"] is not None:
            log(f"▶ 既存クリップ使用: {len(job['clips'])} 件")
        elif job["_in_clips"]:
            clips_file = _resolve(job["_in_clips"], TRANSCRIPTIONS_DIR)
            with open(clips_file, encoding="utf-8") as f:
                job["clips"] = json.load(f)
            log(f"▶ クリップファイル使用: {clips_file.name}")
        else:
            job["stage"] = "suggesting"
            log("▶ Asking Claude for clip suggestions...")
            assert result is not None
            job["clips"] = pl.suggest_clips_from_result(result, chat_path, job.get("_extra_prompt"))
            clips_file = pl.save_clips(job["clips"], video_path)
            job["clips_path"] = str(clips_file)
            log(f"✓ {len(job['clips'])} 件のクリップを提案 → {clips_file.name}")

        if job.get("_silence_cut") and result is not None:
            segs = result.get("segments", [])
            before = len(job["clips"])
            job["clips"] = pl.cut_silence_from_clips(
                job["clips"], segs, job.get("_silence_threshold", 2.0)
            )
            log(f"✓ 無音カット: {before} 件 → {len(job['clips'])} 件")
            if job.get("clips_path"):
                clips_file = pl.save_clips(job["clips"], video_path)
                job["clips_path"] = str(clips_file)

        # Migrate any old-style split clips (_concat_group) to keepIntervals format
        job["clips"] = pl.merge_split_clips(job["clips"])

        job["stage"] = "ready"
        job["status"] = "ready"

    except Exception as exc:
        job["status"] = "error"
        job["stage"] = "error"
        job["error"] = str(exc)
        log(f"✗ Error: {exc}")


async def _pipeline_task(job_id: str) -> None:
    global _active_job
    _active_job = job_id
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _run_pipeline, job_id)
    finally:
        _active_job = None


# ── REST API ──────────────────────────────────────────────────────────────────

class StartReq(BaseModel):
    # Phase 1
    url: str | None = None
    language: str = "ja"
    stop_after: str | None = None          # "download" → stop after phase 1
    chat_only: bool = False                # チャットのみダウンロード（動画スキップ）
    # Skip phases by providing existing files (filenames relative to their dirs)
    video_path: str | None = None          # skip download
    transcription_path: str | None = None  # skip transcription
    transcription_prompt: str | None = None  # initial prompt for Whisper transcription
    audio_mode: str = "mp3"               # audio conversion: "mp3" | "flac_fast" | "stream_copy"
    transcription_model: str = "groq"    # transcription backend: "groq" | "gemini"
    trim_start_min: float | None = None  # clip video before transcription (minutes)
    trim_end_min: float | None = None    # clip video before transcription (minutes)
    clips_path: str | None = None          # skip suggestion (load from file)
    clips: list[dict] | None = None        # skip suggestion (inline)
    extra_prompt: str | None = None        # additional instruction for clip suggestion
    silence_cut: bool = False              # trim/split clips at silent sections
    silence_threshold: float = 2.0        # minimum silence length in seconds to cut
    src_aspect: float | None = None       # source video aspect ratio override (e.g. 9/16 for vertical)

    @model_validator(mode="after")
    def check_source(self) -> "StartReq":
        if not self.url and not self.video_path and not self.transcription_path:
            raise ValueError("url / video_path / transcription_path のいずれかが必要です")
        return self


class UpdateClipsReq(BaseModel):
    clips: list[dict]


class RenderReq(BaseModel):
    indices: list[int] | None = None  # None = all clips


@app.post("/api/jobs")
async def create_job(req: StartReq):
    if _active_job:
        raise HTTPException(409, "別のジョブが実行中です")
    jid = _new_job(req)
    asyncio.create_task(_pipeline_task(jid))
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{jid}/events")
async def job_events(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "Job not found")

    async def gen():
        cursor = 0
        terminal = {"complete", "error", "ready"}
        while True:
            job = jobs[jid]
            for text in job["logs"][cursor:]:
                yield f"data: {json.dumps({'type': 'log', 'text': text})}\n\n"
                cursor += 1
            yield f"data: {json.dumps({'type': 'state', 'status': job['status'], 'stage': job['stage'], 'clips': job['clips'], 'rendered': job['rendered'], 'error': job['error'], 'video_path': job['video_path'], 'transcription_path': job['transcription_path'], 'chat_path': job.get('chat_path'), 'clips_path': job.get('clips_path'), 'src_aspect': job.get('src_aspect')})}\n\n"
            if job["status"] in terminal:
                break
            await asyncio.sleep(0.5 if job["status"] != "ready" else 1.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.put("/api/jobs/{jid}/clips")
def update_clips(jid: str, req: UpdateClipsReq):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    job["clips"] = req.clips
    return {"ok": True}


@app.post("/api/jobs/{jid}/render")
async def start_render(jid: str, req: RenderReq):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] not in ("ready", "complete"):
        raise HTTPException(409, "Job is not ready for rendering")

    job["status"] = "rendering"
    job["stage"] = "rendering"

    async def render_task() -> None:
        import pipeline as pl

        def log(text: str) -> None:
            job["logs"].append(text)

        try:
            clips: list[dict] = job["clips"] or []
            video_path = Path(job["video_path"])
            with open(job["transcription_path"], encoding="utf-8") as f:
                transcription = json.load(f)
            segments: list[dict] = transcription.get("segments", [])

            indices = req.indices if req.indices is not None else list(range(len(clips)))
            clips_dir = PROJECT_DIR / "clips"
            loop = asyncio.get_running_loop()
            src_aspect = job.get("src_aspect", 16 / 9)

            for idx in indices:
                if job.get("status") == "canceled":
                    break
                clip = clips[idx]
                log(f"▶ Rendering [{idx}] {clip.get('title', '')}")

                def set_proc(p, _job=job):
                    _job["_proc"] = p

                def check_cancel(_job=job):
                    return _job.get("status") == "canceled"

                out = await loop.run_in_executor(
                    None, pl.render_clip, clip, video_path, segments, idx, log, clips_dir, check_cancel, set_proc, src_aspect
                )
                if job.get("status") == "canceled":
                    break
                job["rendered"].append(out.name)
                log(f"✓ {out.name}")

            if job.get("status") != "canceled":
                job["status"] = "complete"
                job["stage"] = "complete"
                log("✓ All clips rendered")

        except Exception as exc:
            if job.get("status") != "canceled":
                job["status"] = "error"
                job["error"] = str(exc)
                log(f"✗ {exc}")

    asyncio.create_task(render_task())
    return {"ok": True}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] not in ("running", "rendering", "transcribing", "suggesting", "downloading"):
        raise HTTPException(400, "Job is not cancelable in its current state")
    
    job["status"] = "canceled"
    job["stage"] = "canceled"
    job["logs"].append("⚠ ジョブがキャンセルされました。")
    
    proc = job.get("_proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass

    return {"ok": True}


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(400, "Invalid filename")
    dest = DOWNLOADS_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"filename": safe_name}


@app.get("/api/clips/{filename}")
def serve_clip(filename: str):
    path = PROJECT_DIR / "clips" / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/api/files")
def list_files(type: str = Query(...)):
    configs: dict[str, tuple[Path, list[str]]] = {
        "video":         (DOWNLOADS_DIR,      ["*.mp4"]),
        "audio":         (DOWNLOADS_DIR,      ["*.mp4", "*.flac", "*.mp3", "*.wav"]),
        "chat":          (DOWNLOADS_DIR,      ["*.live_chat.json"]),
        "transcription": (TRANSCRIPTIONS_DIR, ["*_full.json"]),
        "clips":         (TRANSCRIPTIONS_DIR, ["clips*.json"]),
    }
    if type not in configs:
        raise HTTPException(400, f"Unknown type: {type}")
    base, patterns = configs[type]
    if not base.exists():
        return []
    files: set[Path] = set()
    for pat in patterns:
        files.update(base.glob(pat))
    return [f.name for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


class StudioReq(BaseModel):
    video_path: str
    transcription_path: str
    clips: list[dict]


_studio_video_dir: str | None = None  # public-dir the current Studio was started with


def _make_captions_ts(segments: list[dict], start_sec: float, end_sec: float) -> list[dict]:
    captions = []
    for seg in segments:
        s = seg.get("start", 0)
        e = seg.get("end", 0)
        if e <= start_sec or s >= end_sec:
            continue
        start_ms = max(0.0, (s - start_sec) * 1000)
        end_ms = (min(e, end_sec) - start_sec) * 1000
        text = seg.get("text", "").strip()
        if not text:
            continue
        if len(text) >= 18:
            split_idx = text.find("、")
            if split_idx == -1 or split_idx < 5 or split_idx > len(text) - 5:
                split_idx = len(text) // 2
            else:
                split_idx += 1
            half_time = start_ms + (end_ms - start_ms) * (split_idx / len(text))
            captions.append({"text": " " + text[:split_idx].strip(), "startMs": start_ms, "endMs": half_time})
            captions.append({"text": " " + text[split_idx:].strip(), "startMs": half_time, "endMs": end_ms})
        else:
            captions.append({"text": " " + text, "startMs": start_ms, "endMs": end_ms})
    return captions


def _generate_studio_compositions(video_src: str, segments: list[dict], clips: list[dict], src_aspect: float = 16 / 9) -> str:
    """Generate studioCompositions.tsx with static named consts so Remotion can save props."""
    lines = [
        "// Auto-generated by Kirinuki web app — do not edit manually",
        'import { Composition } from "remotion";',
        "import {",
        "  ClipComposition,",
        "  calculateMetadata,",
        "  clipSchema,",
        "  type ClipProps,",
        '} from "./ClipComposition";',
        "",
    ]

    for i, clip in enumerate(clips):
        captions = _make_captions_ts(segments, clip["start_sec"], clip["end_sec"])
        props: dict = {
            "videoSrc": video_src,
            "startSec": clip["start_sec"],
            "endSec": clip["end_sec"],
            "vertical": bool(clip.get("vertical", False)),
            "verticalMode": clip.get("verticalMode", "crop"),
            "cropX": float(clip.get("cropX", 90)),
            "faceCamZoom": float(clip.get("faceCamZoom", 1.5)),
            "faceCamY": float(clip.get("faceCamY", 50)),
            "title": clip.get("title", ""),
            "captions": captions,
            "srcAspect": clip.get("srcAspect", src_aspect),
        }
        if clip.get("captionFontSize"):
            props["captionFontSize"] = int(clip["captionFontSize"])
        if clip.get("keepIntervals"):
            props["keepIntervals"] = clip["keepIntervals"]
        if clip.get("theme"):
            props["theme"] = clip["theme"]
        lines.append(
            f"const clip{i:02d}Props: ClipProps = {json.dumps(props, ensure_ascii=False, indent=2)};"
        )
        lines.append("")

    lines.append("export const StudioCompositions: React.FC = () => (")
    lines.append("  <>")
    for i, clip in enumerate(clips):
        keep_ivs = clip.get("keepIntervals")
        if keep_ivs:
            dur_sec = sum(iv["endSec"] - iv["startSec"] for iv in keep_ivs)
        else:
            dur_sec = clip["end_sec"] - clip["start_sec"]
        dur = max(1, round(dur_sec * 30))
        lines += [
            f'    <Composition',
            f'      id="clip-{i:02d}"',
            f"      component={{ClipComposition}}",
            f"      schema={{clipSchema}}",
            f"      defaultProps={{clip{i:02d}Props}}",
            f"      calculateMetadata={{calculateMetadata}}",
            f"      durationInFrames={{{dur}}}",
            f"      fps={{30}}",
            f"      width={{1920}}",
            f"      height={{1080}}",
            f"    />",
        ]
    lines.append("  </>")
    lines.append(");")

    return "\n".join(lines) + "\n"


@app.post("/api/studio/open")
async def open_studio(req: StudioReq):
    global _studio_proc, _studio_video_dir

    video_path = _resolve(req.video_path, DOWNLOADS_DIR)
    transcription_path = _resolve(req.transcription_path, TRANSCRIPTIONS_DIR)

    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {req.video_path}")
    if not transcription_path.exists():
        raise HTTPException(404, f"文字起こしが見つかりません: {req.transcription_path}")

    with open(transcription_path, encoding="utf-8") as f:
        transcription = json.load(f)

    segments = transcription.get("segments", [])

    # Migrate any old-style split clips (_concat_group) to keepIntervals before Studio
    import pipeline as _pl
    merged_clips = _pl.merge_split_clips(req.clips)

    # Detect source dimensions for correct aspect ratio in compositions
    vw, vh = _pl.get_video_dimensions(video_path)
    studio_src_aspect = vw / vh if vh else 16 / 9

    # Generate static TypeScript file with named consts — lets Remotion save props
    tsx = _generate_studio_compositions(video_path.name, segments, merged_clips, studio_src_aspect)
    compositions_path = REMOTION_DIR / "src" / "studioCompositions.tsx"
    if not compositions_path.exists() or compositions_path.read_text(encoding="utf-8") != tsx:
        compositions_path.write_text(tsx, encoding="utf-8")

    # Keep studioData.json updated for ClipComposition's internal fallback
    studio_data = {"videoSrc": video_path.name, "segments": segments, "clips": []}
    (REMOTION_DIR / "src" / "studioData.json").write_text(
        json.dumps(studio_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    video_dir = str(video_path.parent)
    studio_running = _studio_proc and _studio_proc.poll() is None

    if not studio_running or _studio_video_dir != video_dir:
        if studio_running:
            _studio_proc.terminate()
            try:
                _studio_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _studio_proc.kill()

        _studio_proc = subprocess.Popen(
            [
                "npx", "remotion", "studio",
                "--no-open",
                "--public-dir", video_dir,
            ],
            cwd=REMOTION_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _studio_video_dir = video_dir
        await asyncio.sleep(4)

    return {"url": "http://localhost:3009"}


# Serve frontend (must be mounted last)
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static",
)
