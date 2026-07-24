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

import anthropic

import config as cfg

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
    template = str(output_dir / "%(title).80s_%(id)s.%(ext)s")

    proc = subprocess.Popen(
        [
            "yt-dlp",
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
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
        **cfg.no_window_kwargs(),
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
    template = str(output_dir / "%(title).80s_%(id)s.%(ext)s")

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
        **cfg.no_window_kwargs(),
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
    transcription_model: str = "groq",
) -> dict:
    if transcription_model == "gemini":
        from gemini_transcribe import transcribe_with_gemini
        raw = transcribe_with_gemini(video_path, language=language, initial_prompt=initial_prompt)
    else:
        raw = transcribe_audio_in_chunks(video_path, language=language, initial_prompt=initial_prompt, audio_mode=audio_mode)
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

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が設定されていません。設定画面からAPIキーを入力してください。")

    client = anthropic.Anthropic(api_key=api_key, timeout=600)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"Could not parse clip suggestions from Claude response:\n{text[:500]}")

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
            captions.append(p1)
            captions.append(p2)
        else:
            cap: dict = {"text": " " + text, "startMs": start_ms, "endMs": end_ms}
            if effect:
                cap["effect"] = effect
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
    if clip.get("theme"):
        props_data["theme"] = clip["theme"]
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
    # Remotionのbundlerが全ファイルをコピーしてしまう。
    # シンボリックリンクはコピーされず転送されるため、動画はsymlinkで参照する。
    tmp_pub = Path(tempfile.mkdtemp(prefix="remotion_pub_"))
    try:
        os.symlink(video_abs, tmp_pub / video_abs.name)
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
