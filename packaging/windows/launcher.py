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

import atexit
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
WEB_DIR = APP_ROOT / "web" if (APP_ROOT / "web").exists() else APP_ROOT.parent.parent / "web"
HOST = "127.0.0.1"
PORT = 8000
START_TIMEOUT_SEC = 30.0

LOG_DIR = Path(os.getenv("LOCALAPPDATA") or str(APP_ROOT)) / "Kirinuki" / "logs"
LOG_PATH = LOG_DIR / "launcher.log"
SERVER_LOG_PATH = LOG_DIR / "server.log"
MAX_LOG_BYTES = 5 * 1024 * 1024  # a runaway retry loop must not be able to fill the disk


def _redirect_streams() -> None:
    """pythonw.exe has no console, so sys.stdout/sys.stderr (and sys.__stdout__/
    sys.__stderr__) are None — not a dummy stream, actually None. Anything that
    calls e.g. sys.stdout.isatty() (uvicorn's own logging setup does, on startup)
    crashes immediately. Point them at a real file before any of that runs."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stream = open(SERVER_LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = sys.__stdout__ = sys.__stderr__ = stream


def _log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.unlink()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n")
    except OSError:
        pass  # nowhere left to report this — data dir itself isn't writable


def cleanup() -> None:
    """サーバーおよび Remotion Studio プロセスのクリーンアップを行う"""
    _log("cleaning up server resources...")
    try:
        from app import STUDIO_PORT, _kill_process_on_port, _shutdown_studio_proc
        _shutdown_studio_proc()
        _kill_process_on_port(STUDIO_PORT)
        _log("Remotion Studio process cleaned up successfully")
    except Exception as e:
        _log(f"cleanup error: {e}")


atexit.register(cleanup)


_COOKIE_EXTENSION_CHROME_URL = (
    "https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
    "cclelndahbckbenkjhflpdbgdldlbecc?hl=ja"
)


def _cookie_guide_text(data_dir: Path) -> str:
    return (
        "【Cookieの手動保存手順】\n\n"
        "ログインが必要な動画（年齢制限・メンバー限定など）をダウンロードするには、\n"
        "手動で書き出したCookieが必要です。\n\n"
        "1. ブラウザに「Get cookies.txt LOCALLY」拡張機能を追加します。\n"
        f"   Chrome: {_COOKIE_EXTENSION_CHROME_URL}\n\n"
        "2. 拡張機能の管理画面で「シークレットモードでの実行を許可する」を有効にします。\n\n"
        "3. シークレット（プライベート）ウィンドウを開き、YouTubeにログインします。\n\n"
        "4. 同じタブで https://www.youtube.com/robots.txt を開きます。\n\n"
        "5. 拡張機能のアイコンをクリックし「Export As」で書き出して、\n"
        "   下記フォルダに保存してください。\n"
        "   ファイル名は末尾が「cookies.txt」であれば自動で認識されます\n"
        "   （例: 127.0.0.1_cookies.txt）。\n\n"
        f"   {data_dir}\n\n"
        "6. シークレットウィンドウを閉じてください。"
    )


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
    import uvicorn
    from app import app  # web/app.py's FastAPI instance

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def _open_folder(path: Path) -> None:
    """指定されたフォルダを作成し、OSの標準ファイルマネージャーで開く"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", str(path)])
        else:
            import subprocess
            subprocess.run(["xdg-open", str(path)])
        _log(f"opened folder: {path}")
    except Exception as e:
        _log(f"failed to open folder {path}: {e}")


def _show_cookie_guide_dialog(data_dir: Path) -> None:
    """Cookieの手動保存手順テキストファイルをOSの既定エディタ（メモ帳など）で開く"""
    try:
        import config as cfg
        guide_path = cfg.ensure_cookie_guide_file(data_dir)
        if sys.platform == "win32":
            os.startfile(str(guide_path))
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", str(guide_path)])
        else:
            import subprocess
            subprocess.run(["xdg-open", str(guide_path)])
        _log(f"opened cookie guide file: {guide_path}")
    except Exception as e:
        _log(f"failed to open cookie guide file: {e}")


def _try_show_pystray_manager(data_dir: Path) -> bool:
    """pystrayを使用してシステムトレイ（タスクトレイ）に常駐し、メニューから各種操作を行えるようにする"""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError as e:
        _log(f"pystray module import failed: {e}")
        return False

    try:
        icon_path = APP_ROOT / "icon.ico"
        if icon_path.exists():
            image = Image.open(icon_path)
        else:
            # 64x64 の緑丸アイコンを自動生成
            image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.ellipse((8, 8, 56, 56), fill=(16, 124, 65))

        def on_open_browser(icon, item):
            webbrowser.open(f"http://{HOST}:{PORT}")

        def on_open_data_dir(icon, item):
            _open_folder(data_dir)

        def on_open_remotion_out(icon, item):
            # Remotion Studio の手動レンダーは仕様上ここ固定（data_dir/clips には出せない）
            _open_folder(APP_ROOT / "remotion" / "out")

        def on_show_cookie_guide(icon, item):
            _show_cookie_guide_dialog(data_dir)

        def on_shutdown(icon, item):
            icon.stop()

        menu = pystray.Menu(
            pystray.MenuItem(f"🟢 Kirinuki サーバー実行中 (http://{HOST}:{PORT})", lambda i, item: None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("🌐 Web画面（ブラウザ）を開く", on_open_browser, default=True),
            pystray.MenuItem("📂 インストールフォルダを開く", on_open_data_dir),
            pystray.MenuItem("🎬 Remotion（動画出力先）を開く", on_open_remotion_out),
            pystray.MenuItem("🍪 Cookieの手動保存手順を見る", on_show_cookie_guide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌ Kirinuki サーバーを終了する", on_shutdown),
        )

        icon = pystray.Icon("Kirinuki", image, "Kirinuki サーバーマネージャー", menu)
        _log("showing pystray server manager in system tray")

        try:
            icon.notify(
                f"Kirinuki サーバーが起動しました。\nhttp://{HOST}:{PORT}",
                "Kirinuki サーバーマネージャー"
            )
        except Exception:
            pass

        icon.run()
        return True
    except Exception as e:
        _log(f"pystray manager failed: {e}")
        return False


def _show_server_manager(data_dir: Path) -> None:
    """サーバーマネージャーUIを表示する（pystray タスクトレイ常駐 -> フォールバックの順に試行）"""
    if _try_show_pystray_manager(data_dir):
        return

    # フォールバック (イベント待機)
    _log("showing fallback server manager (event wait)")
    try:
        stop_event = threading.Event()
        stop_event.wait()
    except KeyboardInterrupt:
        pass


def main() -> None:
    _redirect_streams()
    _log("launcher starting")

    # メインスレッドでもWebディレクトリのインポートやyt-dlpのパス解決を行えるようにする
    sys.path.insert(0, str(WEB_DIR))
    os.environ["KIRINUKI_BIN_DIR"] = os.pathsep.join([str(APP_ROOT / "bin"), str(APP_ROOT / "node")])
    import config as cfg
    cfg.bootstrap_bin_path()

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
    data_dir = cfg.get_data_dir()

    # 常駐GUIマネージャーを表示
    _show_server_manager(data_dir)

    _log("user requested shutdown. exiting process.")
    cleanup()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        _show_error("予期しないエラーが発生しました:\n\n" + traceback.format_exc())

