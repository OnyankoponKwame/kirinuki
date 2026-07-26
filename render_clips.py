#!/usr/bin/env python3
"""Render video clips with subtitles using Remotion."""

import argparse
import json
import os
import sys
import subprocess
from pathlib import Path

REMOTION_DIR = Path(__file__).parent / "remotion"
PUBLIC_DIR = REMOTION_DIR / "public"
OUT_DIR = Path(__file__).parent / "clips"


def _no_window_kwargs() -> dict:
    """Extra subprocess.Popen/run kwargs to suppress a flashing console window on Windows."""
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        return {"creationflags": creationflags}
    return {}


def ensure_remotion_installed() -> None:
    node_modules = REMOTION_DIR / "node_modules"
    if not node_modules.exists():
        print("Installing Remotion dependencies...")
        subprocess.run(["npm", "install"], cwd=REMOTION_DIR, check=True, **_no_window_kwargs())


def make_captions(segments: list[dict], start_sec: float, end_sec: float) -> list[dict]:
    """Convert transcription segments to Remotion Caption format, relative to clip start."""
    captions = []
    for seg in segments:
        seg_start = seg.get("start", 0)
        seg_end = seg.get("end", 0)
        if seg_end <= start_sec or seg_start >= end_sec:
            continue
        captions.append({
            "text": seg.get("text", "").strip(),
            "startMs": max(0.0, (seg_start - start_sec) * 1000),
            "endMs": (min(seg_end, end_sec) - start_sec) * 1000,
            "timestampMs": max(0.0, (seg_start - start_sec) * 1000),
            "confidence": None,
        })
    return captions


def safe_filename(title: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in title)[:40]


def render_clip(
    clip: dict, video_path: Path, segments: list[dict], index: int
) -> Path:
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    start_sec = clip["start_sec"]
    end_sec = clip["end_sec"]
    title = clip.get("title", f"clip_{index:02d}")

    # Symlink video into public/ (avoids copying large files)
    video_link = PUBLIC_DIR / "clip_video.mp4"
    if video_link.is_symlink() or video_link.exists():
        video_link.unlink()
    video_link.symlink_to(video_path.absolute())

    captions = make_captions(segments, start_sec, end_sec)

    props = {
        "videoSrc": "clip_video.mp4",
        "startSec": start_sec,
        "endSec": end_sec,
        "captions": captions,
    }

    output_path = OUT_DIR / f"{index:02d}_{safe_filename(title)}.mp4"

    print(f"\n[{index}] Rendering: {title}")
    print(f"  {start_sec:.1f}s - {end_sec:.1f}s  →  {output_path.name}")

    subprocess.run(
        [
            "npx",
            "remotion",
            "render",
            "ClipComposition",
            str(output_path.absolute()),
            "--props",
            json.dumps(props, ensure_ascii=False),
        ],
        cwd=REMOTION_DIR,
        check=True,
        **_no_window_kwargs(),
    )
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Render clips with subtitles via Remotion")
    parser.add_argument("video", help="Path to original video file (.mp4)")
    parser.add_argument("clips", help="Path to clips JSON (from suggest_clips.py)")
    parser.add_argument("transcription", help="Path to transcription _full.json")
    parser.add_argument(
        "--index",
        type=int,
        help="Render only the clip at this index (0-based)",
    )
    args = parser.parse_args()

    video_path = Path(args.video)
    clips_path = Path(args.clips)
    transcription_path = Path(args.transcription)

    ensure_remotion_installed()

    with open(clips_path, encoding="utf-8") as f:
        clips = json.load(f)

    with open(transcription_path, encoding="utf-8") as f:
        transcription = json.load(f)
    segments = transcription.get("segments", [])

    if args.index is not None:
        target_clips = [(args.index, clips[args.index])]
    else:
        target_clips = list(enumerate(clips))

    rendered = []
    for idx, clip in target_clips:
        out = render_clip(clip, video_path, segments, idx)
        rendered.append(out)

    print(f"\n{len(rendered)} clip(s) rendered to {OUT_DIR}/")


if __name__ == "__main__":
    main()
