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
        "_in_clips": req.clips_path,
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
            log("▶ Transcribing audio...")
            with pl.with_logging(log):
                result = pl.run_transcription(video_path, job["_language"])
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
            job["clips"] = pl.suggest_clips_from_result(result, chat_path)
            clips_file = pl.save_clips(job["clips"], video_path)
            job["clips_path"] = str(clips_file)
            log(f"✓ {len(job['clips'])} 件のクリップを提案 → {clips_file.name}")

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
    clips_path: str | None = None          # skip suggestion (load from file)
    clips: list[dict] | None = None        # skip suggestion (inline)

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
            yield f"data: {json.dumps({'type': 'state', 'status': job['status'], 'stage': job['stage'], 'clips': job['clips'], 'rendered': job['rendered'], 'error': job['error'], 'video_path': job['video_path'], 'transcription_path': job['transcription_path'], 'chat_path': job.get('chat_path'), 'clips_path': job.get('clips_path')})}\n\n"
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

            for idx in indices:
                if job.get("status") == "canceled":
                    break
                clip = clips[idx]
                log(f"▶ Rendering [{idx}] {clip.get('title', '')}")
                
                def set_proc(p):
                    job["_proc"] = p
                    
                def check_cancel():
                    return job.get("status") == "canceled"
                
                out = await loop.run_in_executor(
                    None, pl.render_clip, clip, video_path, segments, idx, log, clips_dir, check_cancel, set_proc
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
    """Python mirror of utils.ts makeCaptions — produces {text, startMs, endMs}."""
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

        # 長すぎる文章（18文字以上）は分割
        if len(text) >= 18:
            # 読点「、」があればそこで分割、なければ真ん中で分割
            split_idx = text.find("、")
            if split_idx == -1 or split_idx < 5 or split_idx > len(text) - 5:
                split_idx = len(text) // 2
            else:
                split_idx += 1 # 「、」を含める

            half_time = start_ms + (end_ms - start_ms) * (split_idx / len(text))

            captions.append({
                "text": " " + text[:split_idx].strip(),
                "startMs": start_ms,
                "endMs": half_time
            })
            captions.append({
                "text": " " + text[split_idx:].strip(),
                "startMs": half_time,
                "endMs": end_ms
            })
        else:
            captions.append({
                "text": " " + text,
                "startMs": start_ms,
                "endMs": end_ms,
            })
    return captions


def _generate_studio_compositions(video_src: str, segments: list[dict], clips: list[dict]) -> str:
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
        props = {
            "videoSrc": video_src,
            "startSec": clip["start_sec"],
            "endSec": clip["end_sec"],
            "vertical": bool(clip.get("vertical", False)),
            "cropX": float(clip.get("cropX", 90)),
            "title": clip.get("title", ""),
            "captions": captions,
        }
        # json.dumps output is valid TypeScript object literal syntax
        lines.append(
            f"const clip{i:02d}Props: ClipProps = {json.dumps(props, ensure_ascii=False, indent=2)};"
        )
        lines.append("")

    lines.append("export const StudioCompositions: React.FC = () => (")
    lines.append("  <>")
    for i, clip in enumerate(clips):
        dur = max(1, round((clip["end_sec"] - clip["start_sec"]) * 30))
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

    # Generate static TypeScript file with named consts — lets Remotion save props
    tsx = _generate_studio_compositions(video_path.name, segments, req.clips)
    (REMOTION_DIR / "src" / "studioCompositions.tsx").write_text(tsx, encoding="utf-8")

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

    return {"url": "http://localhost:3000"}


# Serve frontend (must be mounted last)
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static",
)
