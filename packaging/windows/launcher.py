"""Kirinuki launcher — starts the local server and opens the browser. No console window.

This is the double-click entry point for a packaged Windows install. It is meant to
be run with pythonw.exe (bundled by build.ps1), which has no console window at all,
so nothing flashes on screen — the browser tab opening is the only visible effect.

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
import webbrowser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
WEB_DIR = APP_ROOT / "web"
HOST = "127.0.0.1"
PORT = 8000

# Bundled ffmpeg/ffprobe/yt-dlp (bin/) and portable Node (node/) — picked up by
# config.bootstrap_bin_path() so every subprocess call in pipeline.py/app.py resolves
# them without needing a system-wide PATH entry.
os.environ["KIRINUKI_BIN_DIR"] = os.pathsep.join([str(APP_ROOT / "bin"), str(APP_ROOT / "node")])

sys.path.insert(0, str(WEB_DIR))


def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main() -> None:
    import uvicorn
    from app import app  # web/app.py's FastAPI instance

    thread = threading.Thread(
        target=lambda: uvicorn.run(app, host=HOST, port=PORT, log_level="warning"),
        daemon=True,
    )
    thread.start()

    # Best-effort wait so the browser doesn't hit a connection-refused page on a
    # slow first start; open regardless afterwards since there's no console to
    # report a startup failure to.
    _wait_for_port(HOST, PORT)
    webbrowser.open(f"http://{HOST}:{PORT}")
    thread.join()


if __name__ == "__main__":
    main()
