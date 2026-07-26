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
    """Cookieの手動保存手順をダイアログで表示する"""
    guide_text = _cookie_guide_text(data_dir)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            guide_text,
            "Cookieの手動保存手順",
            0x40,  # MB_ICONINFORMATION
        )
    else:
        print(guide_text)


def _try_show_tkinter_manager(data_dir: Path) -> bool:
    """Tkinterが利用可能な場合、専用GUIウィンドウでサーバーマネージャーを表示する"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        return False

    try:
        root = tk.Tk()
        root.title("Kirinuki サーバーマネージャー")
        root.geometry("480x400")
        root.resizable(False, False)

        # ウィンドウを画面中央付近に配置
        root.update_idletasks()
        width = root.winfo_width()
        height = root.winfo_height()
        x = (root.winfo_screenwidth() // 2) - (width // 2)
        y = (root.winfo_screenheight() // 2) - (height // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")

        icon_path = APP_ROOT / "icon.ico"
        if icon_path.exists():
            try:
                root.iconbitmap(str(icon_path))
            except Exception:
                pass

        # スタイル設定
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # ヘッダーエリア
        header_frame = ttk.Frame(root, padding=(20, 15, 20, 10))
        header_frame.pack(fill="x")

        ttk.Label(
            header_frame,
            text="🟢 Kirinuki サーバー実行中",
            font=("Segoe UI", 12, "bold"),
            foreground="#107c41",
        ).pack(anchor="w")

        ttk.Label(
            header_frame,
            text=f"アドレス: http://{HOST}:{PORT}",
            font=("Segoe UI", 9),
            foreground="#555555",
        ).pack(anchor="w", pady=(3, 0))

        # ボタンエリア
        btn_frame = ttk.Frame(root, padding=(20, 10, 20, 20))
        btn_frame.pack(fill="both", expand=True)

        def on_open_browser():
            webbrowser.open(f"http://{HOST}:{PORT}")

        def on_open_install_dir():
            _open_folder(APP_ROOT)

        def on_open_data_dir():
            _open_folder(data_dir)

        def on_show_cookie_guide():
            guide_win = tk.Toplevel(root)
            guide_win.title("Cookieの手動保存手順")
            guide_win.geometry("560x460")
            guide_win.transient(root)
            guide_win.grab_set()

            txt_frame = ttk.Frame(guide_win, padding=15)
            txt_frame.pack(fill="both", expand=True)

            txt = tk.Text(txt_frame, wrap="word", font=("メイリオ", 9), relief="solid", bd=1)
            txt.insert("1.0", _cookie_guide_text(data_dir))
            txt.config(state="disabled")
            txt.pack(fill="both", expand=True)

            ttk.Button(guide_win, text="閉じる", command=guide_win.destroy).pack(pady=10)

        def on_shutdown():
            root.destroy()

        buttons = [
            ("🌐  Web画面（ブラウザ）を開く", on_open_browser),
            ("📁  インストールフォルダを開く", on_open_install_dir),
            ("📂  データフォルダを開く (cookies.txt 保存先)", on_open_data_dir),
            ("🍪  Cookieの手動保存手順を見る", on_show_cookie_guide),
            ("❌  Kirinuki サーバーを終了する", on_shutdown),
        ]

        for text, cmd in buttons:
            btn = ttk.Button(btn_frame, text=text, command=cmd)
            btn.pack(fill="x", pady=4, ipady=4)

        root.protocol("WM_DELETE_WINDOW", on_shutdown)
        _log("showing Tkinter server manager window")
        root.mainloop()
        return True
    except Exception as e:
        _log(f"Tkinter manager failed: {e}")
        return False


def _try_show_taskdialog_manager(data_dir: Path) -> bool:
    """Windows環境でWin32 APIのTaskDialogIndirectを使用して専用ボタン付きダイアログを表示する"""
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    try:
        class TASKDIALOG_BUTTON(ctypes.Structure):
            _fields_ = [
                ("nButtonID", ctypes.c_int),
                ("pszButtonText", wintypes.LPCWSTR),
            ]

        TDF_USE_COMMAND_LINKS = 0x0001
        TDF_ALLOW_DIALOG_CANCELLATION = 0x0008
        TD_INFORMATION_ICON = 65533

        buttons = (TASKDIALOG_BUTTON * 5)(
            TASKDIALOG_BUTTON(101, f"Web画面（ブラウザ）を開く\nhttp://{HOST}:{PORT} を既定のブラウザで開きます"),
            TASKDIALOG_BUTTON(102, "インストールフォルダを開く\nプログラム本体や関連ツールがあるフォルダを開きます"),
            TASKDIALOG_BUTTON(103, "データフォルダを開く\ncookies.txt やログファイルが保存されるフォルダを開きます"),
            TASKDIALOG_BUTTON(104, "Cookieの手動保存手順を見る\n年齢制限・メンバー限定動画ダウンロード用の設定方法を表示します"),
            TASKDIALOG_BUTTON(105, "Kirinuki サーバーを終了する\nWebサーバーを停止してアプリケーションを終了します"),
        )

        class TASKDIALOGCONFIG(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("hwndParent", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("dwFlags", ctypes.c_uint),
                ("dwCommonButtons", ctypes.c_uint),
                ("pszWindowTitle", wintypes.LPCWSTR),
                ("hMainIcon", wintypes.LPCWSTR),
                ("pszMainInstruction", wintypes.LPCWSTR),
                ("pszContent", wintypes.LPCWSTR),
                ("cButtons", ctypes.c_uint),
                ("pButtons", ctypes.POINTER(TASKDIALOG_BUTTON)),
                ("nDefaultButton", ctypes.c_int),
                ("cRadioButtons", ctypes.c_uint),
                ("pRadioButtons", ctypes.POINTER(TASKDIALOG_BUTTON)),
                ("nDefaultRadioButton", ctypes.c_int),
                ("pszVerificationText", wintypes.LPCWSTR),
                ("pszExpandedInformation", wintypes.LPCWSTR),
                ("pszExpandedControlText", wintypes.LPCWSTR),
                ("pszCollapsedControlText", wintypes.LPCWSTR),
                ("hFooterIcon", wintypes.LPCWSTR),
                ("pszFooter", wintypes.LPCWSTR),
                ("pfCallback", ctypes.c_void_p),
                ("lpCallbackData", ctypes.c_ssize_t),
                ("cxWidth", ctypes.c_uint),
            ]

        _log("showing TaskDialog server manager")
        while True:
            config = TASKDIALOGCONFIG()
            config.cbSize = ctypes.sizeof(TASKDIALOGCONFIG)
            config.dwFlags = TDF_USE_COMMAND_LINKS | TDF_ALLOW_DIALOG_CANCELLATION
            config.pszWindowTitle = "Kirinuki サーバーマネージャー"
            config.hMainIcon = ctypes.cast(TD_INFORMATION_ICON, wintypes.LPCWSTR)
            config.pszMainInstruction = "Kirinuki サーバーが正常に実行中です"
            config.pszContent = f"サーバーアドレス: http://{HOST}:{PORT}\nご希望の操作を選択してください。"
            config.cButtons = len(buttons)
            config.pButtons = buttons

            pnButton = ctypes.c_int()

            res = ctypes.windll.comctl32.TaskDialogIndirect(
                ctypes.byref(config), ctypes.byref(pnButton), None, None
            )

            if res != 0:
                _log(f"TaskDialogIndirect failed with return code {res}")
                return False

            btn_id = pnButton.value
            if btn_id == 101:
                webbrowser.open(f"http://{HOST}:{PORT}")
            elif btn_id == 102:
                _open_folder(APP_ROOT)
            elif btn_id == 103:
                _open_folder(data_dir)
            elif btn_id == 104:
                _show_cookie_guide_dialog(data_dir)
            elif btn_id in (105, 2):  # 105: 終了ボタン, 2: IDCANCEL (閉じるボタン)
                break

        return True
    except Exception as e:
        _log(f"TaskDialog manager failed: {e}")
        return False


def _show_server_manager(data_dir: Path) -> None:
    """サーバーマネージャーUIを表示する（Tkinter -> TaskDialogIndirect -> フォールバックの順に試行）"""
    if _try_show_tkinter_manager(data_dir):
        return

    if _try_show_taskdialog_manager(data_dir):
        return

    # フォールバック
    _log("press enter to shutdown (fallback manager)")
    try:
        input()
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

    _log("server ready, opening browser and data directory")
    webbrowser.open(f"http://{HOST}:{PORT}")
    data_dir = cfg.get_data_dir()
    _open_folder(data_dir)

    # 常駐GUIマネージャーを表示
    _show_server_manager(data_dir)

    _log("user requested shutdown. exiting process.")
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        _show_error("予期しないエラーが発生しました:\n\n" + traceback.format_exc())

