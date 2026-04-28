#!/usr/bin/env python3
"""Suggest clip points from a transcription using Claude."""

import argparse
import json
import re
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")


def build_prompt(transcription: dict, chat_messages: list | None) -> str:
    segments = transcription.get("segments", [])
    if segments:
        lines = [
            f"[{seg.get('start', 0):.1f}s - {seg.get('end', 0):.1f}s] {seg.get('text', '').strip()}"
            for seg in segments
        ]
        transcript_text = "\n".join(lines)
    else:
        transcript_text = transcription.get("text", "（タイムスタンプなし）")

    chat_section = ""
    if chat_messages:
        chat_lines = []
        for msg in chat_messages[:300]:
            try:
                for action in msg.get("replayChatItemAction", {}).get("actions", []):
                    renderer = (
                        action.get("addChatItemAction", {})
                        .get("item", {})
                        .get("liveChatTextMessageRenderer", {})
                    )
                    if renderer:
                        author = renderer.get("authorName", {}).get("simpleText", "?")
                        runs = renderer.get("message", {}).get("runs", [])
                        text = "".join(r.get("text", "") for r in runs)
                        chat_lines.append(f"{author}: {text}")
            except Exception:
                continue
        if chat_lines:
            chat_section = "\n## ライブチャット（抜粋）\n" + "\n".join(chat_lines[:200])

    return f"""以下はYouTube動画の文字起こしです（タイムスタンプ付き）。

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

条件:
- 開始・終了時間は文字起こしのタイムスタンプを参照する
- 各クリップは30秒〜5分程度
- 話の区切りが自然な部分を選ぶ
- ライブチャットが盛り上がっている部分を優先する（チャットがある場合）
"""


def suggest_clips(transcription_path: Path, chat_path: Path | None = None) -> list[dict]:
    with open(transcription_path, encoding="utf-8") as f:
        transcription = json.load(f)

    chat_messages = None
    if chat_path and chat_path.exists():
        with open(chat_path, encoding="utf-8") as f:
            chat_messages = [json.loads(line) for line in f if line.strip()]

    client = anthropic.Anthropic()
    prompt = build_prompt(transcription, chat_messages)

    print("Asking Claude to suggest clip points...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    match = re.search(r"\[.*?\]", response_text, re.DOTALL)
    if not match:
        raise ValueError(f"Could not parse clip suggestions:\n{response_text}")

    return json.loads(match.group())


def main():
    parser = argparse.ArgumentParser(description="Suggest clip points using Claude")
    parser.add_argument("transcription", help="Path to transcription _full.json")
    parser.add_argument("--chat", help="Path to live_chat.json")
    parser.add_argument(
        "--output",
        help="Output JSON path (default: same dir as transcription/clips.json)",
    )
    args = parser.parse_args()

    transcription_path = Path(args.transcription)
    chat_path = Path(args.chat) if args.chat else None
    output_path = (
        Path(args.output) if args.output else transcription_path.parent / "clips.json"
    )

    clips = suggest_clips(transcription_path, chat_path)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)

    print(f"\n{len(clips)} clips suggested:")
    for i, clip in enumerate(clips, 1):
        print(f"  {i}. [{clip['start_sec']:.1f}s - {clip['end_sec']:.1f}s] {clip['title']}")
    print(f"\nSaved to: {output_path}")


if __name__ == "__main__":
    main()
