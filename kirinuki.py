import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent / "audio-chunking"))
from audio_chunking_code import transcribe_audio_in_chunks


def download_video(url: str, output_dir: Path) -> tuple[Path, Path | None]:
    """
    Download video with yt-dlp and fetch live chat if available.
    Returns (video_path, chat_path_or_None).
    Audio extraction is handled downstream by preprocess_audio.
    """
    output_dir.mkdir(exist_ok=True)
    template = str(output_dir / "%(id)s.%(ext)s")

    print(f"Downloading video: {url}")
    cookies_path = Path(__file__).parent / "cookies.txt"
    # cookies.txtが存在すれば、ロックを避けるためにそれを優先使用する。
    # 存在しなければブラウザから抽出し、同時にcookies.txtにキャッシュする。
    if cookies_path.exists():
        cookie_args = ["--cookies", str(cookies_path)]
    else:
        cookie_args = ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)]

    cmd = [
        "yt-dlp",
        "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "--merge-output-format", "mp4",
        "--write-subs",
        "--sub-langs", "live_chat",
        "--print", "after_move:filepath",
    ] + cookie_args + [
        "-o", template,
        url,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # もしキャッシュされたcookies.txtを使用して失敗した場合は、最新のクッキーをブラウザから再取得してキャッシュ更新を試みます
        if cookies_path.exists() and "--cookies-from-browser" not in cmd:
            print("Download with cached cookies failed. Refreshing cookies from browser...")
            retry_cmd = []
            for arg in cmd:
                if arg == str(cookies_path):
                    retry_cmd.append(arg)
                    continue
                if arg == "--cookies":
                    retry_cmd.extend(["--cookies-from-browser", "chrome", "--cookies"])
                    continue
                retry_cmd.append(arg)
            result = subprocess.run(
                retry_cmd,
                capture_output=True,
                text=True,
            )

        # クッキーのロックエラー（ブラウザ起動中）等で失敗した場合
        stderr_lower = result.stderr.lower()
        if result.returncode != 0 and "cookie" in stderr_lower and ("could not copy" in stderr_lower or "error" in stderr_lower or "failed" in stderr_lower):
            if cookies_path.exists():
                try:
                    cookies_path.unlink()
                except OSError:
                    pass
            raise RuntimeError(
                "Download failed. YouTube requires cookies to bypass bot detection, but the cookies database is locked because Chrome is running.\n"
                "Please completely close your Chrome browser and try again (subsequent downloads will work with Chrome open)."
            )

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{result.stderr}")

    lines = [l for l in result.stdout.strip().splitlines() if l.endswith(".mp4")]
    if not lines:
        raise RuntimeError(f"Could not find downloaded mp4 in yt-dlp output:\n{result.stdout}")
    video_path = Path(lines[-1])
    if not video_path.exists():
        raise RuntimeError(f"Downloaded file not found: {video_path}")
    print(f"Video saved: {video_path}")

    chat_path = video_path.with_suffix("").with_suffix(".live_chat.json")
    if chat_path.exists():
        print(f"Live chat saved: {chat_path}")
    else:
        chat_path = None
        print("Live chat: not available for this video")

    return video_path, chat_path


def main():
    parser = argparse.ArgumentParser(
        description="Kirinuki: YouTube video download + transcription"
    )
    parser.add_argument("url", help="YouTube URL to download")
    parser.add_argument(
        "--language", default="ja",
        help="Audio language code for Whisper (default: ja)"
    )
    parser.add_argument(
        "--chunk-length", type=int, default=600,
        help="Chunk length in seconds (default: 600)"
    )
    parser.add_argument(
        "--overlap", type=int, default=10,
        help="Overlap between chunks in seconds (default: 10)"
    )
    args = parser.parse_args()

    downloads_dir = Path(__file__).parent / "downloads"

    video_path, chat_path = download_video(args.url, downloads_dir)

    print("\nStarting transcription with Groq whisper-large-v3...")
    result = transcribe_audio_in_chunks(
        video_path,
        chunk_length=args.chunk_length,
        overlap=args.overlap,
        language=args.language,
    )

    print("\n--- Transcript preview (first 500 chars) ---")
    print(result["text"][:500])
    print("---")

    return result


if __name__ == "__main__":
    main()
