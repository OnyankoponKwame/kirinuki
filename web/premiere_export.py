"""Adobe Premiere Pro export — caption-free clip material + SRT.

A companion to the Remotion renderer, not a replacement. Remotion produces the
*finished* clip (captions, title bar, effects burned in). This produces *material*
for a human editor who wants to do the captioning themselves — per clip:

  - an mp4 with the vertical framing (crop / split) and the cut intervals applied,
    but no captions, title bar, or effects,
  - an SRT timed to match that mp4 exactly.

Baking the framing into the pixels rather than shipping a project file means the
result works the same whichever NLE it lands in, and nothing depends on how any
importer interprets an interchange format. The trade-off is that crop position
and cuts can no longer be adjusted downstream — re-export from the app instead.

(An earlier version also emitted an FCP7 XML timeline. Once framing and cuts were
baked in, that file only placed one whole mp4 on one sequence — something dragging
the mp4 into Premiere already does — so it was dropped rather than maintained.)

Every geometry value comes from `compute_layers()`, which mirrors
ClipComposition.tsx, so exported material and a Remotion render of the same clip
are framed identically.

What never survives the trip: karaoke-style active-word highlighting, the
captionEffect animations, the title bar text, and theme colours.
"""

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ── Layout constants (mirrored from remotion/src/ClipComposition.tsx) ─────────

VERTICAL_PADDING = 12
TITLE_H_PADDING = 4
MIN_TITLE_BAR_H = 280
MAX_TITLE_FONT_PX = 140
DEFAULT_SRC_ASPECT = 16 / 9
SHORTS_SAFE_TOP_RATIO = 0.05

# Encode settings for the exported material. CRF 18 is visually transparent at
# these sizes while staying far smaller than an intermediate codec.
VIDEO_CODEC_ARGS = [
    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
]
AUDIO_CODEC_ARGS = ["-c:a", "aac", "-b:a", "192k"]
CANVAS_COLOR = "0x111111"  # matches ClipComposition's backgroundColor "#111"


def _jsround(x: float) -> int:
    """Match JavaScript's Math.round (half away from zero toward +inf), not
    Python's banker's rounding, so the geometry matches ClipComposition exactly."""
    return math.floor(x + 0.5)


# ── Title bar geometry (mirrored from ClipComposition.tsx) ────────────────────

def _effective_width(s: str) -> float:
    """CJK = 1.0em, ASCII (codepoint < 256) = 0.55em."""
    return sum(0.55 if ord(ch) < 256 else 1.0 for ch in s)


def _auto_split_title(title: str, usable_width: float, max_lines: int = 2) -> str:
    if "\n" in title:
        return title
    total_em = _effective_width(title)
    if total_em * MAX_TITLE_FONT_PX <= usable_width:
        return title

    best2_idx, best2_max = 1, math.inf
    for i in range(1, len(title)):
        left_em = _effective_width(title[:i])
        max_em = max(left_em, total_em - left_em)
        if max_em < best2_max:
            best2_max, best2_idx = max_em, i

    if max_lines <= 2 or best2_max * MAX_TITLE_FONT_PX <= usable_width or len(title) < 3:
        return f"{title[:best2_idx]}\n{title[best2_idx:]}"

    best3_idx1, best3_idx2, best3_max = 1, 2, math.inf
    for i in range(1, len(title) - 1):
        for j in range(i + 1, len(title)):
            line1_em = _effective_width(title[:i])
            line2_em = _effective_width(title[i:j])
            line3_em = _effective_width(title[j:])
            max_em = max(line1_em, line2_em, line3_em)
            if max_em < best3_max:
                best3_max = max_em
                best3_idx1, best3_idx2 = i, j

    return f"{title[:best3_idx1]}\n{title[best3_idx1:best3_idx2]}\n{title[best3_idx2:]}"


def _calc_title_bar_height(
    title: str, container_width: int, min_height: int = MIN_TITLE_BAR_H, max_lines: int = 2
) -> int:
    if not title or max_lines == 0:
        return 0
    display = _auto_split_title(title, container_width - TITLE_H_PADDING * 2, max_lines)
    lines = display.split("\n")
    usable_width = container_width - TITLE_H_PADDING * 2
    longest_em = max(_effective_width(l) for l in lines)
    fs_by_width = usable_width / longest_em if longest_em > 0 else MAX_TITLE_FONT_PX
    font_size = min(MAX_TITLE_FONT_PX, math.floor(fs_by_width))
    auto_wrap_lines = math.ceil(longest_em * font_size / usable_width)
    estimated_lines = max(len(lines), auto_wrap_lines, 2)
    return max(
        min_height,
        _jsround(font_size * 1.2 * estimated_lines + VERTICAL_PADDING * 2),
    )


# ── Framing ───────────────────────────────────────────────────────────────────

@dataclass
class Layer:
    """One instance of the source video placed in the output sequence.

    x/y/w/h are the scaled video's bounding box in sequence pixels. `window`
    (x0, y0, x1, y1) is the visible region — anything outside it is cropped away.
    """
    name: str
    x: float
    y: float
    w: float
    h: float
    window: tuple[float, float, float, float] | None = None
    audio: bool = True

    def ffmpeg_geometry(self, seq_w: int, seq_h: int) -> "_Geometry | None":
        """Pixel-space scale / crop / overlay values for the ffmpeg filtergraph.

        The layer is scaled to (w, h), the part falling outside its visible window
        (or outside the canvas) is cropped off, and what remains is overlaid at the
        window's top-left. Returns None when nothing of the layer is visible.
        """
        wx0, wy0, wx1, wy1 = self.window or (0, 0, seq_w, seq_h)
        wx0, wy0 = max(0.0, wx0), max(0.0, wy0)
        wx1, wy1 = min(float(seq_w), wx1), min(float(seq_h), wy1)

        vx0, vy0 = max(self.x, wx0), max(self.y, wy0)
        vx1, vy1 = min(self.x + self.w, wx1), min(self.y + self.h, wy1)
        if vx1 - vx0 < 1 or vy1 - vy0 < 1:
            return None

        scale_w, scale_h = max(1, _jsround(self.w)), max(1, _jsround(self.h))
        crop_x, crop_y = max(0, _jsround(vx0 - self.x)), max(0, _jsround(vy0 - self.y))
        crop_w = max(1, min(_jsround(vx1 - vx0), scale_w - crop_x))
        crop_h = max(1, min(_jsround(vy1 - vy0), scale_h - crop_y))
        return _Geometry(
            scale_w, scale_h, crop_w, crop_h, crop_x, crop_y,
            max(0, _jsround(vx0)), max(0, _jsround(vy0)),
        )


@dataclass(frozen=True)
class _Geometry:
    scale_w: int
    scale_h: int
    crop_w: int
    crop_h: int
    crop_x: int
    crop_y: int
    overlay_x: int
    overlay_y: int


def sequence_size(clip: dict) -> tuple[int, int]:
    return (1080, 1920) if clip.get("vertical") else (1920, 1080)


@dataclass(frozen=True)
class SplitGeometry:
    """Where the two panels of 二段構成モード sit vertically in the sequence."""
    seq_w: int
    seq_h: int
    safe_top: int
    title_bar_h: int
    main_top: int
    main_h: int
    bottom_top: int
    bottom_h: int


def split_geometry(
    title: str,
    split_top_ratio: float,
    seq_w: int = 1080,
    seq_h: int = 1920,
    safe_top_ratio: float = SHORTS_SAFE_TOP_RATIO,
    title_bar_min_height: int = MIN_TITLE_BAR_H,
    title_max_lines: int = 2,
) -> SplitGeometry:
    """Split-mode panel bounds — mirrors ClipComposition.tsx's 二段構成モード block.

    Separated from compute_layers() because the position picker in the web UI needs
    the panel heights to turn a rectangle drawn on a frame into that panel's zoom and
    position (上段: mainZoom / mainCropX / mainCropY, 下段: faceCamZoom / cropX /
    faceCamY), and the title bar's height (which the panel boundary depends on) is
    only derivable from the title text by this module's port of calcTitleBar().

    `safe_top_ratio` / `title_bar_min_height` / `title_max_lines` come from the clip's
    theme (see theme_store.THEME_FIELDS' titleTopMargin / titleBarMinHeight /
    titleMaxLines) — all three are per-theme in ClipComposition.tsx, so they must be
    passed in here rather than read off the module constants/defaults whenever a
    clip's theme overrides them.
    """
    safe_top = _jsround(seq_h * safe_top_ratio)
    title_bar_h = _calc_title_bar_height(title, seq_w, title_bar_min_height, title_max_lines)
    main_top = safe_top + title_bar_h
    main_h = _jsround((seq_h - main_top) * split_top_ratio / 10)
    bottom_top = main_top + main_h
    return SplitGeometry(
        seq_w=seq_w,
        seq_h=seq_h,
        safe_top=safe_top,
        title_bar_h=title_bar_h,
        main_top=main_top,
        main_h=main_h,
        bottom_top=bottom_top,
        bottom_h=seq_h - bottom_top,
    )


def compute_layers(clip: dict, src_aspect: float, theme: dict | None = None) -> list[Layer]:
    """Reproduce ClipComposition's framing as a stack of positioned video layers.

    `theme` (a resolved themeColors dict, see theme_store.resolve_theme_props) supplies
    the titleTopMargin / titleBarMinHeight / titleMaxLines / splitTopRatio overrides that
    shift the split-mode panel boundary — colors and fonts don't matter here since no
    title text is drawn (see module docstring), but the gap it reserves still has to
    match Remotion's.

    Returned bottom-most first (V1, V2, ...).
    """
    seq_w, seq_h = sequence_size(clip)

    if not clip.get("vertical"):
        # Horizontal: objectFit "contain" into the 16:9 frame.
        fit = min(seq_w / (seq_h * src_aspect), 1.0)
        w = seq_h * src_aspect * fit
        h = seq_h * fit
        return [Layer("main", (seq_w - w) / 2, (seq_h - h) / 2, w, h)]

    if clip.get("verticalMode", "split") == "crop":
        zoom = float(clip.get("faceCamZoom", 1.0) or 1.0)
        scaled_h = _jsround(seq_h * zoom)
        scaled_w = _jsround(scaled_h * src_aspect)
        top = -_jsround((scaled_h - seq_h) * (float(clip.get("faceCamY", 50)) / 100))
        left = -_jsround((scaled_w - seq_w) * (float(clip.get("cropX", 93)) / 100))
        # The sequence bounds do the clipping — no Crop filter needed.
        return [Layer("crop", left, top, scaled_w, scaled_h)]

    # Split: full-width panel on top, zoomed face-cam below.
    theme = theme or {}
    top_margin = theme.get("titleTopMargin")
    safe_top_ratio = (top_margin / 100) if top_margin is not None else SHORTS_SAFE_TOP_RATIO
    title_bar_min_height = theme.get("titleBarMinHeight") or MIN_TITLE_BAR_H
    title_max_lines = 2 if theme.get("titleMaxLines") is None else int(theme["titleMaxLines"])
    split_top_ratio = float(theme.get("splitTopRatio") or 4.5)
    geo = split_geometry(
        str(clip.get("title", "")), split_top_ratio, seq_w, seq_h,
        safe_top_ratio=safe_top_ratio, title_bar_min_height=title_bar_min_height,
        title_max_lines=title_max_lines,
    )
    main_top, main_h = geo.main_top, geo.main_h
    bottom_top, bottom_h = geo.bottom_top, geo.bottom_h

    main_inner_h = _jsround(main_h * float(clip.get("mainZoom", 1.0) or 1.0))
    main_inner_w = _jsround(main_inner_h * src_aspect)
    main_video_top = -_jsround((main_inner_h - main_h) * (float(clip.get("mainCropY", 50)) / 100))
    raw_main_left = _jsround(seq_w / 2 - main_inner_w * (float(clip.get("mainCropX", 50)) / 100))
    main_video_left = max(-(main_inner_w - seq_w), min(0, raw_main_left))

    face_zoom = float(clip.get("faceCamZoom", 1.5) or 1.5)
    face_inner_h = _jsround(bottom_h * face_zoom)
    face_inner_w = _jsround(face_inner_h * src_aspect)
    face_video_top = -_jsround((face_inner_h - bottom_h) * (float(clip.get("faceCamY", 50)) / 100))
    raw_face_left = _jsround(seq_w / 2 - face_inner_w * (float(clip.get("cropX", 93)) / 100))
    face_video_left = max(-(face_inner_w - seq_w), min(0, raw_face_left))

    return [
        Layer(
            "main-panel",
            main_video_left,
            main_top + main_video_top,
            main_inner_w,
            main_inner_h,
            window=(0, main_top, seq_w, main_top + main_h),
        ),
        Layer(
            "face-cam",
            face_video_left,
            bottom_top + face_video_top,
            face_inner_w,
            face_inner_h,
            window=(0, bottom_top, seq_w, bottom_top + bottom_h),
            audio=False,  # muted in ClipComposition — the top panel carries the audio
        ),
    ]


# ── SRT ───────────────────────────────────────────────────────────────────────

def _srt_timestamp(ms: float) -> str:
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msec:03d}"


def normalize_cues(captions: list[dict]) -> tuple[list[dict], int]:
    """Sort and de-overlap cues for formats that need one chronological track.

    Transcription backends sometimes emit overlapping or duplicated segments (the
    same phrase transcribed twice with different spans). Remotion tolerates that —
    it just draws overlapping caption pages — but Premiere's caption track holds
    one caption at a time, and an SRT whose cues run backwards imports badly.

    Cues are sorted by start time and truncated at the next cue's start. Anything
    left with no duration is dropped; the count comes back so the caller can say so.
    Returns (cues, dropped).
    """
    ordered = sorted(
        (
            # make_captions prefixes a space to mark TikTok page breaks — not wanted here.
            {**c, "text": str(c.get("text", "")).strip()}
            for c in captions
            if str(c.get("text", "")).strip()
        ),
        key=lambda c: (float(c["startMs"]), float(c["endMs"])),
    )

    result: list[dict] = []
    dropped = 0
    for i, cue in enumerate(ordered):
        start = float(cue["startMs"])
        end = float(cue["endMs"])
        if i + 1 < len(ordered):
            end = min(end, float(ordered[i + 1]["startMs"]))
        if end - start < 1:
            dropped += 1
            continue
        result.append({**cue, "startMs": start, "endMs": end})
    return result, dropped


def build_srt(cues: list[dict]) -> str:
    """Render normalized clip-relative cues as SubRip. Premiere imports this directly."""
    return "\n".join(
        f"{n}\n{_srt_timestamp(float(c['startMs']))} --> {_srt_timestamp(float(c['endMs']))}\n{c['text']}\n"
        for n, c in enumerate(cues, start=1)
    )


def _sanitize(name: str, fallback: str) -> str:
    invalid = set('/\\:*?"<>|\x00')
    safe = "".join("_" if c in invalid else c for c in name).strip()[:60]
    return safe or fallback


# ── Material rendering (ffmpeg) ───────────────────────────────────────────────

def build_filtergraph(
    layers: list[Layer], seq_w: int, seq_h: int, n_inputs: int, has_audio: bool
) -> str:
    """Filtergraph that concatenates the kept ranges and reproduces the framing.

    Every geometry value comes from `compute_layers()` — the same function that
    drives the layout — so the exported material and a Remotion render of the same
    clip are framed identically by construction.
    """
    parts: list[str] = []

    # 1. Stitch the kept ranges together. Each range is a separate seeked input,
    #    so ffmpeg never decodes the gaps between them.
    if n_inputs > 1:
        streams = "".join(f"[{i}:v][{i}:a]" if has_audio else f"[{i}:v]" for i in range(n_inputs))
        parts.append(
            f"{streams}concat=n={n_inputs}:v=1:a={1 if has_audio else 0}"
            + ("[vsrc][aout]" if has_audio else "[vsrc]")
        )
    else:
        parts.append("[0:v]null[vsrc]")
        if has_audio:
            parts.append("[0:a]anull[aout]")

    geometries = [(l, l.ffmpeg_geometry(seq_w, seq_h)) for l in layers]
    visible = [(l, g) for l, g in geometries if g]
    if not visible:
        raise RuntimeError("クリップの構図設定では映像が1ピクセルも表示されません")

    # 2. Canvas, so uncovered areas (letterbox, the title-bar gap in split mode)
    #    match ClipComposition's background rather than being transparent.
    parts.append(f"color=c={CANVAS_COLOR}:s={seq_w}x{seq_h}[base]")

    if len(visible) > 1:
        parts.append(f"[vsrc]split={len(visible)}" + "".join(f"[src{i}]" for i in range(len(visible))))
        sources = [f"[src{i}]" for i in range(len(visible))]
    else:
        sources = ["[vsrc]"]

    # 3. Scale → crop to the visible window → overlay at that window's corner.
    for i, (layer, g) in enumerate(visible):
        parts.append(
            f"{sources[i]}scale={g.scale_w}:{g.scale_h},"
            f"crop={g.crop_w}:{g.crop_h}:{g.crop_x}:{g.crop_y}[l{i}]"
        )

    prev = "[base]"
    for i, (layer, g) in enumerate(visible):
        out_label = "[vout]" if i == len(visible) - 1 else f"[o{i}]"
        # shortest=1 on the first overlay bounds the otherwise-endless color source.
        shortest = ":shortest=1" if i == 0 else ""
        parts.append(f"{prev}[l{i}]overlay={g.overlay_x}:{g.overlay_y}{shortest}{out_label}")
        prev = out_label

    return ";".join(parts)


def build_ffmpeg_command(
    video_path: Path, keeps: list[dict], filtergraph: str, out_path: Path, has_audio: bool
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for iv in keeps:
        # Seek before -i so each range starts decoding at its own keyframe;
        # -t (duration) rather than -to, whose meaning as an input option varies.
        duration = max(0.001, iv["endSec"] - iv["startSec"])
        cmd += ["-ss", f"{iv['startSec']:.6f}", "-t", f"{duration:.6f}", "-i", str(video_path)]
    cmd += ["-filter_complex", filtergraph, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]", *AUDIO_CODEC_ARGS]
    else:
        cmd += ["-an"]
    cmd += [*VIDEO_CODEC_ARGS, "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
            str(out_path)]
    return cmd


def render_material(
    video_path: Path,
    keeps: list[dict],
    layers: list[Layer],
    seq_w: int,
    seq_h: int,
    has_audio: bool,
    out_path: Path,
    log=lambda _: None,
    check_cancel=None,
    set_proc=None,
) -> None:
    """Encode one caption-free clip mp4 with the framing and cuts applied."""
    import config as cfg

    import tempfile
    import threading

    target_sec = sum(iv["endSec"] - iv["startSec"] for iv in keeps)
    filtergraph = build_filtergraph(layers, seq_w, seq_h, len(keeps), has_audio)
    cmd = build_ffmpeg_command(video_path, keeps, filtergraph, out_path, has_audio)

    # stderr goes to a file rather than a pipe: it is only read when the encode
    # fails, and an unread pipe would deadlock ffmpeg once its buffer filled.
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as errf:
        err_path = Path(errf.name)

    try:
        with open(err_path, "w", encoding="utf-8") as errfile:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errfile, text=True,
                encoding="utf-8", errors="replace", **cfg.no_window_kwargs(),
            )
        if set_proc:
            set_proc(proc)

        def pump_progress() -> None:
            # -progress emits key=value lines; out_time_us tracks encoded position.
            last_pct = -10
            assert proc.stdout
            for line in proc.stdout:
                if not line.startswith("out_time_us=") or target_sec <= 0:
                    continue
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                pct = max(0, min(100, int(done / target_sec * 100)))
                if pct >= last_pct + 10:
                    last_pct = pct - pct % 10
                    log(f"    …{pct}%")

        # Progress is read on its own thread: ffmpeg's stdout is block-buffered, so
        # polling it for cancellation would leave the request hanging for tens of
        # seconds between lines. Cancellation is checked on a timer instead.
        reader = threading.Thread(target=pump_progress, daemon=True)
        reader.start()

        canceled = False
        while True:
            try:
                proc.wait(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                if check_cancel and check_cancel():
                    canceled = True
                    proc.terminate()
                    try:
                        # Short grace period: the partial file is discarded either
                        # way, so there is nothing worth letting ffmpeg finalise.
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
        reader.join(timeout=2)

        if canceled or (check_cancel and check_cancel()):
            out_path.unlink(missing_ok=True)  # don't leave a truncated clip behind
            raise RuntimeError("書き出しがキャンセルされました")
        if proc.returncode != 0:
            tail = "\n".join(
                err_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-4:]
            )
            raise RuntimeError(f"ffmpeg failed for {out_path.name}:\n{tail}")
    finally:
        err_path.unlink(missing_ok=True)


# ── Package builder ───────────────────────────────────────────────────────────

README = """Kirinuki → 編集用 素材書き出し
=======================================

■ このフォルダの中身
  NN_タイトル.mp4   縦構図（クロップ／二段構成）とカット区間を適用済みの映像。
                    字幕・タイトルバー・エフェクトは入っていません。
  NN_タイトル.srt   その mp4 に時間軸が一致する字幕。

■ この素材の使い方（Adobe Premiere Pro）
  1. このフォルダの中の mp4 を、Premiere のプロジェクトパネルにドラッグ＆ドロップします。
     そのままタイムラインパネルにドラッグすると、素材に合ったサイズ
     （1080×1920 など）のシーケンスが自動で作られます。
  2. メニューから「ファイル > 読み込み...」（英語版UIでは File > Import...）を選び、
     同じフォルダの `*.srt` を選択して読み込みます。
  3. プロジェクトパネルに読み込まれた srt を、シーケンスのキャプショントラックへ
     ドラッグします。mp4 とタイムコードが一致しているので、そのまま位置が合います。
  4. 字幕の文字（誤字脱字の修正など）やフォント・色・アニメーションなどのスタイルは、
     Premiere 側で自由に編集できます。

  ※ DaVinci Resolve / Final Cut Pro など他の編集ソフトでも、
     このフォルダの mp4 と srt をそのまま読み込めば同じように使えます。

■ 構図とカットは焼き込み済みです
  縦構図の寄せ位置・ズーム、および無音カット等の区間削除は mp4 に適用済みのため、
  編集ソフト側では変更できません。調整したい場合はアプリ側で設定を変えて
  書き出し直してください。
  二段構成の上部に空いている帯は、タイトルバー用に確保された余白です。
  お好みのテロップを乗せてください。

■ 再現されないもの（Remotion 側の最終レンダー専用）
  - カラオケ風のアクティブワード強調
  - captionEffect による演出（anger / panic / laugh ...）
  - タイトルバーの文字、カラーテーマ
  これらが必要な場合は、これまで通りアプリの「▶ レンダリング」を使ってください。
"""


def export_package(
    clips: list[dict],
    indices: list[int],
    video_path: Path,
    segments: list[dict],
    out_dir: Path,
    src_aspect: float,
    log=lambda _: None,
    check_cancel=None,
    set_proc=None,
) -> Path:
    """Write the editing-material package and return its directory."""
    import pipeline as pl
    import theme_store

    out_dir.mkdir(parents=True, exist_ok=True)

    channels = pl.get_audio_channels(video_path)
    has_audio = channels > 0
    src_w, src_h = pl.get_video_dimensions(video_path)
    log(
        f"  ソース: {src_w}×{src_h} / "
        f"{'音声なし' if not has_audio else f'音声 {channels}ch'}"
    )

    for n, idx in enumerate(indices, start=1):
        if check_cancel and check_cancel():
            raise RuntimeError("書き出しがキャンセルされました")

        clip = clips[idx]
        start_sec = float(clip["start_sec"])
        end_sec = float(clip["end_sec"])
        keeps = pl.compute_keep_intervals(start_sec, end_sec, clip.get("cutIntervals"))
        raw_captions = pl.make_captions(segments, start_sec, end_sec, clip.get("captionEffect"))
        captions, dropped = normalize_cues(
            pl.remap_captions_to_cuts(raw_captions, keeps, start_sec)
        )
        clip_aspect = float(clip.get("srcAspect", src_aspect) or src_aspect)
        theme_colors = theme_store.resolve_theme_props(clip.get("theme")).get("themeColors")
        layers = compute_layers(clip, clip_aspect, theme_colors)
        seq_w, seq_h = sequence_size(clip)

        base = f"{idx:02d}_{_sanitize(str(clip.get('title', '')), f'clip_{idx:02d}')}"
        kept_sec = sum(iv["endSec"] - iv["startSec"] for iv in keeps)
        overlap_note = f" ⚠ 重複字幕 {dropped} 件を除外" if dropped else ""
        log(
            f"  [{n}/{len(indices)}] {clip.get('title', '')} — {seq_w}×{seq_h} / "
            f"{len(keeps)} 区間 / {round(kept_sec, 1)}秒 / 字幕 {len(captions)} 件{overlap_note}"
        )

        material_path = out_dir / f"{base}.mp4"
        render_material(
            video_path=video_path, keeps=keeps, layers=layers,
            seq_w=seq_w, seq_h=seq_h, has_audio=has_audio, out_path=material_path,
            log=log, check_cancel=check_cancel, set_proc=set_proc,
        )
        log(f"    ✓ {material_path.name} ({pl.get_video_duration(material_path):.1f}秒)")

        srt = build_srt(captions)
        if srt:
            (out_dir / f"{base}.srt").write_text(srt, encoding="utf-8")

    (out_dir / "README.txt").write_text(README, encoding="utf-8")
    return out_dir
