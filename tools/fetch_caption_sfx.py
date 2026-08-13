#!/usr/bin/env python3
"""字幕エフェクト用の効果音セットを効果音ラボから取得し remotion/public/sfx/ に配置する。

    python3 tools/fetch_caption_sfx.py

素材元: 効果音ラボ https://soundeffect-lab.info/
  商用利用無料・クレジット表記不要。ただし「アプリにデフォルト素材として組み込んで配布する」
  ことは利用規約で禁止されている。このリポジトリを自分の環境で使う分には該当しないが、
  packaging/windows のインストーラを第三者に配布する場合は素材を差し替えること
  （CC BY 4.0 の OtoLogic https://otologic.jp/ なら再配布可。クレジット表記が条件）。

取得した音はピーク -3dBFS に正規化してモノラル mp3 で書き出す。
実際の音量は remotion/src/captionStyles.ts の SFX テーブルの gain で決まる。
スクリプトは最後に「テーブルに書くべき durMs / gain」を出力するので、
音を差し替えたらその値を captionStyles.ts に反映すること（durMs が実長より短いと音が切れる）。
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "remotion" / "public" / "sfx"

BASE = "https://soundeffect-lab.info"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)

TARGET_PEAK_DB = -3.0  # 全ファイル共通のピーク（クリップ回避＋音量の基準そろえ）
TARGET_RMS_DB = -26.0  # gain 算出時の目標ラウドネス

# 水滴のように「アタックだけ鋭くて後がほぼ無音」の音は、RMS 合わせだと gain が過剰になり
# ピークだけが突出して聞こえる。そういう素材はここで手動の値に上書きする。
GAIN_OVERRIDE: dict[str, float] = {
    "sad": 0.45,
}

# 効果音キー -> (カテゴリ, 元ファイル名, 日本語ラベル, 用途)
SOURCES: dict[str, tuple[str, str, str, str]] = {
    "pop":     ("anime",  "pa1",          "パッ",           "軽いリアクション・かわいい・短い訴求語"),
    "impact":  ("anime",  "text-impact1", "ドンッ",          "怒り・断言／テロップ用インパクト音"),
    "boom":    ("anime",  "jajean1",      "ジャジャーン",      "予想外の大オチ・強い驚き"),
    "ding":    ("anime",  "kira1",        "キラッ",          "盛り上がり・神プレイ・告知"),
    "sparkle": ("anime",  "eye-shine1",   "キラーン",         "映え・エモい"),
    "boing":   ("anime",  "boyon1",       "ビヨン",          "笑い・ツッコミ"),
    "swell":   ("anime",  "horror-text1", "ホラーテロップ",     "ホラー・不穏"),
    "glitch":  ("anime",  "dj-scratch1",  "レコードスクラッチ",  "バグ・ラグ・違和感"),
    "sad":     ("anime",  "teardrop1",    "ぽちゃん",         "悲しみ・別れ"),
    "alarm":   ("button", "warning2",     "警告音",          "悲鳴・パニック"),
    "levelup": ("anime",  "levelup1",     "レベルアップ",      "勝敗・クリア・ボス戦"),
    "wonder":  ("anime",  "question2",    "ピコン？",         "疑問・困惑"),
}


def _download(category: str, name: str, dest: Path) -> None:
    """効果音ラボは Referer を見て直リンクを弾くので、配布ページを Referer に付ける。"""
    url = f"{BASE}/sound/{category}/mp3/{name}.mp3"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": f"{BASE}/sound/{category}/"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        data = res.read()
    if not data.startswith(b"ID3") and data[:2] != b"\xff\xfb":
        raise RuntimeError(f"{url}: mp3 ではないレスポンス（{len(data)} bytes）")
    dest.write_bytes(data)


def _measure(path: Path) -> tuple[float, float, float]:
    """(全長ms, ピークdBFS, 有音区間のRMS dBFS) を返す。"""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True,
    ).stdout
    n = len(raw) // 2
    if n == 0:
        raise RuntimeError(f"{path}: デコードできない")
    d = struct.unpack(f"<{n}h", raw[: n * 2])
    peak = max((abs(v) for v in d), default=1) or 1
    thr = peak * 0.02
    first = next((i for i, v in enumerate(d) if abs(v) >= thr), 0)
    last = next((i for i in range(n - 1, -1, -1) if abs(d[i]) >= thr), n - 1)
    span = d[first : last + 1]
    rms = math.sqrt(sum(float(v) * v for v in span) / max(1, len(span))) / 32768
    return n / 8000 * 1000, 20 * math.log10(peak / 32768), 20 * math.log10(max(rms, 1e-9))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, int, float]] = []
    with tempfile.TemporaryDirectory(prefix="kirinuki_sfx_") as tmp:
        tmp_dir = Path(tmp)
        for key, (category, name, label, _use) in SOURCES.items():
            src = tmp_dir / f"{key}.mp3"
            try:
                _download(category, name, src)
            except (urllib.error.URLError, RuntimeError) as e:
                print(f"  {key:9s} 取得失敗: {e}", file=sys.stderr)
                continue
            _, peak_db, _ = _measure(src)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(src),
                 "-af", f"volume={TARGET_PEAK_DB - peak_db:.2f}dB",
                 "-codec:a", "libmp3lame", "-q:a", "5", "-ac", "1", str(OUT_DIR / f"{key}.mp3")],
                check=True,
            )
            dur_ms, _, rms_db = _measure(OUT_DIR / f"{key}.mp3")
            gain = GAIN_OVERRIDE.get(key, min(1.0, max(0.15, 10 ** ((TARGET_RMS_DB - rms_db) / 20))))
            rows.append((key, math.ceil(dur_ms), round(gain, 2)))
            print(f"  {key:9s} {label:<10s} {dur_ms:6.0f}ms  gain {gain:.2f}  ← {category}/{name}.mp3")
            time.sleep(0.3)  # 連続リクエストを避ける

    print(f"\n{len(rows)} 個を {OUT_DIR} に配置しました。")
    print("captionStyles.ts の SFX テーブルに書く値:\n")
    for key, dur_ms, gain in rows:
        label = SOURCES[key][2]
        print(f'  {key}: {{ label: "{label}", durMs: {dur_ms}, gain: {gain}, leadMs: 0 }},')
    return 0


if __name__ == "__main__":
    sys.exit(main())
