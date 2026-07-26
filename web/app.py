#!/usr/bin/env python3
"""Kirinuki Web — FastAPI backend."""

import atexit
import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

PROJECT_DIR = Path(__file__).parent.parent

# .env (dev convenience) loads first; config.json (settings screen, packaged installs)
# is applied on top of it — see web/config.py.
load_dotenv(PROJECT_DIR / ".env")
import config as cfg  # noqa: E402
import theme_store  # noqa: E402

cfg.load_settings()
cfg.bootstrap_bin_path()

DATA_DIR = cfg.get_data_dir()
DOWNLOADS_DIR = DATA_DIR / "downloads"
TRANSCRIPTIONS_DIR = DATA_DIR / "transcriptions"
REMOTION_DIR = PROJECT_DIR / "remotion"
# Studio 専用の使い捨てソースツリー（remotion/src のコピー）— _sync_studio_src() 参照
STUDIO_SRC_DIR = REMOTION_DIR / "studio-src"
# remotion.config.ts の Config.setStudioPort(3009) と一致させること
STUDIO_PORT = 3009
CLIPS_DIR = DATA_DIR / "clips"
EXPORTS_DIR = DATA_DIR / "exports"
LOGS_DIR = cfg.get_log_dir()


def migrate_legacy_transcriptions() -> None:
    """Migrate any files from legacy PROJECT_DIR/transcriptions into TRANSCRIPTIONS_DIR and cleanup."""
    legacy_dir = PROJECT_DIR / "transcriptions"
    if legacy_dir.exists() and legacy_dir.resolve() != TRANSCRIPTIONS_DIR.resolve():
        TRANSCRIPTIONS_DIR.mkdir(parents=True, exist_ok=True)
        for item in legacy_dir.iterdir():
            if item.is_file():
                dest = TRANSCRIPTIONS_DIR / item.name
                if not dest.exists():
                    try:
                        shutil.move(str(item), str(dest))
                    except Exception:
                        pass
        try:
            if not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
        except Exception:
            pass


migrate_legacy_transcriptions()


def migrate_legacy_clips() -> None:
    """Migrate rendered clips from the old DATA_DIR/remotion/out layout (CLIPS_DIR was
    nested under a "remotion" folder to mirror Remotion CLI's own output convention) into
    CLIPS_DIR, now a DATA_DIR/clips sibling of downloads/transcriptions/exports."""
    legacy_dir = DATA_DIR / "remotion" / "out"
    if legacy_dir.exists() and legacy_dir.resolve() != CLIPS_DIR.resolve():
        CLIPS_DIR.mkdir(parents=True, exist_ok=True)
        for item in legacy_dir.iterdir():
            if item.is_file():
                dest = CLIPS_DIR / item.name
                if not dest.exists():
                    try:
                        shutil.move(str(item), str(dest))
                    except Exception:
                        pass
        try:
            if not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
        except Exception:
            pass


migrate_legacy_clips()

app = FastAPI(title="Kirinuki Web")

# ── In-memory job store ───────────────────────────────────────────────────────

jobs: dict[str, dict[str, Any]] = {}
_active_job: str | None = None
_studio_proc: subprocess.Popen | None = None

import os
import signal
import time


@app.on_event("shutdown")
async def shutdown_event():
    _shutdown_studio_proc()


@app.post("/api/system/open-folder")
async def open_folder_endpoint(target: str = Query("data")):
    """指定されたフォルダ（data / install）をOSのファイルマネージャーで開く"""
    path = DATA_DIR if target == "data" else PROJECT_DIR
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
        return {"status": "ok", "opened": str(path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/system/open-cookie-guide")
async def open_cookie_guide_endpoint():
    """Cookieの手動保存手順テキストファイルをOSの標準テキストエディタ（メモ帳など）で開く"""
    guide_path = cfg.ensure_cookie_guide_file(DATA_DIR)
    try:
        if sys.platform == "win32":
            os.startfile(str(guide_path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(guide_path)])
        else:
            subprocess.run(["xdg-open", str(guide_path)])
        return {"status": "ok", "opened": str(guide_path)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/api/system/shutdown")
async def shutdown_endpoint():
    """サーバープロセスを停止する"""
    _shutdown_studio_proc()
    def _exit_later():
        time.sleep(0.5)
        os.kill(os.getpid(), signal.SIGINT)

    asyncio.get_event_loop().run_in_executor(None, _exit_later)
    return {"status": "shutting_down"}



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
        "_video_quality": req.video_quality,
        "_in_video": req.video_path,
        "_in_transcription": req.transcription_path,
        "_transcription_prompt": req.transcription_prompt,
        "_in_clips": req.clips_path,
        "_audio_mode": req.audio_mode,
        "_transcription_model": req.transcription_model,
        "_gemini_model": req.gemini_model,
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
    if p.is_absolute() and p.exists():
        return p
    resolved = base / name
    if resolved.exists():
        return resolved
    legacy = PROJECT_DIR / base.name / name
    if legacy.exists():
        return legacy
    return resolved


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
            quality = job.get("_video_quality", pl.DEFAULT_VIDEO_QUALITY)
            log(f"▶ 画質: {'フルHD (1080p)' if quality == '1080' else '720p'}")
            video_path, chat_path = pl.download_video(
                job["_url"], DOWNLOADS_DIR, log, quality=quality,
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
                        job.get("_transcription_model", "elevenlabs"),
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

        # Live chat is only ever read from this dedicated-folder copy, saved once here
        # alongside its transcription — never straight out of downloads/.
        if chat_path and chat_path.exists():
            chat_path = pl.save_chat(chat_path, t_path)
            job["chat_path"] = str(chat_path)
            log(f"✓ Chat saved: {chat_path.name}")

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
            log("▶ Asking Gemini for clip suggestions...")
            assert result is not None
            job["clips"] = pl.suggest_clips_from_result(
                result, chat_path, job.get("_extra_prompt"), job.get("_gemini_model")
            )
            clips_file = pl.save_clips(job["clips"], t_path)
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
                clips_file = pl.save_clips(job["clips"], t_path)
                job["clips_path"] = str(clips_file)

        if result is not None:
            job["clips"] = pl.enrich_clip_caption_effects(
                job["clips"], result.get("segments", [])
            )

        # Migrate any old-style split clips (_concat_group) to cutIntervals format
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
    video_quality: str = "1080"            # ダウンロード画質: "720" | "1080"（既定はフルHD）
    # Skip phases by providing existing files (filenames relative to their dirs)
    video_path: str | None = None          # skip download
    transcription_path: str | None = None  # skip transcription
    transcription_prompt: str | None = None  # initial prompt for Whisper transcription
    audio_mode: str = "mp3"               # audio conversion: "mp3" | "flac_fast" | "stream_copy"
    transcription_model: str = "elevenlabs"  # transcription backend: "elevenlabs" | "groq" (Gemini is suggestion-only)
    gemini_model: str | None = None        # clip-suggestion model override (default: pipeline.GEMINI_MODEL_ID)
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


class SettingsReq(BaseModel):
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    elevenlabs_keyterms: str | None = None


@app.get("/api/settings")
def get_settings():
    return cfg.settings_status()


@app.put("/api/settings")
def update_settings(req: SettingsReq):
    cfg.save_settings({
        "GROQ_API_KEY": req.groq_api_key,
        "GEMINI_API_KEY": req.gemini_api_key,
        "ELEVENLABS_API_KEY": req.elevenlabs_api_key,
        "ELEVENLABS_KEYTERMS": req.elevenlabs_keyterms,
    })
    return cfg.settings_status()


class ThemeReq(BaseModel):
    label: str
    titleBackground: str
    titleTextColor: str
    titleAccentColor: str
    captionTextColor: str
    captionActiveColor: str
    captionActiveGlow: str
    captionFont: str | None = None
    titleFont: str | None = None
    titleBarMinHeight: float | None = None
    titleTopMargin: float | None = None


@app.get("/api/themes")
def list_themes():
    return theme_store.list_themes()


@app.post("/api/themes")
def create_theme(req: ThemeReq):
    theme_id, theme = theme_store.create_theme(req.model_dump())
    return {"id": theme_id, "theme": theme}


@app.put("/api/themes/default/{theme_id}")
def set_default_theme(theme_id: str):
    try:
        theme_store.set_default_theme(theme_id)
    except KeyError:
        raise HTTPException(404, "テーマが見つかりません")
    return theme_store.list_themes()


@app.put("/api/themes/{theme_id}")
def update_theme(theme_id: str, req: ThemeReq):
    try:
        theme = theme_store.update_theme(theme_id, req.model_dump())
    except KeyError:
        raise HTTPException(404, "テーマが見つかりません")
    return {"id": theme_id, "theme": theme}


@app.delete("/api/themes/{theme_id}")
def delete_theme(theme_id: str):
    try:
        theme_store.delete_theme(theme_id)
    except ValueError:
        raise HTTPException(400, "組み込みテーマは削除できません")
    except KeyError:
        raise HTTPException(404, "テーマが見つかりません")
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(req: StartReq):
    if _active_job:
        raise HTTPException(409, "別のジョブが実行中です")

    missing: list[str] = []
    if not req.chat_only and req.stop_after != "download":
        status = cfg.settings_status()
        key_labels = {"gemini": "Gemini", "groq": "Groq", "elevenlabs": "ElevenLabs"}
        transcription_keys = {"groq", "elevenlabs"}

        def require(key: str) -> None:
            if not status[key] and key_labels[key] not in missing:
                missing.append(key_labels[key])

        if not req.transcription_path:
            needed_key = req.transcription_model if req.transcription_model in transcription_keys else "elevenlabs"
            require(needed_key)
        # suggest_clips_from_result() always uses Gemini, regardless of transcription_model
        if req.clips is None and not req.clips_path:
            require("gemini")
    if missing:
        raise HTTPException(400, f"{'・'.join(missing)} のAPIキーが設定画面で未設定です。")

    jid = _new_job(req)
    asyncio.create_task(_pipeline_task(jid))
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404, "Job not found")
    # Underscore-prefixed entries are internal (pipeline inputs, and "_proc" — the
    # live subprocess handle, which FastAPI cannot serialise and which used to make
    # this endpoint 500 for the whole duration of a render or export).
    return {k: v for k, v in job.items() if not k.startswith("_")}


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
            yield f"data: {json.dumps({'type': 'state', 'status': job['status'], 'stage': job['stage'], 'clips': job['clips'], 'rendered': job['rendered'], 'error': job['error'], 'video_path': job['video_path'], 'transcription_path': job['transcription_path'], 'chat_path': job.get('chat_path'), 'clips_path': job.get('clips_path'), 'src_aspect': job.get('src_aspect'), 'export': job.get('export')})}\n\n"
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
            clips_dir = CLIPS_DIR
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


class ExportPremiereReq(BaseModel):
    indices: list[int] | None = None  # None = all clips


@app.post("/api/jobs/{jid}/export-premiere")
async def export_premiere(jid: str, req: ExportPremiereReq):
    """Start an editing-material export (caption-free mp4 + matching SRT per clip).

    Runs in the background like /render — each clip is a full ffmpeg re-encode, so
    this takes minutes, not milliseconds. Progress arrives over the job's SSE
    stream, and the finished package lands in the state message's `export` field.
    """
    import datetime

    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] not in ("ready", "complete"):
        raise HTTPException(409, "ジョブが書き出せる状態ではありません")
    if not job.get("video_path"):
        raise HTTPException(400, "動画ファイルが選択されていません")
    if not job.get("transcription_path"):
        raise HTTPException(400, "文字起こしファイルが選択されていません")

    clips: list[dict] = job["clips"] or []
    if not clips:
        raise HTTPException(400, "クリップが読み込まれていません")

    indices = req.indices if req.indices is not None else list(range(len(clips)))
    invalid = [i for i in indices if not 0 <= i < len(clips)]
    if invalid:
        raise HTTPException(400, f"クリップ番号が範囲外です: {invalid}")
    if not indices:
        raise HTTPException(400, "書き出すクリップを選択してください")

    video_path = Path(job["video_path"])
    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {video_path.name}")

    with open(job["transcription_path"], encoding="utf-8") as f:
        segments = json.load(f).get("segments", [])

    job["status"] = "exporting"
    job["stage"] = "exporting"
    job["export"] = None

    def log(text: str) -> None:
        job["logs"].append(text)

    def build() -> tuple[Path, Path]:
        import premiere_export as px

        def set_proc(p, _job=job):
            _job["_proc"] = p

        def check_cancel(_job=job):
            return _job.get("status") == "canceled"

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        pkg_dir = EXPORTS_DIR / f"premiere_{video_path.stem[:40]}_{ts}"
        px.export_package(
            clips=clips,
            indices=indices,
            video_path=video_path,
            segments=segments,
            out_dir=pkg_dir,
            src_aspect=job.get("src_aspect") or 16 / 9,
            log=log,
            check_cancel=check_cancel,
            set_proc=set_proc,
        )
        return pkg_dir, Path(shutil.make_archive(str(pkg_dir), "zip", root_dir=pkg_dir))

    async def export_task() -> None:
        log(f"▶ Premiere用に書き出し: {len(indices)} 件（構図・カット適用、字幕なし）")
        try:
            loop = asyncio.get_running_loop()
            pkg_dir, zip_path = await loop.run_in_executor(None, build)
        except Exception as exc:
            if job.get("status") != "canceled":
                job["status"] = "error"
                job["stage"] = "error"
                job["error"] = str(exc)
                log(f"✗ {exc}")
            return
        finally:
            job.pop("_proc", None)

        if job.get("status") == "canceled":
            return
        job["export"] = {
            "filename": zip_path.name,
            "directory": str(pkg_dir),
            "count": len(indices),
        }
        job["status"] = "complete"
        job["stage"] = "complete"
        log(f"✓ {zip_path.name}")

    asyncio.create_task(export_task())
    return {"ok": True, "count": len(indices)}


@app.get("/api/exports/{filename}")
def serve_export(filename: str):
    path = EXPORTS_DIR / Path(filename).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    job = jobs.get(jid)
    if not job:
        raise HTTPException(404)
    if job["status"] not in ("running", "rendering", "exporting", "transcribing", "suggesting", "downloading"):
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


class SplitGeometryReq(BaseModel):
    title: str = ""
    splitTopRatio: float = 4.5
    theme: str | None = None


@app.post("/api/split-geometry")
def split_geometry(req: SplitGeometryReq):
    """Panel bounds of 二段構成モード for the position picker.

    The picker turns a rectangle drawn on a video frame into that panel's zoom and
    position (上段: mainZoom / mainCropX / mainCropY, 下段: faceCamZoom / cropX /
    faceCamY), which needs the panel's height — and both depend on the title bar,
    whose height only premiere_export's port of calcTitleBar() can work out.

    The title bar's height/margin can be overridden per theme (titleBarMinHeight /
    titleTopMargin), so `theme` selects which one to resolve them from — same key
    the clip cards send as `theme` when building render props.
    """
    import premiere_export as px

    theme_colors = theme_store.resolve_theme_props(req.theme).get("themeColors") or {}
    top_margin = theme_colors.get("titleTopMargin")
    safe_top_ratio = (top_margin / 100) if top_margin is not None else px.SHORTS_SAFE_TOP_RATIO
    title_bar_min_height = theme_colors.get("titleBarMinHeight") or px.MIN_TITLE_BAR_H
    g = px.split_geometry(
        req.title, max(1, min(9, req.splitTopRatio)),
        safe_top_ratio=safe_top_ratio, title_bar_min_height=title_bar_min_height,
    )
    return {
        "seqW": g.seq_w,
        "seqH": g.seq_h,
        "titleBarHeight": g.title_bar_h,
        "mainTop": g.main_top,
        "mainH": g.main_h,
        "bottomTop": g.bottom_top,
        "bottomH": g.bottom_h,
    }


class ClipSegmentsReq(BaseModel):
    transcription_path: str
    start_sec: float
    end_sec: float


@app.post("/api/clip-segments")
def get_clip_segments(req: ClipSegmentsReq):
    """Transcript segments overlapping a clip's time range, each with its `isComment`
    flag — backs the clip card's per-line 💬 comment-bubble checkbox list."""
    path = _resolve(req.transcription_path, TRANSCRIPTIONS_DIR)
    if not path.exists():
        raise HTTPException(404, f"文字起こしが見つかりません: {req.transcription_path}")
    with open(path, encoding="utf-8") as f:
        segments = json.load(f).get("segments", [])
    out = [
        {
            "index": i,
            "start": s.get("start", 0),
            "end": s.get("end", 0),
            "text": s.get("text", ""),
            "isComment": bool(s.get("is_comment")),
        }
        for i, s in enumerate(segments)
        if s.get("end", 0) > req.start_sec and s.get("start", 0) < req.end_sec
    ]
    return {"segments": out}


class SegmentCommentReq(BaseModel):
    transcription_path: str
    index: int
    is_comment: bool


@app.post("/api/segment-comment")
def set_segment_comment(req: SegmentCommentReq):
    """Persists one transcript segment's comment-bubble flag (see get_clip_segments)."""
    path = _resolve(req.transcription_path, TRANSCRIPTIONS_DIR)
    if not path.exists():
        raise HTTPException(404, f"文字起こしが見つかりません: {req.transcription_path}")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments", [])
    if not (0 <= req.index < len(segments)):
        raise HTTPException(400, "無効なセグメント index です")
    if req.is_comment:
        segments[req.index]["is_comment"] = True
    else:
        segments[req.index].pop("is_comment", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return {"ok": True}


@app.get("/api/frame")
def video_frame(video: str = Query(...), t: float = Query(0.0)):
    """A single JPEG frame from a downloaded video, for the position picker."""
    path = _resolve(Path(video).name, DOWNLOADS_DIR)
    if not path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {path.name}")

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, t):.3f}", "-i", str(path),
        "-frames:v", "1",
        # Downscaled only when the source is larger — the picker maps the rectangle
        # in normalised coordinates, so the exact pixel size does not matter.
        "-vf", "scale='min(1280,iw)':-2",
        "-q:v", "3", "-f", "image2", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120, **cfg.no_window_kwargs())
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise HTTPException(500, f"フレームを取得できませんでした: {detail[-1] if detail else '不明なエラー'}")
    # The URL identifies the frame (file name + timestamp), so caching it saves the
    # picker an ffmpeg run every time it re-reads the same frame.
    return Response(content=proc.stdout, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/clips/{filename}")
def serve_clip(filename: str):
    path = CLIPS_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/api/files")
def list_files(type: str = Query(...)):
    configs: dict[str, tuple[Path, list[str]]] = {
        "video":           (DOWNLOADS_DIR,        ["*.mp4"]),
        "audio":           (DOWNLOADS_DIR,        ["*.mp4", "*.flac", "*.mp3", "*.wav"]),
        "chat":            (DOWNLOADS_DIR,        ["*.live_chat.json"]),
        "transcription":   (TRANSCRIPTIONS_DIR,   ["*_full.json"]),
        "clips":           (TRANSCRIPTIONS_DIR,   ["clips*.json"]),
        "rendered_clips":  (CLIPS_DIR,              ["*.mp4"]),
        "exports":         (EXPORTS_DIR,          ["*.zip"]),
    }
    if type not in configs:
        raise HTTPException(400, f"Unknown type: {type}")
    base, patterns = configs[type]
    files: set[Path] = set()
    for b in (base, PROJECT_DIR / base.name):
        if b.exists():
            for pat in patterns:
                files.update(b.glob(pat))
    return [f.name for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]


class StudioReq(BaseModel):
    video_path: str
    transcription_path: str
    clips: list[dict]


_studio_video_path: str | None = None  # source video the current Studio was started with
_studio_pub_dir: Path | None = None  # scratch --public-dir Studio was started with (see open_studio)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _kill_process_on_port(port: int) -> None:
    """Force-free a port that a previous, no-longer-tracked Studio process is still holding.

    `npx remotion studio` spawns a real `node` process under the `npx` shim, and on --reload
    restarts (or an abrupt kill of the parent, e.g. via Task Manager) the shim's
    death doesn't take that child with it. The next launch then finds the port already bound
    and Remotion Studio exits immediately — this recovers by finding and killing whatever is
    actually listening, independent of our in-memory _studio_proc handle.
    """
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, **cfg.no_window_kwargs()
            ).stdout
            pids = {
                line.split()[-1]
                for line in out.splitlines()
                if f":{port}" in line and "LISTENING" in line
            }
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid], **cfg.no_window_kwargs())
        else:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True
            ).stdout
            for pid in out.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except FileNotFoundError:
        pass  # lsof/netstat unavailable — best-effort only


def _shutdown_studio_proc() -> None:
    """Kill the tracked Studio process (if any) so the server never exits leaving it orphaned."""
    global _studio_proc, _studio_pub_dir, _studio_video_path
    if _studio_proc and _studio_proc.poll() is None:
        _terminate_studio_proc(_studio_proc)
    _studio_proc = None
    _kill_process_on_port(STUDIO_PORT)
    if _studio_pub_dir is not None:
        shutil.rmtree(_studio_pub_dir, ignore_errors=True)
        _studio_pub_dir = None
    _studio_video_path = None


atexit.register(_shutdown_studio_proc)


def _terminate_studio_proc(proc: subprocess.Popen) -> None:
    """Kill the whole `npx remotion studio` tree, not just the npx shim (see _kill_process_on_port)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)], **cfg.no_window_kwargs()
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, subprocess.SubprocessError):
            pass


# studio-src/ 側にだけ生成するファイル（src/ にはスタブを置く）
_STUDIO_GENERATED = ("studioCompositions.tsx", "studioData.json")

_STUB_COMPOSITIONS_TSX = """\
// Auto-generated by Kirinuki web app — do not edit manually
// 本物のプレビュー用コンポジションは studio-src/ 側に生成される（_sync_studio_src 参照）。
// CLI レンダリングが使うのは Root.tsx の ClipComposition だけなので、
// こちらは import を満たすだけの空実装でよい。
export const StudioCompositions: React.FC = () => null;
"""

_STUB_STUDIO_DATA = '{\n  "videoSrc": "",\n  "segments": [],\n  "clips": []\n}\n'

# Remotion composition ids may only contain a-z, A-Z, 0-9, CJK ideographs (一-鿿) and
# "-" (see remotion/node_modules/remotion/dist/esm/index.mjs validateCompositionId) — notably
# NOT hiragana/katakana or punctuation, both of which dominate real clip titles. So a title like
# 「まさかの結末がヤバすぎるｗ」 keeps only 結末 as an id fragment; still far more useful in the
# Studio sidebar than a bare index when the title has kanji, and harmless (falls back to just
# the index) when it doesn't.
_COMPOSITION_ID_INVALID = re.compile(r"[^a-zA-Z0-9一-鿿]+")


def _composition_id_slug(title: str, max_len: int = 20) -> str:
    return _COMPOSITION_ID_INVALID.sub("-", title).strip("-")[:max_len].strip("-")


def _sync_studio_src() -> list[str]:
    """remotion/src を使い捨ての remotion/studio-src/ にミラーし、上書きした分を返す。

    Remotion Studio はタイムラインやキャンバス上の編集（Sequence の Offset、要素の
    Scale / Translate / Rotate、Props エディタの保存）を**ソースファイルに直接書き戻す**。
    Studio をこのコピーに対して起動しておけば書き戻し先はコピー側になり、「Studio で確認」
    を押すたびにここで上書きされて元に戻る。remotion/src 自体は汚れないので、CLI
    レンダリング（pipeline.render_clip）が Studio の編集に引きずられることもない。

    studio-src/ は remotion/ 直下（src/ と同階層）でなければならない:
    ClipComposition.tsx が `../public/Onoma-Pop04.mp3` を import しており、
    node_modules の解決も remotion/ を辿るため。
    """
    src_dir = REMOTION_DIR / "src"
    overwritten: list[str] = []

    for src_file in sorted(src_dir.rglob("*")):
        rel = src_file.relative_to(src_dir)
        if src_file.is_dir() or rel.name.startswith(".") or rel.name in _STUDIO_GENERATED:
            continue
        dst = STUDIO_SRC_DIR / rel
        data = src_file.read_bytes()
        if dst.exists():
            if dst.read_bytes() == data:
                continue
            overwritten.append(str(rel))  # Studio が書き換えたものを元に戻す
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    if STUDIO_SRC_DIR.exists():
        for dst in STUDIO_SRC_DIR.rglob("*"):
            rel = dst.relative_to(STUDIO_SRC_DIR)
            if dst.is_dir() or rel.name in _STUDIO_GENERATED:
                continue
            if not (src_dir / rel).exists():
                dst.unlink()

    return overwritten


def _write_if_changed(path: Path, text: str) -> None:
    """Rewrite only on change — Studio の無駄な再ビルドを避ける。"""
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _generate_studio_compositions(
    video_src: str,
    segments: list[dict],
    clips: list[dict],
    src_aspect: float = 16 / 9,
    reset_token: str = "",
) -> str:
    """Generate studioCompositions.tsx with defaultProps inlined as object literals
    directly in the JSX (not through a named const) — Remotion Studio can only save
    Props-editor edits back to a composition whose defaultProps is an inline object
    literal, per https://www.remotion.dev/docs/troubleshooting/cannot-save-default-props.

    Written to studio-src/ (not src/), so those saves land in the throwaway copy and are
    replaced by this freshly generated file on the next press — see _sync_studio_src().
    """
    import pipeline as _pl
    lines = [
        "// Auto-generated by Kirinuki web app — do not edit manually",
        'import { Composition } from "remotion";',
        "import {",
        "  ClipComposition,",
        "  calculateMetadata,",
        "  clipSchema,",
        '} from "./ClipComposition";',
        'import { resetStudioViewState } from "./studioViewReset";',
        "",
        "// 「Studio で確認」を押すたびに変わるトークン。プレビューの拡大率/表示位置を戻す。",
        f'resetStudioViewState({json.dumps(reset_token)});',
        "",
        "export const StudioCompositions: React.FC = () => (",
        "  <>",
    ]

    for i, clip in enumerate(clips):
        effects_enabled = bool(clip.get("captionEffectsEnabled", True))
        captions = _pl.make_captions(
            segments,
            clip["start_sec"],
            clip["end_sec"],
            clip.get("captionEffect"),
            effects_enabled=effects_enabled,
        )
        props: dict = {
            "videoSrc": video_src,
            "startSec": clip["start_sec"],
            "endSec": clip["end_sec"],
            "vertical": bool(clip.get("vertical", False)),
            "verticalMode": clip.get("verticalMode", "split"),
            "cropX": float(clip.get("cropX", 90)),
            "faceCamZoom": float(clip.get("faceCamZoom", 1.5)),
            "faceCamY": float(clip.get("faceCamY", 50)),
            "splitTopRatio": float(clip.get("splitTopRatio", 4.5)),
            "mainZoom": float(clip.get("mainZoom", 1.0)),
            "mainCropX": float(clip.get("mainCropX", 50)),
            "mainCropY": float(clip.get("mainCropY", 50)),
            "title": clip.get("title", ""),
            "captions": captions,
            "srcAspect": clip.get("srcAspect", src_aspect),
        }
        if clip.get("captionFontSize"):
            props["captionFontSize"] = int(clip["captionFontSize"])
        if effects_enabled and clip.get("captionEffect") in _pl.CAPTION_EFFECTS:
            props["captionEffect"] = clip["captionEffect"]
        if clip.get("captionFont"):
            props["captionFont"] = clip["captionFont"]
        if clip.get("cutIntervals"):
            props["cutIntervals"] = clip["cutIntervals"]
        props.update(theme_store.resolve_theme_props(clip.get("theme")))

        cut_ivs = clip.get("cutIntervals")
        if cut_ivs:
            dur_sec = (clip["end_sec"] - clip["start_sec"]) - sum(iv["endSec"] - iv["startSec"] for iv in cut_ivs)
        else:
            dur_sec = clip["end_sec"] - clip["start_sec"]
        dur = max(1, round(dur_sec * 30))

        props_json = json.dumps(props, ensure_ascii=False, indent=2)
        props_json_indented = "\n".join(
            line if idx == 0 else "      " + line
            for idx, line in enumerate(props_json.splitlines())
        )
        title_slug = _composition_id_slug(clip.get("title", ""))
        comp_id = f"clip-{i:02d}-{title_slug}" if title_slug else f"clip-{i:02d}"
        lines += [
            f'    <Composition',
            f'      id="{comp_id}"',
            f"      component={{ClipComposition}}",
            f"      schema={{clipSchema}}",
            f"      defaultProps={{{props_json_indented}}}",
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
    global _studio_proc, _studio_video_path, _studio_pub_dir

    video_path = _resolve(req.video_path, DOWNLOADS_DIR)
    transcription_path = _resolve(req.transcription_path, TRANSCRIPTIONS_DIR)

    if not video_path.exists():
        raise HTTPException(404, f"動画ファイルが見つかりません: {req.video_path}")
    if not transcription_path.exists():
        raise HTTPException(404, f"文字起こしが見つかりません: {req.transcription_path}")

    with open(transcription_path, encoding="utf-8") as f:
        transcription = json.load(f)

    segments = transcription.get("segments", [])

    # Migrate any old-style split clips (_concat_group) to cutIntervals before Studio
    import pipeline as _pl
    merged_clips = _pl.merge_split_clips(req.clips)

    # Detect source dimensions for correct aspect ratio in compositions
    vw, vh = _pl.get_video_dimensions(video_path)
    studio_src_aspect = vw / vh if vh else 16 / 9

    # Studio 用のソースを毎回コピーし直す — 前回 Studio 上でずらした Offset / Scale 等の
    # 書き戻しはここで消える。remotion/src が唯一の正で、studio-src/ は使い捨て。
    reverted = _sync_studio_src()

    # CLI レンダリング側（remotion/src）は Root.tsx の import を満たすスタブで足りる
    _write_if_changed(REMOTION_DIR / "src" / "studioCompositions.tsx", _STUB_COMPOSITIONS_TSX)
    _write_if_changed(REMOTION_DIR / "src" / "studioData.json", _STUB_STUDIO_DATA)

    # Generate static TypeScript file — the reset token is fresh on every press, so the
    # file always changes and studioViewReset.ts resets the preview zoom/pan Studio persists.
    tsx = _generate_studio_compositions(
        video_path.name, segments, merged_clips, studio_src_aspect, uuid.uuid4().hex
    )
    (STUDIO_SRC_DIR / "studioCompositions.tsx").write_text(tsx, encoding="utf-8")

    # Keep studioData.json updated for ClipComposition's internal fallback
    studio_data = {"videoSrc": video_path.name, "segments": segments, "clips": []}
    _write_if_changed(
        STUDIO_SRC_DIR / "studioData.json",
        json.dumps(studio_data, ensure_ascii=False, indent=2),
    )

    video_abs = video_path.resolve()

    studio_running = _studio_proc and _studio_proc.poll() is None

    if not studio_running or _studio_video_path != str(video_abs):
        if studio_running:
            _terminate_studio_proc(_studio_proc)
        _studio_proc = None

        if _studio_pub_dir is not None:
            shutil.rmtree(_studio_pub_dir, ignore_errors=True)
            _studio_pub_dir = None

        # downloads/ 全体を --public-dir にすると、Remotionのbundlerがそこにある動画を
        # 全部一時ディレクトリへコピーしてしまう（render_clip() と同じ問題。pipeline.py の
        # コメント参照）。ここでもこの動画1本だけをスクラッチディレクトリに用意する。
        pub_tmp = Path(tempfile.mkdtemp(prefix="remotion_studio_pub_"))
        try:
            os.link(video_abs, pub_tmp / video_abs.name)
        except OSError:
            shutil.copy2(video_abs, pub_tmp / video_abs.name)
        for asset_name in ["kkrn_icon_user_2.png", "Onoma-Pop04.mp3"]:
            src = REMOTION_DIR / "public" / asset_name
            if src.exists():
                shutil.copy2(src, pub_tmp / asset_name)

        # 前回のプロセスをここまでで確実に止めていても、--reload によるワーカー再起動や
        # 親プロセスの強制終了（Task Manager など）を挟んだ場合は npx の孫プロセスだけが
        # ポートを掴んだまま生き残ることがある。_studio_proc の状態に関係なくポートの実態を
        # 見て、塞がっていれば起動前に強制的に空ける。
        if _port_in_use(STUDIO_PORT):
            _kill_process_on_port(STUDIO_PORT)
            for _ in range(10):
                if not _port_in_use(STUDIO_PORT):
                    break
                await asyncio.sleep(0.3)

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        studio_log = LOGS_DIR / "studio.log"
        # 新しいセッション/プロセスグループで起動 — npx シムだけでなくその配下の
        # node プロセスもまとめて確実に殺せるようにする（_terminate_studio_proc 参照）。
        # Windows は creationflags がビットフラグなので no_window_kwargs() の値と OR で合成する
        # （素直に dict をマージすると片方の creationflags が丸ごと上書きされてしまう）。
        popen_kwargs = dict(cfg.no_window_kwargs())
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                popen_kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True
        with open(studio_log, "w", encoding="utf-8") as logf:
            _studio_proc = subprocess.Popen(
                cfg.get_npx_cmd() + [
                    "remotion", "studio",
                    # エントリポイントは studio-src/ 側 — Studio の書き戻しから
                    # remotion/src を守るため（_sync_studio_src 参照）
                    "studio-src/index.ts",
                    "--no-open",
                    "--public-dir", str(pub_tmp),
                ],
                cwd=REMOTION_DIR,
                stdout=logf,
                stderr=subprocess.STDOUT,
                **popen_kwargs,
            )
        _studio_video_path = str(video_abs)
        _studio_pub_dir = pub_tmp

        # Studio HTTP サーバーが実際に 3009 ポートで接続可能になるまでポーリング待機（最大 20 秒）
        studio_ready = False
        for _ in range(40):
            if _studio_proc.poll() is not None:
                break
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", STUDIO_PORT)
                writer.close()
                await writer.wait_closed()
                studio_ready = True
                break
            except Exception:
                await asyncio.sleep(0.5)

        if not studio_ready and _studio_proc.poll() is not None:
            tail = studio_log.read_text(encoding="utf-8", errors="replace").strip()
            tail = "\n".join(tail.splitlines()[-5:])
            _studio_proc = None
            raise HTTPException(
                500,
                "Remotion Studio を起動できませんでした。別の Studio がポート 3009 で"
                f"動作している場合は終了してから再度お試しください。\n{tail}",
            )

    return {"url": f"http://localhost:{STUDIO_PORT}", "reverted": reverted}


class ConcatReq(BaseModel):
    filenames: list[str]


@app.post("/api/concat")
async def concat_clips(req: ConcatReq):
    import datetime
    import tempfile

    if len(req.filenames) < 2:
        raise HTTPException(400, "結合には2つ以上のファイルが必要です")

    clips_dir = CLIPS_DIR
    clips_dir.mkdir(exist_ok=True)

    input_paths: list[Path] = []
    for name in req.filenames:
        safe = Path(name).name
        p = clips_dir / safe
        if not p.exists():
            raise HTTPException(404, f"ファイルが見つかりません: {name}")
        input_paths.append(p)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_name = f"concat_{ts}.mp4"
    out_path = CLIPS_DIR / out_name

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in input_paths:
            f.write(f"file '{p.as_posix()}'\n")
        list_file = f.name

    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", str(out_path)]
        loop = asyncio.get_running_loop()
        proc = await loop.run_in_executor(
            None, lambda: subprocess.run(cmd, capture_output=True, **cfg.no_window_kwargs())
        )
        if proc.returncode != 0:
            raise HTTPException(500, f"ffmpeg concat 失敗: {proc.stderr.decode(errors='replace')[:300]}")
    finally:
        Path(list_file).unlink(missing_ok=True)

    return {"filename": out_name}


# Serve frontend (must be mounted last)
app.mount(
    "/",
    StaticFiles(directory=Path(__file__).parent / "static", html=True),
    name="static",
)
