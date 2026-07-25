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

# Model used for clip suggestion (suggest_clips_from_result). Transcription does not
# use Gemini — see run_transcription()'s "groq" / "elevenlabs" branches.
GEMINI_MODEL_ID = "gemini-3.5-flash-lite"

PROJECT_DIR = Path(__file__).parent.parent
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
    out_dir = PROJECT_DIR / "transcriptions"
    out_dir.mkdir(exist_ok=True)
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

def download_video(
    url: str,
    output_dir: Path,
    log: Callable[[str], None],
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

    cookies_path = cfg.get_data_dir() / "cookies.txt"
    # cookies.txtが存在すれば、ロックを避けるためにそれを優先使用する。
    # 存在しなければブラウザから抽出し、同時にcookies.txtにキャッシュする。
    if cookies_path.exists():
        cookie_args = ["--cookies", str(cookies_path)]
    else:
        cookie_args = ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)]

    cmd = [
        "yt-dlp",
    ] + js_runtime_args + cookie_args + [
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--newline",
        "--print", "after_move:filepath",
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
        # もしキャッシュされたcookies.txtを使用して失敗した場合は、最新のクッキーをブラウザから再取得してキャッシュ更新を試みます
        if cookies_path.exists() and "--cookies-from-browser" not in cmd:
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

            proc = subprocess.Popen(
                retry_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **cfg.no_window_kwargs(),
            )
            stdout_lines = []
            assert proc.stdout
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log(line)
                    stdout_lines.append(line)
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
                "ダウンロードに失敗しました。YouTubeのボット制限を回避するためにクッキーが必要ですが、Chromeが起動中のためアクセスできません。"
                "一度Chromeブラウザを完全に閉じた状態で、再度実行してください（次回以降はChromeを開いたままでも動作します）。"
            )

    if proc.returncode != 0:
        raise RuntimeError("yt-dlp failed — check the URL and network connection")

    mp4_lines = [l for l in stdout_lines if l.endswith(".mp4")]
    if not mp4_lines:
        raise RuntimeError("Could not find downloaded mp4 in yt-dlp output")

    video_path = Path(mp4_lines[-1])
    if not video_path.exists():
        raise RuntimeError(f"Downloaded file missing: {video_path}")

    chat_path = video_path.with_suffix("").with_suffix(".live_chat.json")
    if not chat_path.exists():
        return video_path, None
    slim_live_chat(chat_path, log)
    return video_path, chat_path


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

    cookies_path = cfg.get_data_dir() / "cookies.txt"
    # cookies.txtが存在すれば、ロックを避けるためにそれを優先使用する。
    # 存在しなければブラウザから抽出し、同時にcookies.txtにキャッシュする。
    if cookies_path.exists():
        cookie_args = ["--cookies", str(cookies_path)]
    else:
        cookie_args = ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)]

    cmd = [
        "yt-dlp",
    ] + js_runtime_args + cookie_args + [
        "--skip-download",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--newline",
        "-o", template,
        url,
    ]

    cookie_error_detected = False

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
            # クッキーのロックエラーや未検出を検知
            if "cookie" in line.lower() and ("could not copy" in line.lower() or "error" in line.lower() or "failed" in line.lower()):
                cookie_error_detected = True

    proc.wait()

    # 失敗した時のハンドリング
    if proc.returncode != 0:
        # もしキャッシュされたcookies.txtを使用して失敗した場合は、最新のクッキーをブラウザから再取得してキャッシュ更新を試みます
        if cookies_path.exists() and "--cookies-from-browser" not in cmd:
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
            keyterms_str = "飴白, 飴白なび"
        keyterms_str = keyterms_str.replace("，", ",").replace("、", ",")
        keyterms = [k.strip() for k in keyterms_str.split(",") if k.strip()] if keyterms_str else None
        raw = transcribe_with_elevenlabs(
            video_path, language=language, initial_prompt=initial_prompt, audio_mode=audio_mode, keyterms=keyterms
        )
    return slim_transcription_result(raw)


def save_transcription(result: dict, video_path: Path) -> Path:
    out_dir = PROJECT_DIR / "transcriptions"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{video_path.stem}_{ts}_full.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


def save_clips(clips: list[dict], transcription_path: Path) -> Path:
    out_dir = PROJECT_DIR / "transcriptions"
    out_dir.mkdir(exist_ok=True)
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
    result: dict, chat_path: Path | None, extra_prompt: str | None = None
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
        entries = _read_chat_entries(chat_path, limit_lines=300)
        chat_lines = [f"[{t:.1f}s] {text}" for t, text in entries[:200]]
        if chat_lines:
            chat_section = "\n## ライブチャット（タイムスタンプは動画開始からの秒数）\n" + "\n".join(chat_lines)

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
    "cutIntervals": [{{"startSec": 135.0, "endSec": 142.0}}],
    "vertical": true,
    "verticalMode": "split",
    "captionEffect": "hype",
    "reason": "なぜこの部分が切り抜きに適しているか"
  }}
]

条件: 各クリップ30秒〜5分、話の区切りが自然な部分、チャットが盛り上がっている部分を優先。
cutIntervals は省略可能です。指定した区間を動画から除去します。無音・話が脱線・間延びした部分がある場合のみ指定してください。

captionEffect は字幕全体の基本効果です。次のルールで1つ選んでください:
- anger: 怒り、強い抗議、ふざけるな・許せない系の場面
- scary: 怖い、不穏、ホラー、背後や気配でゾッとする場面
- panic: 恐怖・悲鳴・逃げたい場面
- shock: 予想外の大オチ、強い驚き、！？が似合う場面
- laugh: 笑い、草、ツッコミ、コメント欄が笑っている場面
- gaming: ゲームの勝敗、ボス、コンボ、キル、クリア場面
- hype: 盛り上がり、成功、神プレイ、テンションが上がる場面
- cute: かわいい、尊い、癒やし場面
- question: 疑問、困惑、何が起きたかわからない場面
- whisper: 小声、内緒、落ち着いた含みのある場面
- news: 告知、発表、速報っぽい場面
- glitch: バグ、ラグ、フリーズ、違和感のある場面
- punch: 断言、結論、パンチライン、強い一言
- pill: 限定、無料、最強、おすすめなど短い訴求語
- neon: 映え、エモい、キラキラした場面
- pop: 軽いリアクション、汎用的に楽しい場面
- sad: 悲しみ、喪失感、別れ、やるせない・つらい場面
"""
    if extra_prompt:
        prompt += f"\n## 追加指示\n{extra_prompt}\n"

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。設定画面からAPIキーを入力してください。")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_MODEL_ID,
        contents=[prompt],
        config=types.GenerateContentConfig(max_output_tokens=8192),
    )

    text = response.text.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"Could not parse clip suggestions from Gemini response:\n{text[:500]}")

    clips = json.loads(m.group())
    return enrich_clip_caption_effects(clips, segments)


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

def make_captions(
    segments: list[dict],
    start_sec: float,
    end_sec: float,
    default_effect: str | None = None,
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

        effect = detect_effect_for_segment(seg)
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

    props_data: dict = {
        "videoSrc": video_abs.name,
        "startSec": start_sec,
        "endSec": end_sec,
        "vertical": vertical,
        "verticalMode": vertical_mode,
        "cropX": crop_x,
        "faceCamZoom": float(clip.get("faceCamZoom", 2.0)),
        "faceCamY": float(clip.get("faceCamY", 100)),
        "splitTopRatio": int(clip.get("splitTopRatio", 5)),
        "mainZoom": float(clip.get("mainZoom", 1.0)),
        "mainCropX": float(clip.get("mainCropX", 50)),
        "mainCropY": float(clip.get("mainCropY", 50)),
        "title": clip.get("title", ""),
        "captions": make_captions(segments, start_sec, end_sec, clip.get("captionEffect")),
        "srcAspect": clip.get("srcAspect", src_aspect),
    }
    if clip.get("captionFontSize"):
        props_data["captionFontSize"] = int(clip["captionFontSize"])
    if clip.get("captionEffect") in CAPTION_EFFECTS:
        props_data["captionEffect"] = clip["captionEffect"]
    if clip.get("captionFont"):
        props_data["captionFont"] = clip["captionFont"]
    if clip.get("cutIntervals"):
        props_data["cutIntervals"] = clip["cutIntervals"]
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
            [
                "npx", "remotion", "render",
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
