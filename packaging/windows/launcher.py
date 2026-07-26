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

        # コマンドリンク（詳細説明付き）用のボタン定義
        buttons_cmd_links = (TASKDIALOG_BUTTON * 5)(
            TASKDIALOG_BUTTON(101, f"Web画面（ブラウザ）を開く\nhttp://{HOST}:{PORT} を既定のブラウザで開きます"),
            TASKDIALOG_BUTTON(102, "インストールフォルダを開く\nプログラム本体や関連ツールがあるフォルダを開きます"),
            TASKDIALOG_BUTTON(103, "データフォルダを開く\ncookies.txt やログファイルが保存されるフォルダを開きます"),
            TASKDIALOG_BUTTON(104, "Cookieの手動保存手順を見る\n年齢制限・メンバー限定動画ダウンロード用の設定方法を表示します"),
            TASKDIALOG_BUTTON(105, "Kirinuki サーバーを終了する\nWebサーバーを停止してアプリケーションを終了します"),
        )

        # 標準ボタン（Comctl32 v6なしのフォールバック用）のボタン定義
        buttons_standard = (TASKDIALOG_BUTTON * 5)(
            TASKDIALOG_BUTTON(101, "Web画面（ブラウザ）を開く"),
            TASKDIALOG_BUTTON(102, "インストールフォルダを開く"),
            TASKDIALOG_BUTTON(103, "データフォルダを開く"),
            TASKDIALOG_BUTTON(104, "Cookieの手動保存手順を見る"),
            TASKDIALOG_BUTTON(105, "Kirinuki サーバーを終了する"),
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

        # 最初にコマンドリンク形式を試す。失敗した場合は標準ボタン形式で試す
        attempts = [
            (TDF_USE_COMMAND_LINKS | TDF_ALLOW_DIALOG_CANCELLATION, buttons_cmd_links),
            (TDF_ALLOW_DIALOG_CANCELLATION, buttons_standard),
        ]

        for flags, btns in attempts:
            while True:
                config = TASKDIALOGCONFIG()
                config.cbSize = ctypes.sizeof(TASKDIALOGCONFIG)
                config.dwFlags = flags
                config.pszWindowTitle = "Kirinuki サーバーマネージャー"
                config.hMainIcon = ctypes.cast(TD_INFORMATION_ICON, wintypes.LPCWSTR)
                config.pszMainInstruction = "Kirinuki サーバーが正常に実行中です"
                config.pszContent = f"サーバーアドレス: http://{HOST}:{PORT}\nご希望の操作を選択してください。"
                config.cButtons = len(btns)
                config.pButtons = btns

                pnButton = ctypes.c_int()

                res = ctypes.windll.comctl32.TaskDialogIndirect(
                    ctypes.byref(config), ctypes.byref(pnButton), None, None
                )

                if res != 0:
                    _log(f"TaskDialogIndirect with flags {flags:#x} failed with return code {res}")
                    break

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
                    return True

        return False
    except Exception as e:
        _log(f"TaskDialog manager failed: {e}")
        return False


def _try_show_native_win32_manager(data_dir: Path) -> bool:
    """Win32 API (user32.dll) を直接使用し、すべてのWindows環境で確実に動作する専用GUIウィンドウを表示する"""
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32

    # Win32 関数シグネチャの定義
    WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    # Win32 定数
    WS_CAPTION = 0x00C00000
    WS_SYSMENU = 0x00080000
    WS_MINIMIZEBOX = 0x00020000
    WS_VISIBLE = 0x10000000
    WS_CHILD = 0x40000000
    BS_PUSHBUTTON = 0x00000000
    SS_LEFT = 0x00000000
    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_SETFONT = 0x0030
    COLOR_WINDOW = 5
    DEFAULT_GUI_FONT = 17

    # ボタンID
    ID_BROWSER = 101
    ID_INSTALL_DIR = 102
    ID_DATA_DIR = 103
    ID_COOKIE_GUIDE = 104
    ID_SHUTDOWN = 105

    try:
        hinstance = kernel32.GetModuleHandleW(None)
        hfont = gdi32.GetStockObject(DEFAULT_GUI_FONT)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_COMMAND:
                cmd_id = wparam & 0xFFFF
                if cmd_id == ID_BROWSER:
                    webbrowser.open(f"http://{HOST}:{PORT}")
                elif cmd_id == ID_INSTALL_DIR:
                    _open_folder(APP_ROOT)
                elif cmd_id == ID_DATA_DIR:
                    _open_folder(data_dir)
                elif cmd_id == ID_COOKIE_GUIDE:
                    _show_cookie_guide_dialog(data_dir)
                elif cmd_id == ID_SHUTDOWN:
                    user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wndproc_cb = WNDPROC(wnd_proc)

        class_name = "KirinukiServerManagerWindow"
        wndclass = WNDCLASSEXW()
        wndclass.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wndclass.style = 0
        wndclass.lpfnWndProc = wndproc_cb
        wndclass.cbClsExtra = 0
        wndclass.cbWndExtra = 0
        wndclass.hInstance = hinstance
        wndclass.hIcon = user32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))  # IDI_APPLICATION
        wndclass.hCursor = user32.LoadCursorW(None, ctypes.cast(32512, wintypes.LPCWSTR))  # IDC_ARROW
        wndclass.hbrBackground = COLOR_WINDOW + 1
        wndclass.lpszMenuName = None
        wndclass.lpszClassName = class_name
        wndclass.hIconSm = None

        user32.RegisterClassExW(ctypes.byref(wndclass))

        # ウィンドウサイズと画面中央配置
        win_width, win_height = 460, 360
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        x = max(0, (screen_w - win_width) // 2)
        y = max(0, (screen_h - win_height) // 2)

        style = WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX | WS_VISIBLE
        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "Kirinuki サーバーマネージャー",
            style,
            x, y, win_width, win_height,
            None, None, hinstance, None
        )

        if not hwnd:
            return False

        # コントロール作成ヘルパー
        def create_control(class_name_ctrl, text, style_ctrl, x_c, y_c, w_c, h_c, ctrl_id=0):
            h_ctrl = user32.CreateWindowExW(
                0,
                class_name_ctrl,
                text,
                WS_CHILD | WS_VISIBLE | style_ctrl,
                x_c, y_c, w_c, h_c,
                hwnd,
                ctypes.c_void_p(ctrl_id),
                hinstance,
                None
            )
            if hfont:
                user32.SendMessageW(h_ctrl, WM_SETFONT, hfont, 1)
            return h_ctrl

        # ヘッダーとステータス表示
        create_control("STATIC", "🟢 Kirinuki サーバー実行中", SS_LEFT, 25, 18, 400, 22)
        create_control("STATIC", f"サーバーアドレス: http://{HOST}:{PORT}", SS_LEFT, 25, 42, 400, 20)

        # 操作ボタン
        buttons_info = [
            (ID_BROWSER, "🌐  Web画面（ブラウザ）を開く", 25, 75, 395, 38),
            (ID_INSTALL_DIR, "📁  インストールフォルダを開く", 25, 122, 395, 38),
            (ID_DATA_DIR, "📂  データフォルダを開く (cookies.txt 保存先)", 25, 169, 395, 38),
            (ID_COOKIE_GUIDE, "🍪  Cookieの手動保存手順を見る", 25, 216, 395, 38),
            (ID_SHUTDOWN, "❌  Kirinuki サーバーを終了する", 25, 263, 395, 38),
        ]

        for ctrl_id, label_text, bx, by, bw, bh in buttons_info:
            create_control("BUTTON", label_text, BS_PUSHBUTTON, bx, by, bw, bh, ctrl_id)

        _log("showing native Win32 server manager window")

        # メッセージループ
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        return True
    except Exception as e:
        _log(f"Native Win32 manager failed: {e}")
        return False


def _show_server_manager(data_dir: Path) -> None:
    """サーバーマネージャーUIを表示する（Tkinter -> Native Win32 -> TaskDialogIndirect -> フォールバックの順に試行）"""
    if _try_show_tkinter_manager(data_dir):
        return

    if _try_show_native_win32_manager(data_dir):
        return

    if _try_show_taskdialog_manager(data_dir):
        return

    # フォールバック
    _log("showing fallback server manager (MessageBox / event wait)")
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Kirinuki サーバーが正常に起動し、実行中です。\n\n"
            f"アドレス: http://{HOST}:{PORT}\n"
            f"データ保存先: {data_dir}\n\n"
            f"[ OK ] をクリックすると Kirinuki サーバーを終了します。",
            "Kirinuki サーバーマネージャー",
            0x40,  # MB_ICONINFORMATION
        )
    else:
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                input("Press Enter to shutdown Kirinuki server...\n")
            else:
                stop_event = threading.Event()
                stop_event.wait()
        except (RuntimeError, EOFError, OSError, KeyboardInterrupt):
            stop_event = threading.Event()
            try:
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
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        _show_error("予期しないエラーが発生しました:\n\n" + traceback.format_exc())

