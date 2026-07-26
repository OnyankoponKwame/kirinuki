"""Pipeline orchestration for the Kirinuki web system."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

import config as cfg
import theme_store

# Models selectable for clip suggestion (suggest_clips_from_result). Transcription does
# not use Gemini — see run_transcription()'s "groq" / "elevenlabs" branches.
GEMINI_SUGGEST_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]
GEMINI_MODEL_ID = GEMINI_SUGGEST_MODELS[0]  # default

PROJECT_DIR = Path(__file__).parent.parent
DATA_DIR = cfg.get_data_dir()
TRANSCRIPTIONS_DIR = DATA_DIR / "transcriptions"
AUDIO_DIR = PROJECT_DIR / "audio-chunking"
REMOTION_DIR = PROJECT_DIR / "remotion"

sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(AUDIO_DIR))

from dotenv import load_dotenv

load_dotenv(PROJECT_DIR / ".env")

from audio_chunking_code import transcribe_audio_in_chunks, slim_transcription_result  # noqa: E402


# ── Thread-local stdout capture ───────────────────────────────────────────────

_tl = threading.local()
_original_stdout = sys.__stdout__


class _LogInterceptor:
    def write(self, text: str) -> None:
        handler = getattr(_tl, "handler", None)
        if handler and text.strip():
            handler(text.rstrip())
        # sys.__stdout__ is None under pythonw.exe (no console) — there's nowhere
        # to forward the write, but the job-log handler above already got it.
        if _original_stdout:
            _original_stdout.write(text)

    def flush(self) -> None:
        if _original_stdout:
            _original_stdout.flush()


sys.stdout = _LogInterceptor()


@contextmanager
def with_logging(handler: Callable[[str], None]):
    _tl.handler = handler
    try:
        yield
    finally:
        _tl.handler = None


# ── Live chat ─────────────────────────────────────────────────────────────────
# yt-dlp has no download option to slim this down: --convert-subs explicitly refuses to
# convert "json" subtitles (which is what live_chat always is) into any other format. So
# each raw *.live_chat.json line is a full YouTube replay-action tree (badge/author-photo
# URLs, click-tracking params, emoji metadata, ...) — 1.5KB+ per chat message when only the
# timestamp and text are ever used. We rewrite the file in place right after download to
# `{"t": <video-offset-seconds>, "text": "..."}` per line, cutting size by ~95%+.

def _extract_chat_text(message: dict) -> str:
    parts = []
    for run in message.get("runs", []):
        if "text" in run:
            parts.append(run["text"])
        else:
            emoji = run.get("emoji", {})
            # For custom emoji/stamps, accessibilityData.label ("草ああ") is the clean
            # human-readable name — shortcuts ([":_草ああ:", ":草ああ:"]) carry stray
            # colons/underscores from the shortcode syntax, so only fall back to those
            # if a label is somehow missing.
            label = (
                emoji.get("image", {})
                .get("accessibility", {})
                .get("accessibilityData", {})
                .get("label")
            )
            if label:
                parts.append(label)
            else:
                shortcuts = emoji.get("shortcuts") or []
                if shortcuts:
                    parts.append(shortcuts[0])
    return "".join(parts).strip()


def _parse_raw_chat_line(obj: dict) -> list[tuple[float, str]]:
    """Extract (video_offset_sec, text) pairs from one line of yt-dlp's raw live_chat.json."""
    rca = obj.get("replayChatItemAction", {})
    try:
        offset = int(rca.get("videoOffsetTimeMsec", 0) or 0) / 1000
    except (TypeError, ValueError):
        offset = 0.0
    out = []
    for action in rca.get("actions", []):
        r = (
            action.get("addChatItemAction", {})
            .get("item", {})
            .get("liveChatTextMessageRenderer", {})
        )
        if r:
            text = _extract_chat_text(r.get("message", {}))
            if text:
                out.append((offset, text))
    return out


def _read_chat_entries(chat_path: Path, limit_lines: int | None = None) -> list[tuple[float, str]]:
    """Read chat messages as (video_offset_sec, text) pairs.

    Understands three on-disk shapes so callers don't need to care which stage of the
    pipeline produced the file: a pretty-printed `[{"t":.., "text":..}, ...]` array (what
    save_chat() writes into transcriptions/), one `{"t":.., "text":..}` object per line
    (what slim_live_chat() writes back into downloads/), or yt-dlp's original raw JSONL.
    """
    with open(chat_path, encoding="utf-8") as f:
        content = f.read()

    if content.lstrip().startswith("["):
        try:
            data = json.loads(content)
        except Exception:
            return []
        entries = []
        for obj in data if isinstance(data, list) else []:
            text = str(obj.get("text", "")).strip() if isinstance(obj, dict) else ""
            if text:
                entries.append((obj.get("t", 0.0), text))
        return entries[:limit_lines] if limit_lines is not None else entries

    entries = []
    for i, line in enumerate(content.splitlines()):
        if limit_lines is not None and i >= limit_lines:
            break
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if "text" in obj:
            text = str(obj.get("text", "")).strip()
            if text:
                entries.append((obj.get("t", 0.0), text))
        else:
            entries.extend(_parse_raw_chat_line(obj))
    return entries


def _bucket_chat_counts(entries: list[tuple[float, str]], window_sec: float) -> tuple[list[int], float, float]:
    """Bucket chat entries into window_sec-wide time buckets and derive the spike
    threshold. Shared by analyze_chat_spikes() (the Gemini prompt's text report) and
    chat_activity_buckets() (the ③切り抜き提案 activity chart) so both agree on what
    counts as a "盛り上がり" spike."""
    max_time = max(t for t, _ in entries)
    num_buckets = int(max_time // window_sec) + 1
    buckets = [0] * num_buckets
    for t, _text in entries:
        idx = min(int(t // window_sec), num_buckets - 1)
        buckets[idx] += 1

    # 平均コメント数の算出（動画全体の時間に対するバケット平均）
    avg_count = sum(buckets) / len(buckets)
    # 閾値の設定（全体の平均の1.5倍、かつ最低でも3コメント以上）
    threshold = max(avg_count * 1.5, 3.0)
    return buckets, avg_count, threshold


def _detect_chat_reactions(messages: list[str]) -> list[str]:
    """Rough keyword-based reaction tagging for a spike range's messages."""
    text_concat = " ".join(messages).lower()
    reactions = []
    # 笑い (w, 草, 笑)
    if any(w in text_concat for w in ["w", "草", "笑"]):
        reactions.append("笑い(草/w)")
    # 拍手・賞賛 (👏, 8888, ナイス, GJ, うまい, すごい, 神...)
    if any(w in text_concat for w in [
        "👏", "888", "おめ", "さす", "流石", "ナイス", "nice", "gj",
        "グッジョブ", "うまい", "うまっ", "すご", "すげ", "神", "いいね",
    ]):
        reactions.append("拍手/賞賛(👏/ナイス)")
    # 驚き (!?、え、まじ)
    if any(w in text_concat for w in ["!?", "！？", "え", "は？", "まじ", "マジ", "うそ", "嘘"]):
        reactions.append("驚き(!?/えっ)")
    # 悲鳴・絶叫・危機 (ぎゃー、きゃー、やば、こわ)
    if any(w in text_concat for w in ["ぎゃ", "きゃ", "やば", "ヤバ", "こわ", "怖", "たすけ", "助け", "無理", "むり"]):
        reactions.append("悲鳴/パニック(やばい/悲鳴)")
    return reactions


def _detect_chat_spikes(entries: list[tuple[float, str]], window_sec: float = 30.0) -> list[dict]:
    """Detect chat-density spike ranges with their messages/reactions/ratio.

    Shared by analyze_chat_spikes() (the text report sent to Gemini by
    suggest_clips_from_result()) and chat_activity() (the ③切り抜き提案 activity
    chart's spike detail list) so the two always describe the exact same ranges —
    the chart is a view onto the same analysis already used for clip suggestion,
    not a separate computation.
    """
    if not entries:
        return []
    max_time = max(t for t, _ in entries)
    if max_time <= 0:
        return []

    buckets, avg_count, threshold = _bucket_chat_counts(entries, window_sec)

    bucket_ranges: list[tuple[int, int]] = []
    in_spike = False
    spike_start = spike_end = 0
    for idx, count in enumerate(buckets):
        if count >= threshold:
            if not in_spike:
                in_spike = True
                spike_start = idx
            spike_end = idx
        else:
            if in_spike:
                bucket_ranges.append((spike_start, spike_end))
                in_spike = False
    if in_spike:
        bucket_ranges.append((spike_start, spike_end))

    result = []
    for start_idx, end_idx in bucket_ranges[:15]:  # 最大15個に制限
        start, end = start_idx * window_sec, (end_idx + 1) * window_sec
        messages = [text for t, text in entries if start <= t < end]
        count = len(messages)
        ratio = count / (avg_count * ((end - start) / window_sec)) if avg_count > 0 else 0
        result.append({
            "start": start,
            "end": end,
            "count": count,
            "ratio": ratio,
            "reactions": _detect_chat_reactions(messages),
            "messages": messages,
        })
    return result


def chat_activity(chat_path: Path, window_sec: float = 30.0) -> dict:
    """Full chat-activity payload for the ③切り抜き提案 activity chart
    (web/static/index.html): per-window counts for the bar chart, plus the
    detected spike ranges (with their actual messages) for the expandable spike
    detail list. Both are derived from _detect_chat_spikes()/_bucket_chat_counts(),
    the same analysis analyze_chat_spikes() already turns into the text report
    suggest_clips_from_result() sends to Gemini — this just makes it visible.
    """
    entries = _read_chat_entries(chat_path)
    if not entries or max(t for t, _ in entries) <= 0:
        return {"buckets": [], "spikes": []}
    buckets, _avg_count, threshold = _bucket_chat_counts(entries, window_sec)
    bucket_list = [
        {"t": idx * window_sec, "count": count, "spike": count >= threshold}
        for idx, count in enumerate(buckets)
    ]
    return {"buckets": bucket_list, "spikes": _detect_chat_spikes(entries, window_sec)}


def analyze_chat_spikes(entries: list[tuple[float, str]], window_sec: float = 30.0) -> str:
    """Analyze chat density spikes and generate a report of hyped time ranges."""
    if not entries:
        return ""
    max_time = max(t for t, _ in entries)
    if max_time <= 0:
        return ""

    spikes = _detect_chat_spikes(entries, window_sec)
    if not spikes:
        return "（顕著なチャットの盛り上がり区間は検出されませんでした）"

    report_lines = ["### 自動分析されたチャット盛り上がり時間帯（コメント急増区間）:"]
    for sp in spikes:
        reaction_str = "、".join(sp["reactions"]) if sp["reactions"] else "一般的な会話"
        report_lines.append(
            f"- [{sp['start']:.1f}s - {sp['end']:.1f}s] (コメント密度: 通常の {sp['ratio']:.1f}倍) "
            f"- 主なリアクション: {reaction_str}"
        )

    return "\n".join(report_lines)


def slim_live_chat(chat_path: Path, log: Callable[[str], None] | None = None) -> None:
    """Rewrite a yt-dlp live_chat.json in place, keeping only {t, text} per message."""
    try:
        with open(chat_path, encoding="utf-8") as f:
            first_line = next((line for line in f if line.strip()), "")
        if not first_line or "replayChatItemAction" not in first_line:
            return  # already slimmed, or not the format we expect — leave untouched
        entries = _read_chat_entries(chat_path)
    except OSError:
        return

    tmp_path = chat_path.with_suffix(chat_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for offset, text in entries:
            f.write(json.dumps({"t": round(offset, 1), "text": text}, ensure_ascii=False) + "\n")
    tmp_path.replace(chat_path)
    if log:
        log(f"✓ Chat trimmed: {len(entries)} messages")


# Chat reacts to what's said on stream, not simultaneously with it — viewers need to
# hear/read the moment before typing — so a chat message's video-offset timestamp lands
# noticeably after the moment it's actually reacting to. Shifting back by this much when
# pairing chat with the transcription lines a reaction back up with its cause.
CHAT_REACTION_LAG_SEC = 5.0


def save_chat(chat_path: Path, transcription_path: Path) -> Path:
    """Reformat a live_chat.json into transcriptions/, paired with its transcription file.

    Mirrors save_transcription()/save_clips(): the pipeline should never read live chat
    straight out of downloads/ for clip suggestion — it reads this dedicated-folder copy,
    saved once up front, same as the transcription. Timestamps are shifted back by
    CHAT_REACTION_LAG_SEC to compensate for that reaction delay.
    """
    entries = _read_chat_entries(chat_path)
    out_dir = TRANSCRIPTIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = transcription_path.stem  # e.g. "title_20260510_123456_full"
    if base.endswith("_full"):
        base = base[:-5]
    path = out_dir / f"chat_{base}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {"t": round(max(0.0, t - CHAT_REACTION_LAG_SEC), 1), "text": text}
                for t, text in entries
            ],
            f, indent=2, ensure_ascii=False,
        )
    return path


# ── Download ──────────────────────────────────────────────────────────────────

# yt-dlp の進捗行 ([download] ...) と、--print で出力させる最終ファイルパスを
# 混同しないための目印。進捗行にも ".mp4" で終わるものがあるため必要。
FILEPATH_MARKER = "@@KIRINUKI_FILEPATH@@"
VIDEO_ID_MARKER = "@@KIRINUKI_VIDEO_ID@@"

# ダウンロード画質。キーは API / UI から来る文字列で、値は yt-dlp の -f 式の高さ上限。
# 未知の値が -f にそのまま渡らないよう、必ずこの表を経由して解決する。
VIDEO_QUALITIES = {"720": 720, "1080": 1080}
DEFAULT_VIDEO_QUALITY = "1080"


def _format_selector(quality: str) -> str:
    height = VIDEO_QUALITIES.get(quality, VIDEO_QUALITIES[DEFAULT_VIDEO_QUALITY])
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"


def download_video(
    url: str,
    output_dir: Path,
    log: Callable[[str], None],
    quality: str = DEFAULT_VIDEO_QUALITY,
) -> tuple[Path, Path | None]:
    output_dir.mkdir(exist_ok=True)
    template = str(output_dir / "%(title).80s_%(id)s.%(ext)s")

    # Windowsのパッケージ版ではNode.jsが同梱されているため、yt-dlpにそのパスを明示的に伝える
    js_runtime_args = []
    packaged_node = PROJECT_DIR / "node" / "node.exe"
    if packaged_node.exists():
        js_runtime_args = ["--js-runtimes", f"node:{packaged_node}"]
    else:
        js_runtime_args = ["--js-runtimes", "node"]

    cookies_dir = cfg.get_data_dir()
    cookies_path = cfg.find_cookies_file(cookies_dir) or (cookies_dir / "cookies.txt")
    # cookies.txt相当のファイル（"127.0.0.1_cookies.txt" 等、末尾一致で検出）が存在すれば、
    # ロックを避けるためにそれを優先使用する。
    # 存在しなければ、最初はクッキーなしでダウンロードを試みる。
    if cookies_path.exists():
        cookie_args = ["--cookies", str(cookies_path)]
    else:
        cookie_args = []

    ffmpeg_args = []
    bin_dir = PROJECT_DIR / "bin"
    if (bin_dir / "ffmpeg.exe").exists() or (bin_dir / "ffmpeg").exists():
        ffmpeg_args = ["--ffmpeg-location", str(bin_dir)]

    cmd = [
        "yt-dlp",
    ] + js_runtime_args + cookie_args + ffmpeg_args + [
        "-f", _format_selector(quality),
        "--merge-output-format", "mp4",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--newline",
        # --print は暗黙的に --quiet を有効にする (yt_dlp/__init__.py:
        # `opts.quiet = ... or bool(opts.forceprint)`)。その結果 noprogress も True になり、
        # [download] 行が一切出力されず UI の進捗バーが動かなくなる。
        # --no-quiet / --progress で明示的に打ち消す。
        "--no-quiet",
        "--progress",
        # 既定 (0秒) だと毎秒数十行が SSE ログに流れ込むので 1秒間隔に間引く
        "--progress-delta", "1",
        "--print", f"id:{VIDEO_ID_MARKER}%(id)s",
        "--print", f"after_move:{FILEPATH_MARKER}%(filepath)s",
        "-o", template,
        url,
    ]

    cookie_error_detected = False
    stdout_lines: list[str] = []
    filepaths: list[str] = []
    video_id: str | None = None

    def consume(proc: subprocess.Popen) -> None:
        """yt-dlp の出力を1行ずつログに流しつつ、filepath とクッキーエラーを拾う。"""
        nonlocal cookie_error_detected, video_id
        assert proc.stdout
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith(VIDEO_ID_MARKER):
                video_id = line[len(VIDEO_ID_MARKER):].strip()
                continue
            if line.startswith(FILEPATH_MARKER):
                path = line[len(FILEPATH_MARKER):]
                filepaths.append(path)
                log(path)
                continue
            log(line)
            stdout_lines.append(line)
            # クッキーのロックエラーや未検出を検知
            if "cookie" in line.lower() and ("could not copy" in line.lower() or "error" in line.lower() or "failed" in line.lower()):
                cookie_error_detected = True
        proc.wait()

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **cfg.no_window_kwargs(),
    )

    consume(proc)

    # 失敗した時のハンドリング
    if proc.returncode != 0:
        bot_error_detected = any(
            any(kw in line.lower() for kw in ["confirm you are not a bot", "sign in", "cookie", "bot", "captcha", "forbidden"])
            for line in stdout_lines
        )

        retry_cmd = None
        # 1. もしキャッシュされたcookies.txtを使用して失敗した場合は、最新のクッキーをブラウザから再取得してキャッシュ更新を試みます
        if cookies_path.exists() and "--cookies" in cmd and "--cookies-from-browser" not in cmd:
            log("保存されたクッキーでのダウンロードに失敗しました。最新のクッキーをブラウザから再取得して更新を試みます...")
            retry_cmd = []
            for arg in cmd:
                if arg == str(cookies_path):
                    retry_cmd.append(arg)
                    continue
                if arg == "--cookies":
                    retry_cmd.extend(["--cookies-from-browser", "chrome", "--cookies"])
                    continue
                retry_cmd.append(arg)
        # 2. もしクッキーなしで失敗し、かつボットエラーが検出された場合は、Chromeからのクッキー取得を試みてリトライします
        elif not cookies_path.exists() and "--cookies" not in cmd and bot_error_detected:
            log("クッキーなしでのダウンロードに失敗しました。YouTubeの制限回避のため、Chromeからクッキーを取得してリトライします...")
            retry_cmd = cmd.copy()
            # 動画URL(cmd[-1])の手前にクッキー引数を挿入する
            insert_idx = len(retry_cmd) - 1
            retry_cmd[insert_idx:insert_idx] = ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)]

        if retry_cmd:
            proc = subprocess.Popen(
                retry_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **cfg.no_window_kwargs(),
            )
            stdout_lines.clear()
            filepaths.clear()
            consume(proc)

        # クッキーのロックエラー（ブラウザ起動中）で失敗した場合
        if proc.returncode != 0 and cookie_error_detected:
            if cookies_path.exists():
                try:
                    cookies_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(
                "ダウンロードに失敗しました。YouTubeのボット制限を回避するためにクッキーが必要ですが、Chromeが起動中のためアクセスできません。"
                "一度Chromeブラウザを完全に閉じた状態で、再度実行してください（次回以降はChromeを開いたままでも動作します）。"
            )

    if proc.returncode != 0:
        bot_auth_error = any(
            any(kw in line.lower() for kw in [
                "confirm you are not a bot", "confirm you're not a bot",
                "sign in", "cookie", "bot", "captcha", "forbidden",
                "private video", "members-only", "login", "429"
            ])
            for line in stdout_lines
        )
        if bot_auth_error:
            raise RuntimeError(
                "YouTubeのアクセス・ボット制限（Sign in to confirm you're not a bot 等）によりダウンロードに失敗しました。"
                "Cookie（cookies.txt）を設定することで回避できます。「Cookie手順」ボタンをクリックして手動保存手順をご確認ください。"
            )
        raise RuntimeError("yt-dlp failed — check the URL and network connection")

    video_path: Path | None = None
    if filepaths:
        video_path = Path(filepaths[-1])
    else:
        mp4_lines = [l for l in stdout_lines if l.endswith(".mp4") and not l.startswith("[")]
        if mp4_lines:
            video_path = Path(mp4_lines[-1])

    # Windows環境等で絵文字やタイトルのサニタイズ・文字数制限の差異により
    # yt-dlpが報告したパスと実際に生成されたパスが異なる場合のフォールバック処理
    if video_path is None or not video_path.exists():
        candidate: Path | None = None
        # 1. video_id が特定できていれば output_dir から *<video_id>*.mp4 を探す
        if video_id:
            candidates = list(output_dir.glob(f"*{video_id}*.mp4"))
            if candidates:
                candidate = candidates[0]

        # 2. video_id が不明または見つからない場合、出力ディレクトリ内の最新の .mp4 ファイルを採用
        if not candidate:
            mp4s = list(output_dir.glob("*.mp4"))
            if mp4s:
                candidate = max(mp4s, key=lambda p: p.stat().st_mtime)

        if candidate and candidate.exists():
            log(f"通知: yt-dlpが報告したファイルパス ({video_path}) が見つからなかったため、検出された実際のファイル ({candidate.name}) を採用しました。")
            video_path = candidate
        else:
            try:
                files = list(output_dir.glob("*"))
                log(f"Debug: Output directory contains {len(files)} files:")
                for f in files:
                    log(f"  - {f.name}")
            except Exception as e:
                log(f"Debug: Failed to list files: {e}")
            raise RuntimeError(f"Downloaded file missing: {video_path}")

    # チャットログファイルの検出とフォールバック
    chat_path = video_path.with_suffix("").with_suffix(".live_chat.json")
    if not chat_path.exists():
        matched_chats: list[Path] = []
        if video_id:
            matched_chats = list(output_dir.glob(f"*{video_id}*.live_chat.json"))
        if not matched_chats:
            matched_chats = sorted(output_dir.glob("*.live_chat.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if matched_chats:
            chat_path = matched_chats[0]
        else:
            chat_path = None

    if chat_path and chat_path.exists():
        slim_live_chat(chat_path, log)
        return video_path, chat_path
    return video_path, None


# ── Download chat only ────────────────────────────────────────────────────────

def download_chat_only(
    url: str,
    output_dir: Path,
    log: Callable[[str], None],
) -> Path | None:
    output_dir.mkdir(exist_ok=True)
    template = str(output_dir / "%(title).80s_%(id)s.%(ext)s")

    # Windowsのパッケージ版ではNode.jsが同梱されているため、yt-dlpにそのパスを明示的に伝える
    js_runtime_args = []
    packaged_node = PROJECT_DIR / "node" / "node.exe"
    if packaged_node.exists():
        js_runtime_args = ["--js-runtimes", f"node:{packaged_node}"]
    else:
        js_runtime_args = ["--js-runtimes", "node"]

    cookies_dir = cfg.get_data_dir()
    cookies_path = cfg.find_cookies_file(cookies_dir) or (cookies_dir / "cookies.txt")
    # cookies.txt相当のファイル（"127.0.0.1_cookies.txt" 等、末尾一致で検出）が存在すれば、
    # ロックを避けるためにそれを優先使用する。
    # 存在しなければ、最初はクッキーなしでダウンロードを試みる。
    if cookies_path.exists():
        cookie_args = ["--cookies", str(cookies_path)]
    else:
        cookie_args = []

    ffmpeg_args = []
    bin_dir = PROJECT_DIR / "bin"
    if (bin_dir / "ffmpeg.exe").exists() or (bin_dir / "ffmpeg").exists():
        ffmpeg_args = ["--ffmpeg-location", str(bin_dir)]

    cmd = [
        "yt-dlp",
    ] + js_runtime_args + cookie_args + ffmpeg_args + [
        "--skip-download",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--newline",
        "-o", template,
        url,
    ]

    cookie_error_detected = False
    stdout_lines: list[str] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **cfg.no_window_kwargs(),
    )

    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log(line)
            stdout_lines.append(line)
            # クッキーのロックエラーや未検出を検知
            if "cookie" in line.lower() and ("could not copy" in line.lower() or "error" in line.lower() or "failed" in line.lower()):
                cookie_error_detected = True

    proc.wait()

    # 失敗した時のハンドリング
    if proc.returncode != 0:
        bot_error_detected = any(
            any(kw in line.lower() for kw in ["confirm you are not a bot", "sign in", "cookie", "bot", "captcha", "forbidden"])
            for line in stdout_lines
        )

        retry_cmd = None
        # 1. もしキャッシュされたcookies.txtを使用して失敗した場合は、最新のクッキーをブラウザから再取得してキャッシュ更新を試みます
        if cookies_path.exists() and "--cookies" in cmd and "--cookies-from-browser" not in cmd:
            log("保存されたクッキーでのチャット取得に失敗しました。最新のクッキーをブラウザから再取得して更新を試みます...")
            retry_cmd = []
            for arg in cmd:
                if arg == str(cookies_path):
                    retry_cmd.append(arg)
                    continue
                if arg == "--cookies":
                    retry_cmd.extend(["--cookies-from-browser", "chrome", "--cookies"])
                    continue
                retry_cmd.append(arg)
        # 2. もしクッキーなしで失敗し、かつボットエラーが検出された場合は、Chromeからのクッキー取得を試みてリトライします
        elif not cookies_path.exists() and "--cookies" not in cmd and bot_error_detected:
            log("クッキーなしでのチャット取得に失敗しました。YouTubeの制限回避のため、Chromeからクッキーを取得してリトライします...")
            retry_cmd = cmd.copy()
            # 動画URL(cmd[-1])の手前にクッキー引数を挿入する
            insert_idx = len(retry_cmd) - 1
            retry_cmd[insert_idx:insert_idx] = ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)]

        if retry_cmd:
            proc = subprocess.Popen(
                retry_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **cfg.no_window_kwargs(),
            )
            assert proc.stdout
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log(line)
                    if "cookie" in line.lower() and ("could not copy" in line.lower() or "error" in line.lower() or "failed" in line.lower()):
                        cookie_error_detected = True
            proc.wait()

        # クッキーのロックエラー（ブラウザ起動中）で失敗した場合
        if proc.returncode != 0 and cookie_error_detected:
            if cookies_path.exists():
                try:
                    cookies_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(
                "チャットデータの取得に失敗しました。YouTubeの制限を回避するためにクッキーが必要ですが、Chromeが起動中のためアクセスできません。"
                "一度Chromeブラウザを完全に閉じた状態で、再度実行してください（次回以降はChromeを開いたままでも動作します）。"
            )

    if proc.returncode != 0:
        raise RuntimeError("yt-dlp failed — URLとネット接続を確認してください")

    chat_files = sorted(output_dir.glob("*.live_chat.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not chat_files:
        return None
    chat_path = chat_files[0]
    slim_live_chat(chat_path, log)
    return chat_path


# ── Transcribe ────────────────────────────────────────────────────────────────

def trim_video(video_path: Path, start_sec: float, end_sec: float | None) -> Path:
    """Trim video to [start_sec, end_sec) using stream copy (fast, no re-encode). Returns a temp file."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=video_path.suffix or ".mp4", delete=False) as f:
        out_path = Path(f.name)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(start_sec),
    ]
    if end_sec is not None:
        cmd += ["-to", str(end_sec)]
    cmd += ["-i", str(video_path), "-c", "copy", "-y", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True, **cfg.no_window_kwargs())
    return out_path


def offset_timestamps(result: dict, offset_sec: float) -> None:
    """Shift all segment/word timestamps in-place by offset_sec."""
    for seg in result.get("segments", []):
        seg["start"] = seg.get("start", 0) + offset_sec
        seg["end"] = seg.get("end", 0) + offset_sec
    for word in result.get("words", []):
        word["start"] = word.get("start", 0) + offset_sec
        word["end"] = word.get("end", 0) + offset_sec


def run_transcription(
    video_path: Path,
    language: str,
    initial_prompt: str | None = None,
    audio_mode: str = "mp3",
    transcription_model: str = "elevenlabs",
) -> dict:
    if transcription_model == "groq":
        raw = transcribe_audio_in_chunks(video_path, language=language, initial_prompt=initial_prompt, audio_mode=audio_mode)
    else:
        from elevenlabs_transcribe import transcribe_with_elevenlabs
        keyterms_str = os.environ.get("ELEVENLABS_KEYTERMS")
        if keyterms_str is None:
            keyterms_str = "徳川ゆめの, ゆめのん, 飴白, 飴白なび"
        keyterms_str = keyterms_str.replace("，", ",").replace("、", ",")
        keyterms = [k.strip() for k in keyterms_str.split(",") if k.strip()] if keyterms_str else None
        raw = transcribe_with_elevenlabs(
            video_path, language=language, initial_prompt=initial_prompt, audio_mode=audio_mode, keyterms=keyterms
        )
    return slim_transcription_result(raw)


def save_transcription(result: dict, video_path: Path) -> Path:
    out_dir = TRANSCRIPTIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{video_path.stem}_{ts}_full.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


def save_clips(clips: list[dict], transcription_path: Path) -> Path:
    out_dir = TRANSCRIPTIONS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    base = transcription_path.stem  # e.g. "title_20260510_123456_full"
    if base.endswith("_full"):
        base = base[:-5]
    path = out_dir / f"clips_{base}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)
    return path


# ── Caption effect rules ──────────────────────────────────────────────────────

CAPTION_EFFECTS = {
    "anger", "scary", "panic", "laugh", "hype", "pop", "punch", "pill", "neon",
    "glitch", "gaming", "cute", "news", "whisper", "question", "shock",
}

_EFFECT_PRIORITY = [
    "anger", "scary", "panic", "shock", "laugh", "gaming", "hype", "cute",
    "question", "whisper", "news", "glitch", "punch", "pill",
    "neon", "pop",
]

# 優先度順に定義（前のタイプが後のタイプより優先）
_EFFECT_KEYWORDS: dict[str, set[str]] = {
    # 怒り・強い抗議
    "anger": {
        "ふざけるな", "ふざけんな", "ふざけないで", "ふざけてる",
        "怒", "怒る", "怒った", "怒って", "怒り", "激怒",
        "キレる", "キレた", "キレて", "ブチギレ", "ぶちぎれ",
        "許さない", "許せない", "許さん", "許せん",
        "最悪", "最低", "ひどい", "酷い", "ひどすぎ",
        "ムカつく", "むかつく", "ムカついた", "腹立つ", "腹が立つ",
        "なめるな", "舐めるな", "ありえない", "ありえん",
    },
    # 怖い・不穏・ホラー
    "scary": {
        "怖い", "こわい", "怖っ", "こわっ", "怖すぎ", "こわすぎ",
        "ホラー", "不気味", "不穏", "ゾッ", "ぞっ", "鳥肌",
        "幽霊", "おばけ", "化け物", "怪物", "呪い", "呪われ",
        "後ろ", "背後", "見てる", "見られてる", "気配",
        "びっくりした", "ビビった", "びびった",
    },
    # パニック・恐怖・悲鳴
    "panic": {
        "やめろ", "やめて", "やめてください", "やめないで",
        "やばい", "やばっ", "やばー",
        "うわ", "うわー", "うわっ", "うわあ",
        "きゃ", "きゃー", "きゃっ",
        "ひぃ", "ひいい", "ひー",
        "ぎゃ", "ぎゃー", "ぎゃあ",
        "たすけて", "助けて",
        "いやだ", "いやー", "むりむり", "むりー",
        "あああ", "ああああ",
        "まずい", "まずっ",
    },
    # 衝撃・大オチ
    "shock": {
        "！？", "?!", "えええ", "えぇぇ", "うそでしょ", "嘘でしょ",
        "なんで", "どうして", "終わった", "詰んだ", "壊れた",
        "まじか", "まじかー", "マジか",
        "うそだろ", "うそやん", "うそ！",
        "信じられない", "信じられん",
    },
    # 笑い・ウケ
    "laugh": {
        "笑", "ｗｗ", "ｗ", "草", "草生え",
        "ウケる", "うける", "ウケた", "うけた",
        "爆笑", "吹いた", "ふいた",
        "面白", "おもろ", "おもしろ",
        "ジワる", "じわる",
    },
    # ゲーム実況・勝負どころ
    "gaming": {
        "勝った", "負けた", "ラスボス", "ボス", "クリア",
        "レベル", "スキル", "コンボ", "キル", "ヘッドショット",
        "バトル", "戦闘", "耐えた", "ワンチャン",
    },
    # テンション・盛り上がり
    "hype": {
        "すごい", "すごっ", "すごー", "すげー", "すげえ",
        "やった", "やったー",
        "最高", "天才", "神", "つよい", "つよっ",
        "強すぎ", "えぐい", "えぐっ", "えぐー",
        "優勝", "完璧", "完全勝利",
        "うまい", "うまっ", "うますぎ",
    },
    # かわいい・やわらかいリアクション
    "cute": {
        "かわいい", "可愛い", "かわい", "きゃわ", "尊い",
        "癒やし", "癒し", "すき", "好き", "にゃ",
    },
    # 疑問・ツッコミ
    "question": {
        "なに", "何", "なんで", "どういうこと", "どういう",
        "どこ", "どれ", "誰", "だれ", "なぜ", "ほんと？",
        "え？", "えっ", "えぇ",
    },
    # 小声・内緒話
    "whisper": {
        "小声", "内緒", "ないしょ", "こっそり", "ひそひそ",
        "しー", "静かに", "秘密",
    },
    # ニュース・告知
    "news": {
        "速報", "発表", "お知らせ", "告知", "重大発表",
        "ニュース", "決定", "解禁",
    },
    # デジタル崩れ・バグ
    "glitch": {
        "バグ", "ラグ", "エラー", "壊れ", "固まっ", "フリーズ",
        "カクカク", "ずれた",
    },
    # パンチライン・強い断言
    "punch": {
        "結論", "一言で", "正直", "要するに", "だから",
        "これだけ", "これが", "絶対に",
        "本当に", "ほんとに", "ほんと",
        "マジで", "まじで", "マジ", "まじ",
        "ガチで", "ガチ", "がちで", "がち",
        "絶対", "ぜったい",
        "つまり", "ここ", "ポイント", "大事", "重要",
        "覚えて", "見て", "注目",
    },
    # ピル背景で見せたい短い強調
    "pill": {
        "無料", "限定", "新作", "おすすめ", "推し",
        "最強", "便利", "保存版",
    },
    # ネオン・派手な見せ場
    "neon": {
        "キラキラ", "光", "輝", "映え", "エモい", "エモ",
    },
    # 汎用ポップ
    "pop": {
        "はい", "はい！", "じゃん", "どん", "ぽん", "きた", "来た",
    },
}


def detect_effect_for_segment(seg: dict) -> str:
    """Return effect type string for a single segment, or '' if none."""
    text = seg.get("text", "")
    excl = text.count("！") + text.count("!")
    question = text.count("？") + text.count("?")
    for etype in _EFFECT_PRIORITY:
        if any(w in text for w in _EFFECT_KEYWORDS[etype]):
            return etype
    if (excl >= 2 and question >= 1) or excl >= 4:
        return "shock"
    if question >= 2:
        return "question"
    if excl >= 3:
        return "punch"
    return ""


def _segments_for_clip(segments: list[dict], start_sec: float, end_sec: float) -> list[dict]:
    return [
        s for s in segments
        if s.get("end", 0) > start_sec and s.get("start", 0) < end_sec
    ]


def infer_caption_effect_for_clip(clip: dict, segments: list[dict]) -> str:
    """Pick one default effect for a suggested clip. Segment-level effects can override it later."""
    raw = clip.get("captionEffect")
    if raw == "emphasis":
        raw = ""
    if raw in CAPTION_EFFECTS:
        return raw

    c_start = float(clip.get("start_sec", 0))
    c_end = float(clip.get("end_sec", c_start))
    clip_segments = _segments_for_clip(segments, c_start, c_end)
    text = " ".join(
        [
            str(clip.get("title", "")),
            str(clip.get("reason", "")),
            " ".join(s.get("text", "") for s in clip_segments),
        ]
    )

    # まずクリップ全体の文脈から判定。複数ヒット時は優先度で決める。
    for etype in _EFFECT_PRIORITY:
        if any(w in text for w in _EFFECT_KEYWORDS[etype]):
            return etype

    excl = text.count("！") + text.count("!")
    question = text.count("？") + text.count("?")
    if excl >= 4 or (excl and question):
        return "shock"
    if question >= 2:
        return "question"
    if excl >= 3:
        return "punch"

    reason = str(clip.get("reason", ""))
    if any(w in reason for w in ("チャット", "盛り上が", "見どころ", "バズ", "リアクション")):
        return "hype"
    return ""


def enrich_clip_caption_effects(clips: list[dict], segments: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for clip in clips:
        effect = infer_caption_effect_for_clip(clip, segments)
        if effect:
            enriched.append({**clip, "captionEffect": effect})
        else:
            enriched.append(clip)
    return enriched


# ── Suggest clips ─────────────────────────────────────────────────────────────

def suggest_clips_from_result(
    result: dict,
    chat_path: Path | None,
    extra_prompt: str | None = None,
    gemini_model: str | None = None,
) -> tuple[list[dict], str]:
    """Returns (clips, chat_report) — chat_report is analyze_chat_spikes()'s text
    report (empty string if there's no chat), the same text folded into the Gemini
    prompt below, surfaced so the caller can show the user what was actually
    analyzed (see web/app.py's job["chat_analysis"])."""
    segments = result.get("segments", [])
    if segments:
        transcript_text = "\n".join(
            f"[{s.get('start', 0):.1f}s - {s.get('end', 0):.1f}s] {s.get('text', '').strip()}"
            for s in segments
        )
    else:
        transcript_text = result.get("text", "")

    chat_section = ""
    spike_report = ""
    if chat_path and chat_path.exists():
        entries = _read_chat_entries(chat_path)  # 全件読み込み
        if entries:
            spike_report = analyze_chat_spikes(entries)
            chat_lines = [f"[{t:.1f}s] {text}" for t, text in entries]
            chat_section = (
                "\n## ライブチャット分析レポート\n"
                f"{spike_report}\n\n"
                "## ライブチャットログ一覧（タイムスタンプは動画開始からの秒数）\n"
                + "\n".join(chat_lines)
            )

    from web.prompts import get_suggest_clips_prompt

    prompt = get_suggest_clips_prompt(
        transcript_text=transcript_text,
        chat_section=chat_section,
        extra_prompt=extra_prompt,
    )

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。設定画面からAPIキーを入力してください。")

    from google import genai
    from google.genai import types
    from web.schemas import ClipSuggestion

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=gemini_model or GEMINI_MODEL_ID,
        contents=[prompt],
        config=types.GenerateContentConfig(
            max_output_tokens=8192,
            response_mime_type="application/json",
            response_schema=list[ClipSuggestion],
        ),
    )

    text = response.text.strip()
    try:
        clips = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            raise ValueError(f"Could not parse clip suggestions from Gemini response:\n{text[:500]}")
        clips = json.loads(m.group())

    # デフォルト設定のマージ（縦型・画面分割表示をデフォルトとする）
    for clip in clips:
        if "vertical" not in clip:
            clip["vertical"] = True
        if "verticalMode" not in clip:
            clip["verticalMode"] = "split"

    return enrich_clip_caption_effects(clips, segments), spike_report


# ── Silence cut ───────────────────────────────────────────────────────────────

def cut_silence_from_clips(
    clips: list[dict], segments: list[dict], min_silence_sec: float = 2.0
) -> list[dict]:
    """
    Trim leading/trailing silence and build cutIntervals for silent gaps.
    Uses transcription segment boundaries — no audio processing needed.
    """
    MARGIN = 0.15  # seconds to keep before/after speech

    result = []
    for clip in clips:
        c_start, c_end = clip["start_sec"], clip["end_sec"]

        segs = [s for s in segments if s.get("end", 0) > c_start and s.get("start", 0) < c_end]
        if not segs:
            result.append(clip)
            continue

        # Trim clip boundaries to speech
        trimmed_start = max(c_start, segs[0]["start"] - MARGIN)
        trimmed_end   = min(c_end,   segs[-1]["end"]  + MARGIN)

        # Collect cut intervals (the silent gaps themselves)
        cut_intervals: list[dict] = []
        for i in range(len(segs) - 1):
            gap_start = segs[i]["end"]
            gap_end   = segs[i + 1]["start"]
            if gap_end - gap_start >= min_silence_sec:
                cut_start = gap_start + MARGIN
                cut_end   = gap_end   - MARGIN
                if cut_end > cut_start:
                    cut_intervals.append({"startSec": cut_start, "endSec": cut_end})

        new_clip = {**clip, "start_sec": trimmed_start, "end_sec": trimmed_end}
        if cut_intervals:
            new_clip["cutIntervals"] = cut_intervals
        result.append(new_clip)

    return result


# ── Merge legacy split clips ──────────────────────────────────────────────────

def merge_split_clips(clips: list[dict]) -> list[dict]:
    """
    Convert old-format split clips (_concat_group / _concat_index) to the new
    single-clip + cutIntervals format.  Clips without _concat_group pass through.
    """
    import re as _re
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for clip in clips:
        if clip.get("_concat_group"):
            groups[clip["_concat_group"]].append(clip)

    if not groups:
        return clips  # fast path — nothing to migrate

    result: list[dict] = []
    seen: set[str] = set()

    for clip in clips:
        g = clip.get("_concat_group")
        if not g:
            result.append(clip)
            continue
        if g in seen:
            continue
        seen.add(g)
        members = sorted(groups[g], key=lambda c: c.get("_concat_index", 0))
        cut_intervals = [
            {"startSec": members[j]["end_sec"], "endSec": members[j + 1]["start_sec"]}
            for j in range(len(members) - 1)
        ]
        base_title = _re.sub(r"\s*\(\d+\)$", "", members[0].get("title", ""))
        merged = {
            **members[0],
            "title": base_title,
            "start_sec": members[0]["start_sec"],
            "end_sec": members[-1]["end_sec"],
        }
        if cut_intervals:
            merged["cutIntervals"] = cut_intervals
        merged.pop("_concat_group", None)
        merged.pop("_concat_index", None)
        result.append(merged)

    return result


# ── Concat ────────────────────────────────────────────────────────────────────

def concat_clips(paths: list[Path], out_path: Path) -> Path:
    """Concatenate video files in order using ffmpeg concat demuxer (stream copy, no re-encode)."""
    list_file = out_path.parent / f"_concat_{out_path.stem}.txt"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{p.absolute()}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", str(out_path)],
            check=True,
            capture_output=True,
            **cfg.no_window_kwargs(),
        )
    finally:
        list_file.unlink(missing_ok=True)
    return out_path


# ── Source video dimensions ───────────────────────────────────────────────────

def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Return (width, height) of the video using ffprobe. Falls back to (1920, 1080)."""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=s=x:p=0",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
            **cfg.no_window_kwargs(),
        )
        w, h = proc.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _ffprobe_field(video_path: Path, entries: str, stream: str | None = "v:0") -> str:
    cmd = ["ffprobe", "-v", "error"]
    if stream:
        cmd += ["-select_streams", stream]
    cmd += ["-show_entries", entries, "-of", "default=nw=1:nk=1", str(video_path)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, **cfg.no_window_kwargs()
    )
    return proc.stdout.strip().splitlines()[0].strip()


def get_video_duration(video_path: Path) -> float:
    """Return the video's duration in seconds using ffprobe. Falls back to 0.0."""
    try:
        return float(_ffprobe_field(video_path, "format=duration", stream=None))
    except Exception:
        return 0.0


def get_audio_channels(video_path: Path) -> int:
    """Return the audio channel count using ffprobe, or 0 when there is no audio
    stream (ffprobe prints nothing, so the field lookup raises)."""
    try:
        return max(0, int(_ffprobe_field(video_path, "stream=channels", stream="a:0")))
    except Exception:
        return 0


# ── Cut intervals ─────────────────────────────────────────────────────────────
# These two functions mirror ClipComposition.tsx exactly (the `intervals` and
# `effectiveCaptions` useMemos). Any change there must be mirrored here, otherwise
# a Remotion render and a Premiere export of the same clip disagree on duration.

def compute_keep_intervals(
    start_sec: float, end_sec: float, cut_intervals: list[dict] | None
) -> list[dict]:
    """Turn cutIntervals (regions to remove) into the list of regions to keep."""
    if not cut_intervals:
        return [{"startSec": start_sec, "endSec": end_sec}]

    sorted_cuts = sorted(
        (
            iv for iv in cut_intervals
            if _finite(iv.get("startSec")) and _finite(iv.get("endSec"))
            and iv["endSec"] > iv["startSec"]
        ),
        key=lambda iv: iv["startSec"],
    )
    if not sorted_cuts:
        return [{"startSec": start_sec, "endSec": end_sec}]

    keeps: list[dict] = []
    cursor = start_sec
    for cut in sorted_cuts:
        # Clamp to the clip range so Studio's default {0,0} or out-of-range values
        # can't move the cursor backward.
        cut_start = max(cursor, min(end_sec, cut["startSec"]))
        cut_end = max(cut_start, min(end_sec, cut["endSec"]))
        if cut_start > cursor + 0.01:
            keeps.append({"startSec": cursor, "endSec": cut_start})
        cursor = cut_end
    if cursor < end_sec - 0.01:
        keeps.append({"startSec": cursor, "endSec": end_sec})

    return keeps or [{"startSec": start_sec, "endSec": end_sec}]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and x == x and x not in (float("inf"), float("-inf"))


def remap_captions_to_cuts(
    captions: list[dict], keep_intervals: list[dict], start_sec: float
) -> list[dict]:
    """Shift clip-relative caption times so they line up after cut regions are removed."""
    if len(keep_intervals) <= 1:
        return captions

    result: list[dict] = []
    output_offset_ms = 0.0
    for iv in keep_intervals:
        iv_start_ms = (iv["startSec"] - start_sec) * 1000
        iv_end_ms = (iv["endSec"] - start_sec) * 1000
        for cap in captions:
            if cap["endMs"] <= iv_start_ms or cap["startMs"] >= iv_end_ms:
                continue
            result.append({
                **cap,
                "startMs": output_offset_ms + max(0.0, cap["startMs"] - iv_start_ms),
                "endMs": output_offset_ms + min(iv_end_ms - iv_start_ms, cap["endMs"] - iv_start_ms),
            })
        output_offset_ms += iv_end_ms - iv_start_ms
    return result


# ── Render ────────────────────────────────────────────────────────────────────

# Max characters per karaoke-active caption token — see split_captions_for_karaoke().
CAPTION_CHUNK_CHARS = 6
_CAPTION_BREAK_CHARS = set("、。！？・…♪")
_CAPTION_SOKUON = set("っッ")  # attaches to the character that follows it
_CAPTION_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮー")  # attaches to what precedes it


def _chunk_caption_text(text: str, max_chars: int = CAPTION_CHUNK_CHARS) -> list[str]:
    """Split text into a few small pieces, breaking preferentially at punctuation
    and otherwise every `max_chars` characters, then repairing any boundary that
    would strand a small kana that phonetically attaches to a neighboring
    character (see split_captions_for_karaoke() for why this exists)."""
    if not text:
        return []
    raw: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _CAPTION_BREAK_CHARS or len(buf) >= max_chars:
            raw.append(buf)
            buf = ""
    if buf:
        raw.append(buf)

    chunks: list[str] = []
    for piece in raw:
        if chunks and piece[0] in _CAPTION_SMALL_KANA:
            chunks[-1] += piece
        elif chunks and chunks[-1][-1] in _CAPTION_SOKUON:
            chunks[-1] += piece
        else:
            chunks.append(piece)
    return chunks


def split_captions_for_karaoke(captions: list[dict]) -> list[dict]:
    """Subdivide make_captions() output into smaller consecutive tokens so
    CaptionPage's karaoke active-word highlight has more than one token per page
    to alternate between. Without this, most captions (91%+ of segments in a
    typical transcript, at an average 8.5 characters) are short enough to become
    a single token that's "active" for its entire on-screen time — so the
    highlight color never contrasts against the normal one, and captions read as
    permanently "emphasized". There's no real word-level ASR timing to split on
    (elevenlabs_transcribe.py's _words_to_segments() discards it), so timings
    here are interpolated proportionally by character position instead — the
    same approximation make_captions() already used for its one long-segment
    split.

    Only for the Remotion render path (see render_clip()). premiere_export.py
    calls make_captions() directly, unsplit, so exported SRT subtitle lines stay
    one-per-original-segment and readable rather than fragmenting into
    one-cue-per-word.
    """
    out: list[dict] = []
    for cap in captions:
        text = str(cap.get("text", ""))
        had_leading_space = text.startswith(" ")
        chunks = _chunk_caption_text(text.lstrip(" "))
        if not chunks:
            out.append(cap)
            continue
        start_ms, end_ms = cap["startMs"], cap["endMs"]
        total_chars = sum(len(c) for c in chunks)
        offset = 0
        for i, chunk_text in enumerate(chunks):
            piece_start = start_ms + (end_ms - start_ms) * (offset / total_chars)
            offset += len(chunk_text)
            piece_end = start_ms + (end_ms - start_ms) * (offset / total_chars)
            out.append({
                **cap,
                "text": (" " if i == 0 and had_leading_space else "") + chunk_text,
                "startMs": piece_start,
                "endMs": piece_end,
            })
    return out


def make_captions(
    segments: list[dict],
    start_sec: float,
    end_sec: float,
    default_effect: str | None = None,
    effects_enabled: bool = True,
) -> list[dict]:
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

        effect = detect_effect_for_segment(seg) if effects_enabled else ""
        is_comment = bool(seg.get("is_comment"))

        if len(text) >= 18:
            split_idx = text.find("、")
            if split_idx == -1 or split_idx < 5 or split_idx > len(text) - 5:
                split_idx = len(text) // 2
            else:
                split_idx += 1

            half_time = start_ms + (end_ms - start_ms) * (split_idx / len(text))
            p1: dict = {"text": " " + text[:split_idx].strip(), "startMs": start_ms, "endMs": half_time}
            p2: dict = {"text": " " + text[split_idx:].strip(), "startMs": half_time, "endMs": end_ms}
            if effect:
                p1["effect"] = effect
                p2["effect"] = effect
            if is_comment:
                p1["isComment"] = True
                p2["isComment"] = True
            captions.append(p1)
            captions.append(p2)
        else:
            cap: dict = {"text": " " + text, "startMs": start_ms, "endMs": end_ms}
            if effect:
                cap["effect"] = effect
            if is_comment:
                cap["isComment"] = True
            captions.append(cap)
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
    src_aspect: float = 16 / 9,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    start_sec = clip["start_sec"]
    end_sec = clip["end_sec"]
    vertical = bool(clip.get("vertical", False))
    vertical_mode = clip.get("verticalMode", "split")
    crop_x = float(clip.get("cropX", 93))
    title = clip.get("title", f"clip_{index:02d}")
    # Strip only characters that are truly invalid in filenames; Japanese is fine on macOS/Linux
    _invalid = set('/\\:*?"<>|\x00')
    safe = "".join("_" if c in _invalid else c for c in title).strip()[:60] or f"clip_{index:02d}"
    if vertical:
        safe += f"_{vertical_mode}"
    safe += f"_{int(start_sec)}"

    video_abs = video_path.resolve()
    effects_enabled = bool(clip.get("captionEffectsEnabled", True))

    props_data: dict = {
        "videoSrc": video_abs.name,
        "startSec": start_sec,
        "endSec": end_sec,
        "vertical": vertical,
        "verticalMode": vertical_mode,
        "cropX": crop_x,
        "faceCamZoom": float(clip.get("faceCamZoom", 2.0)),
        "faceCamY": float(clip.get("faceCamY", 100)),
        "splitTopRatio": float(clip.get("splitTopRatio", 4.5)),
        "mainZoom": float(clip.get("mainZoom", 1.0)),
        "mainCropX": float(clip.get("mainCropX", 50)),
        "mainCropY": float(clip.get("mainCropY", 50)),
        "title": clip.get("title", ""),
        "captions": split_captions_for_karaoke(
            make_captions(segments, start_sec, end_sec, clip.get("captionEffect"), effects_enabled=effects_enabled)
        ),
        "srcAspect": clip.get("srcAspect", src_aspect),
    }
    if clip.get("captionFontSize"):
        props_data["captionFontSize"] = int(clip["captionFontSize"])
    if effects_enabled and clip.get("captionEffect") in CAPTION_EFFECTS:
        props_data["captionEffect"] = clip["captionEffect"]
    if clip.get("captionFont"):
        props_data["captionFont"] = clip["captionFont"]
    if clip.get("cutIntervals"):
        props_data["cutIntervals"] = clip["cutIntervals"]
    if clip.get("titleMaxLines") is not None:
        props_data["titleMaxLines"] = int(clip["titleMaxLines"])
    props_data.update(theme_store.resolve_theme_props(clip.get("theme")))
    effect_count = sum(1 for c in props_data["captions"] if c.get("effect"))
    if effect_count:
        log(f"  ⚡ エフェクト付き字幕: {effect_count} 件")
    props = json.dumps(props_data, ensure_ascii=False)

    output_path = out_dir / f"{index:02d}_{safe}.mp4"

    if not (REMOTION_DIR / "node_modules").exists():
        log("Installing Remotion dependencies...")
        subprocess.run(["npm", "install"], cwd=REMOTION_DIR, check=True, **cfg.no_window_kwargs())

    import tempfile

    # 動画ファイルが大きいため、downloads/をそのまま--public-dirにすると
    # Remotionのbundlerが全ファイルをコピーしてしまう。そこで動画1本だけを
    # 一時ディレクトリに用意して--public-dirにする。以前はsymlinkで参照して
    # いたが、Remotionのbundlerはpublic-dirを一時バンドル用ディレクトリへ
    # コピーする際にsymlinkの中身を正しく転送せず404になることが判明したため、
    # ハードリンク(同一ボリューム内なら実体コピーなしで済む)を優先し、
    # 別ボリューム等でハードリンクが使えない場合のみ実コピーにフォールバックする。
    tmp_pub = Path(tempfile.mkdtemp(prefix="remotion_pub_"))
    try:
        try:
            os.link(video_abs, tmp_pub / video_abs.name)
        except OSError:
            shutil.copy2(video_abs, tmp_pub / video_abs.name)
        for name in ["kkrn_icon_user_2.png", "Onoma-Pop04.mp3"]:
            src = REMOTION_DIR / "public" / name
            if src.exists():
                shutil.copy2(src, tmp_pub / name)

        proc = subprocess.Popen(
            cfg.get_npx_cmd() + [
                "remotion", "render",
                "ClipComposition",
                str(output_path.absolute()),
                "--props", props,
                "--public-dir", str(tmp_pub),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=REMOTION_DIR,
            **cfg.no_window_kwargs(),
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
    finally:
        shutil.rmtree(tmp_pub, ignore_errors=True)

    return output_path
