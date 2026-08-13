"""Pydantic schemas for Gemini Structured Outputs in Kirinuki."""

from pydantic import BaseModel, Field


class CutInterval(BaseModel):
    startSec: float = Field(description="除去を開始するタイムスタンプ（秒）")
    endSec: float = Field(description="除去を終了するタイムスタンプ（秒）")


class ClipSuggestion(BaseModel):
    # appeals を title より先に置いているのは意図的。Gemini の構造化出力は
    # スキーマの定義順にフィールドを生成するため、先に訴求切り口を言語化させると
    # そのあとのタイトル・理由がその切り口に沿ったものになる。
    appeals: list[str] = Field(
        description=(
            "訴求切り口（この動画を見る理由）をメイン→サブ→補助の順に2〜3個。"
            "カタログの番号と名称をそのまま『13 フラグ回収』の形式で記述する"
        )
    )
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
