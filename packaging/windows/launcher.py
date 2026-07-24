"""Kirinuki launcher — starts the local server and opens the browser. No console window.

This is the double-click entry point for a packaged Windows install, run via
pythonw.exe (bundled by build.ps1). pythonw.exe has no console at all, so any
unhandled exception or stray print() is normally swallowed with zero visible
feedback — the app just "does nothing". Everything here is written to survive
that: failures are written to a log file AND shown in a message box, since a
silent crash is indistinguishable from a slow startup otherwise.

Layout this script expects (see build.ps1 / kirinuki.iss):
    app/                <- this file lives here (== {app} in the Inno Setup script)
      web/              <- copy of the repo's web/ dir (app.py, pipeline.py, config.py, static/)
      audio-chunking/
      remotion/
      python/           <- embeddable Python + installed dependencies
      node/             <- portable Node.js runtime
      bin/              <- ffmpeg.exe, ffprobe.exe, yt-dlp.exe
"""

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
WEB_DIR = APP_ROOT / "web"
HOST = "127.0.0.1"
PORT = 8000
START_TIMEOUT_SEC = 30.0

LOG_DIR = Path(os.getenv("LOCALAPPDATA") or str(APP_ROOT)) / "Kirinuki"
LOG_PATH = LOG_DIR / "launcher.log"


def _log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass  # nowhere left to report this — data dir itself isn't writable


def _show_error(message: str) -> None:
    _log(message)
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None,
        f"{message}\n\nログファイル: {LOG_PATH}",
        "Kirinuki の起動に失敗しました",
        0x10,  # MB_ICONERROR
    )


def run_server() -> None:
    """Runs on a background thread. Any exception here is caught by main()'s caller."""
    sys.path.insert(0, str(WEB_DIR))
    # Bundled ffmpeg/ffprobe/yt-dlp (bin/) and portable Node (node/) — picked up by
    # config.bootstrap_bin_path() so subprocess calls in pipeline.py/app.py resolve
    # them without a system-wide PATH entry.
    os.environ["KIRINUKI_BIN_DIR"] = os.pathsep.join([str(APP_ROOT / "bin"), str(APP_ROOT / "node")])

    import uvicorn
    from app import app  # web/app.py's FastAPI instance

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def main() -> None:
    _log("launcher starting")
    errors: list[BaseException] = []

    def target() -> None:
        try:
            run_server()
        except BaseException as exc:  # must survive to be shown — pythonw has no console
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    deadline = time.time() + START_TIMEOUT_SEC
    ready = False
    while time.time() < deadline and not errors:
        try:
            with socket.create_connection((HOST, PORT), timeout=1):
                ready = True
                break
        except OSError:
            if not thread.is_alive():
                break  # thread exited without an exception AND without opening the port
            time.sleep(0.3)

    if not ready:
        if errors:
            tb = "".join(traceback.format_exception(type(errors[0]), errors[0], errors[0].__traceback__))
            _show_error("サーバーの起動中にエラーが発生しました:\n\n" + tb)
        else:
            _show_error(f"サーバーが{START_TIMEOUT_SEC:.0f}秒以内に起動しませんでした。")
        return

    _log("server ready, opening browser")
    webbrowser.open(f"http://{HOST}:{PORT}")
    thread.join()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        _show_error("予期しないエラーが発生しました:\n\n" + traceback.format_exc())
