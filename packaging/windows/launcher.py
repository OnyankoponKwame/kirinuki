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


_COOKIE_EXTENSION_CHROME_URL = (
    "https://chromewebstore.google.com/detail/get-cookiestxt-locally/"
    "cclelndahbckbenkjhflpdbgdldlbecc?hl=ja"
)
_COOKIE_EXTENSION_FIREFOX_URL = "https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/"


def _cookie_guide_text(data_dir: Path) -> str:
    return (
        "【Cookieの手動保存手順】\n\n"
        "ログインが必要な動画（年齢制限・メンバー限定など）をダウンロードするには、\n"
        "手動で書き出したCookieが必要です。\n\n"
        "1. ブラウザに「Get cookies.txt LOCALLY」拡張機能を追加します。\n"
        f"   Chrome: {_COOKIE_EXTENSION_CHROME_URL}\n"
        f"   Firefox: {_COOKIE_EXTENSION_FIREFOX_URL}\n\n"
        "2. 拡張機能の管理画面で「シークレットモードでの実行を許可する」を有効にします。\n\n"
        "3. シークレット（プライベート）ウィンドウを開き、YouTubeにログインします。\n\n"
        "4. 同じタブで https://www.youtube.com/robots.txt を開きます。\n\n"
        "5. 拡張機能のアイコンをクリックし「Export As」で書き出して、\n"
        "   下記フォルダに保存してください。\n"
        "   ファイル名は末尾が「cookies.txt」であれば自動で認識されます\n"
        "   （例: 127.0.0.1_cookies.txt）。\n\n"
        f"   {data_dir}\n\n"
        "6. シークレットウィンドウを閉じてください。\n\n"
        "※ Cookieを使いすぎるとアカウントがBANされるリスクがあります。\n"
        "　 不要なときは使わない、メインのアカウントは使わないことをおすすめします。"
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

    _log("server ready, opening browser and data directory")
    webbrowser.open(f"http://{HOST}:{PORT}")
    data_dir = cfg.get_data_dir()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(data_dir))
        else:
            import subprocess
            subprocess.run(["open", str(data_dir)])
    except Exception as e:
        _log(f"failed to open data directory: {e}")

    # 3. 実行中常駐ダイアログを表示
    _log("showing server active dialog")
    import ctypes
    if sys.platform == "win32":
        MB_YESNOCANCEL = 3
        MB_ICONINFORMATION = 0x40
        IDYES, IDNO = 6, 7

        while True:
            # メインメニュー: [はい]=インストールフォルダを開く / [いいえ]=その他の操作 / [キャンセル]=終了
            ret = ctypes.windll.user32.MessageBoxW(
                None,
                "Kirinuki サーバーを実行中です。\n\n"
                "・インストールフォルダ（bin/やweb/等）を開くには [はい] を押してください。\n"
                "・データフォルダを開く/Cookie保存手順を見るには [いいえ] を押してください。\n"
                "・アプリを終了してサーバーを停止するには [キャンセル] を押してください。",
                "Kirinuki サーバーマネージャー",
                MB_YESNOCANCEL,
            )
            if ret == IDYES:
                try:
                    os.startfile(str(APP_ROOT))
                except Exception as e:
                    _log(f"failed to open install directory: {e}")
                continue
            if ret == IDNO:
                # サブメニュー: [はい]=データフォルダを開く / [いいえ]=Cookie保存手順を表示 / [キャンセル]=戻る
                sub = ctypes.windll.user32.MessageBoxW(
                    None,
                    "・データフォルダ（cookies.txt の保存先）を開くには [はい] を押してください。\n"
                    "・Cookieの手動保存手順を表示するには [いいえ] を押してください。\n"
                    "・メインメニューに戻るには [キャンセル] を押してください。",
                    "Kirinuki サーバーマネージャー — その他の操作",
                    MB_YESNOCANCEL,
                )
                if sub == IDYES:
                    try:
                        data_dir.mkdir(parents=True, exist_ok=True)
                        os.startfile(str(data_dir))
                    except Exception as e:
                        _log(f"failed to open data directory: {e}")
                elif sub == IDNO:
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        _cookie_guide_text(data_dir),
                        "Cookieの手動保存手順",
                        MB_ICONINFORMATION,
                    )
                continue
            # IDCANCEL、またはダイアログを閉じた場合 — 終了
            break
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
