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
WEB_DIR = APP_ROOT / "web" if (APP_ROOT / "web").exists() else APP_ROOT.parent.parent / "web"
HOST = "127.0.0.1"
PORT = 8000
START_TIMEOUT_SEC = 30.0

LOG_DIR = Path(os.getenv("LOCALAPPDATA") or str(APP_ROOT)) / "Kirinuki"
LOG_PATH = LOG_DIR / "launcher.log"
SERVER_LOG_PATH = LOG_DIR / "server.log"


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
    import uvicorn
    from app import app  # web/app.py's FastAPI instance

    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


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

    _log("server ready, showing startup prompt")

    import ctypes
    import subprocess

    should_open_browser = False

    # 1. 起動準備ダイアログを表示
    if sys.platform == "win32":
        # MB_OKCANCEL = 1
        ret = ctypes.windll.user32.MessageBoxW(
            None,
            "Kirinuki サーバーが起動しました。\n"
            "ブラウザの起動とクッキーの取得（ボット対策の回避用）を行います。\n\n"
            "Chromeブラウザが開いている場合は、完全に閉じた状態で [OK] を押してください。\n"
            "（手動で cookies.txt を配置済みの場合は [キャンセル] で進めてください）",
            "Kirinuki - 起動準備",
            1,
        )
        if ret == 1:  # IDOK
            should_open_browser = True
            try:
                cookies_path = cfg.get_data_dir() / "cookies.txt"
                _log("pre-fetching cookies from chrome")
                cookies_path.parent.mkdir(parents=True, exist_ok=True)
                while True:
                    res = subprocess.run(
                        [
                            "yt-dlp",
                            "--cookies-from-browser", "chrome",
                            "--write-cookies", str(cookies_path),
                            "--skip-download",
                            "https://www.youtube.com"
                        ],
                        capture_output=True,
                        text=True,
                        **cfg.no_window_kwargs()
                    )
                    if res.returncode == 0:
                        _log("cookies pre-fetched and cached successfully")
                        if sys.platform == "win32":
                            ctypes.windll.user32.MessageBoxW(
                                None,
                                "YouTubeのクッキー情報の取得に成功しました！\n\n"
                                f"保存先: {cookies_path}",
                                "Kirinuki - 取得成功",
                                64, # MB_ICONINFORMATION
                            )
                        break

                    # クッキーの取得失敗
                    stderr_lower = res.stderr.lower()
                    _log(f"failed to pre-fetch cookies: {res.stderr}")
                    if sys.platform == "win32":
                        if "cookie" in stderr_lower and ("could not copy" in stderr_lower or "lock" in stderr_lower or "permission" in stderr_lower):
                            error_msg = (
                                "Chromeブラウザが起動中、またはバックグラウンドで実行中のためクッキーを取得できません。\n\n"
                                "【解決方法】\n"
                                "1. Chromeブラウザを完全に閉じてください（タスクバー右下のインジケーターやタスクマネージャー等でプロセスが残っていないか確認してください）。その後、[OK] を押して再試行してください。\n\n"
                                "2. または、ブラウザ拡張機能を使ってクッキーをエクスポートし、以下のフォルダに「cookies.txt」として手動保存してから [キャンセル] を押してください。\n"
                                f"保存先フォルダ: {cookies_path.parent}"
                            )
                        else:
                            error_msg = (
                                "クッキー情報の取得に失敗しました。\n"
                                "別の原因（Chromeがインストールされていない等）の可能性があります。\n\n"
                                "【エラー詳細】\n"
                                f"{res.stderr}\n\n"
                                "※手動でクッキーファイルを用意する場合は、ブラウザ拡張機能を使って「cookies.txt」としてエクスポートし、以下のフォルダに保存してから [キャンセル] を押してください。\n"
                                f"保存先フォルダ: {cookies_path.parent}"
                            )
                        
                        retry_ret = ctypes.windll.user32.MessageBoxW(
                            None,
                            error_msg,
                            "Kirinuki - クッキー取得エラー",
                            1, # MB_OKCANCEL
                        )
                        if retry_ret == 2:  # IDCANCEL
                            _log("user canceled cookie pre-fetch prompt")
                            break
                    else:
                        break
            except Exception as e:
                _log(f"failed to pre-fetch cookies: {e}")
                if sys.platform == "win32":
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"クッキー取得処理中に例外が発生しました:\n{e}",
                        "Kirinuki - 例外エラー",
                        16, # MB_ICONERROR
                    )
    else:
        # win32以外はデフォルトで自動起動に進む
        should_open_browser = True

    # 2. 必要に応じてブラウザを起動し、作業フォルダを開く
    if should_open_browser:
        _log("opening browser and data directory")
        webbrowser.open(f"http://{HOST}:{PORT}")
        try:
            data_dir = cfg.get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(data_dir))
            else:
                subprocess.run(["open", str(data_dir)])
        except Exception as e:
            _log(f"failed to open data directory: {e}")

    # 3. 実行中常駐ダイアログを表示
    _log("showing server active dialog")
    if sys.platform == "win32":
        # MB_OK = 0
        ctypes.windll.user32.MessageBoxW(
            None,
            "Kirinuki サーバーを実行中です。\n\n"
            "アプリの利用を終了する場合は、[OK] を押してください。\n"
            "Python サーバーと関連プロセスが完全にシャットダウンします。",
            "Kirinuki サーバーマネージャー",
            0,
        )
    else:
        # win32以外はキー入力待機などの簡易ブロック
        _log("press enter to shutdown in non-windows")
        try:
            input()
        except KeyboardInterrupt:
            pass

    _log("user requested shutdown. exiting process.")
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        _show_error("予期しないエラーが発生しました:\n\n" + traceback.format_exc())
