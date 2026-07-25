"""Pydantic schemas for Gemini Structured Outputs in Kirinuki."""

from pydantic import BaseModel, Field


class CutInterval(BaseModel):
    startSec: float = Field(description="除去を開始するタイムスタンプ（秒）")
    endSec: float = Field(description="除去を終了するタイムスタンプ（秒）")


class ClipSuggestion(BaseModel):
    title: str = Field(description="YouTube Shorts/TikTok等でスクロールを止めさせる引きの強いタイトル")
    start_sec: float = Field(description="クリップの開始時間（秒）")
    end_sec: float = Field(description="クリップの終了時間（秒）")
    cutIntervals: list[CutInterval] | None = Field(
        default=None,
        description="無音・言い淀み・脱線などを除去するジャンプカット区間リスト（不要な場合はNoneまたは空リスト）"
    )
    captionEffect: str = Field(
        description="字幕の基本エフェクト (anger, scary, panic, shock, laugh, gaming, hype, cute, question, whisper, news, glitch, punch, pill, neon, pop, sad)"
    )
    reason: str = Field(description="切り抜き動画として選定した理由・見どころの説明")
