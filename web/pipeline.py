"""Pipeline orchestration for the Kirinuki web system."""

import json
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

PROJECT_DIR = Path(__file__).parent.parent
AUDIO_DIR = PROJECT_DIR / "audio-chunking"
REMOTION_DIR = PROJECT_DIR / "remotion"

sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(AUDIO_DIR))

from dotenv import load_dotenv

load_dotenv(PROJECT_DIR / ".env")

from audio_chunking_code import transcribe_audio_in_chunks  # noqa: E402


# ── Thread-local stdout capture ───────────────────────────────────────────────

_tl = threading.local()
_original_stdout = sys.__stdout__


class _LogInterceptor:
    def write(self, text: str) -> None:
        handler = getattr(_tl, "handler", None)
        if handler and text.strip():
            handler(text.rstrip())
        _original_stdout.write(text)

    def flush(self) -> None:
        _original_stdout.flush()


sys.stdout = _LogInterceptor()


@contextmanager
def with_logging(handler: Callable[[str], None]):
    _tl.handler = handler
    try:
        yield
    finally:
        _tl.handler = None


# ── Download ──────────────────────────────────────────────────────────────────

def download_video(
    url: str,
    output_dir: Path,
    log: Callable[[str], None],
) -> tuple[Path, Path | None]:
    output_dir.mkdir(exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")

    proc = subprocess.Popen(
        [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "--write-subs",
            "--sub-langs", "live_chat",
            "--newline",
            "--print", "after_move:filepath",
            "-o", template,
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    stdout_lines: list[str] = []
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
            stdout_lines.append(line)

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("yt-dlp failed — check the URL and network connection")

    mp4_lines = [l for l in stdout_lines if l.endswith(".mp4")]
    if not mp4_lines:
        raise RuntimeError("Could not find downloaded mp4 in yt-dlp output")

    video_path = Path(mp4_lines[-1])
    if not video_path.exists():
        raise RuntimeError(f"Downloaded file missing: {video_path}")

    chat_path = video_path.with_suffix("").with_suffix(".live_chat.json")
    return video_path, chat_path if chat_path.exists() else None


# ── Download chat only ────────────────────────────────────────────────────────

def download_chat_only(
    url: str,
    output_dir: Path,
    log: Callable[[str], None],
) -> Path | None:
    output_dir.mkdir(exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")

    proc = subprocess.Popen(
        [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--sub-langs", "live_chat",
            "--newline",
            "-o", template,
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("yt-dlp failed — URLとネット接続を確認してください")

    chat_files = sorted(output_dir.glob("*.live_chat.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return chat_files[0] if chat_files else None


# ── Transcribe ────────────────────────────────────────────────────────────────

def run_transcription(video_path: Path, language: str) -> dict:
    return transcribe_audio_in_chunks(video_path, language=language)


def save_transcription(result: dict, video_path: Path) -> Path:
    out_dir = PROJECT_DIR / "transcriptions"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{video_path.stem}_{ts}_full.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


def save_clips(clips: list[dict], video_path: Path) -> Path:
    out_dir = PROJECT_DIR / "transcriptions"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"clips_{video_path.stem}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)
    return path


# ── Suggest clips ─────────────────────────────────────────────────────────────

def suggest_clips_from_result(
    result: dict, chat_path: Path | None
) -> list[dict]:
    segments = result.get("segments", [])
    if segments:
        transcript_text = "\n".join(
            f"[{s.get('start', 0):.1f}s - {s.get('end', 0):.1f}s] {s.get('text', '').strip()}"
            for s in segments
        )
    else:
        transcript_text = result.get("text", "")

    chat_section = ""
    if chat_path and chat_path.exists():
        chat_lines: list[str] = []
        with open(chat_path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= 300:
                    break
                try:
                    msg = json.loads(line)
                    for action in msg.get("replayChatItemAction", {}).get("actions", []):
                        r = (
                            action.get("addChatItemAction", {})
                            .get("item", {})
                            .get("liveChatTextMessageRenderer", {})
                        )
                        if r:
                            author = r.get("authorName", {}).get("simpleText", "?")
                            text = "".join(
                                x.get("text", "") for x in r.get("message", {}).get("runs", [])
                            )
                            chat_lines.append(f"{author}: {text}")
                except Exception:
                    continue
        if chat_lines:
            chat_section = "\n## ライブチャット\n" + "\n".join(chat_lines[:200])

    prompt = f"""以下はYouTube動画の文字起こしです。

## 文字起こし
{transcript_text}
{chat_section}

この動画から切り抜き動画として面白い・バズりそうな部分を5〜10個提案してください。
以下のJSON配列のみを出力してください（前後に説明文不要）:

[
  {{
    "title": "クリップのタイトル",
    "start_sec": 120.5,
    "end_sec": 185.2,
    "reason": "なぜこの部分が切り抜きに適しているか"
  }}
]

条件: 各クリップ30秒〜5分、話の区切りが自然な部分、チャットが盛り上がっている部分を優先。
"""

    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "text"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Claude CLI failed:\n{proc.stderr.strip()}")

    text = proc.stdout.strip()
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"Could not parse clip suggestions from Claude response:\n{text[:500]}")

    return json.loads(m.group())


# ── Render ────────────────────────────────────────────────────────────────────

def make_captions(segments: list[dict], start_sec: float, end_sec: float) -> list[dict]:
    captions = []
    for seg in segments:
        s, e = seg.get("start", 0), seg.get("end", 0)
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


def render_clip(
    clip: dict,
    video_path: Path,
    segments: list[dict],
    index: int,
    log: Callable[[str], None],
    out_dir: Path,
    check_cancel: Callable[[], bool] | None = None,
    set_proc: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    start_sec = clip["start_sec"]
    end_sec = clip["end_sec"]
    vertical = bool(clip.get("vertical", False))
    crop_x = float(clip.get("cropX", 90))
    title = clip.get("title", f"clip_{index:02d}")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)[:40]
    if vertical:
        safe += "_v"

    video_abs = video_path.resolve()

    props = json.dumps(
        {
            "videoSrc": video_abs.name,
            "startSec": start_sec,
            "endSec": end_sec,
            "vertical": vertical,
            "cropX": crop_x,
            "title": clip.get("title", ""),
            "captions": make_captions(segments, start_sec, end_sec),
        },
        ensure_ascii=False,
    )

    output_path = out_dir / f"{index:02d}_{safe}.mp4"

    if not (REMOTION_DIR / "node_modules").exists():
        log("Installing Remotion dependencies...")
        subprocess.run(["npm", "install"], cwd=REMOTION_DIR, check=True)

    proc = subprocess.Popen(
        [
            "npx", "remotion", "render",
            "ClipComposition",
            str(output_path.absolute()),
            "--props", props,
            "--public-dir", str(video_abs.parent),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=REMOTION_DIR,
    )
    if set_proc:
        set_proc(proc)

    assert proc.stdout
    for line in proc.stdout:
        if check_cancel and check_cancel():
            proc.terminate()
            break
        line = line.rstrip()
        if line:
            log(line)
    proc.wait()
    if check_cancel and check_cancel():
        raise RuntimeError("レンダリングがキャンセルされました")
    if proc.returncode != 0:
        raise RuntimeError(f"Remotion render failed for clip {index}")

    return output_path
