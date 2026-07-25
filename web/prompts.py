"""Prompt templates loader and builders for Kirinuki."""

import os
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def get_suggest_clips_prompt(
    transcript_text: str,
    chat_section: str = "",
    extra_prompt: str | None = None,
) -> str:
    """Load the suggest_clips.md template and inject data fields."""
    prompt_file = PROMPTS_DIR / "suggest_clips.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt template file not found: {prompt_file}")

    template = prompt_file.read_text(encoding="utf-8")

    extra_prompt_section = ""
    if extra_prompt and extra_prompt.strip():
        extra_prompt_section = f"\n### 追加指示\n{extra_prompt.strip()}\n"

    return (
        template
        .replace("{{TRANSCRIPT_TEXT}}", transcript_text)
        .replace("{{CHAT_SECTION}}", chat_section)
        .replace("{{EXTRA_PROMPT_SECTION}}", extra_prompt_section)
    )
