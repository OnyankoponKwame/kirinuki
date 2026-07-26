"""Verifies the staged cookie fallback flow in web/pipeline.py and kirinuki.py.

Expected flow when no cookies.txt exists yet: try yt-dlp with no cookie args at
all first, and only fall back to `--cookies-from-browser chrome` (which can pop a
DPAPI decryption dialog) if that attempt fails with a bot/sign-in/cookie error.
Videos that don't need cookies should never pay that cost.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR / "web"))

import config  # noqa: E402
import pipeline  # noqa: E402
import kirinuki  # noqa: E402


class FakePopen:
    """Stand-in for subprocess.Popen that yields canned stdout lines."""

    def __init__(self, cmd, stdout_lines=(), returncode=0, **kwargs):
        self.cmd = cmd
        self.stdout = iter(list(stdout_lines))
        self.returncode = returncode

    def wait(self):
        return self.returncode


def make_fake_popen(calls):
    """calls: list of (stdout_lines, returncode), one per expected Popen invocation."""
    calls_iter = iter(calls)
    captured_cmds = []

    def fake_popen(cmd, **kwargs):
        captured_cmds.append(cmd)
        stdout_lines, returncode = next(calls_iter)
        return FakePopen(cmd, stdout_lines=stdout_lines, returncode=returncode)

    return fake_popen, captured_cmds


def assert_chrome_cookie_args_well_formed(testcase, cmd, cookies_path):
    # --cookies-from-browser chrome --cookies <path> がこの順で連続していること
    # (順序が崩れると、パスがURLとして、URLがクッキーパスとして渡ってしまう)
    i = cmd.index("--cookies-from-browser")
    testcase.assertEqual(cmd[i:i + 4], ["--cookies-from-browser", "chrome", "--cookies", str(cookies_path)])


class FindCookiesFileTest(unittest.TestCase):
    """config.find_cookies_file() — backward/suffix match for browser-exported filenames."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)

    def test_returns_none_when_nothing_present(self):
        self.assertIsNone(config.find_cookies_file(self.data_dir))

    def test_exact_name_is_found(self):
        path = self.data_dir / "cookies.txt"
        path.write_text("x")
        self.assertEqual(config.find_cookies_file(self.data_dir), path)

    def test_suffix_matched_name_is_found(self):
        # "Get cookies.txt LOCALLY" 等の拡張機能が付けるドメイン付きファイル名
        path = self.data_dir / "127.0.0.1_cookies.txt"
        path.write_text("x")
        self.assertEqual(config.find_cookies_file(self.data_dir), path)

    def test_exact_name_preferred_over_suffix_match(self):
        exact = self.data_dir / "cookies.txt"
        exact.write_text("x")
        (self.data_dir / "www.youtube.com_cookies.txt").write_text("x")
        self.assertEqual(config.find_cookies_file(self.data_dir), exact)

    def test_unrelated_files_are_ignored(self):
        (self.data_dir / "config.json").write_text("{}")
        (self.data_dir / "cookies.txt.bak").write_text("x")
        self.assertIsNone(config.find_cookies_file(self.data_dir))

    def test_most_recently_modified_suffix_match_wins(self):
        older = self.data_dir / "127.0.0.1_cookies.txt"
        newer = self.data_dir / "www.youtube.com_cookies.txt"
        older.write_text("x")
        os.utime(older, (1_000_000_000, 1_000_000_000))
        newer.write_text("x")
        os.utime(newer, (2_000_000_000, 2_000_000_000))
        self.assertEqual(config.find_cookies_file(self.data_dir), newer)


class PipelineDownloadVideoCookieFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        os.environ["KIRINUKI_DATA_DIR"] = str(self.data_dir)
        self.addCleanup(os.environ.pop, "KIRINUKI_DATA_DIR", None)

        self.output_dir = self.data_dir / "downloads"
        self.output_dir.mkdir(parents=True)
        self.video_path = self.output_dir / "video_abc123.mp4"
        self.video_path.write_bytes(b"fake")
        self.cookies_path = self.data_dir / "cookies.txt"

    def test_no_cookies_file_tries_without_cookies_first(self):
        fake_popen, cmds = make_fake_popen([([str(self.video_path)], 0)])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            video_path, _ = pipeline.download_video(
                "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
            )

        self.assertEqual(len(cmds), 1)
        self.assertNotIn("--cookies", cmds[0])
        self.assertNotIn("--cookies-from-browser", cmds[0])
        self.assertEqual(video_path, self.video_path)

    def test_no_cookies_file_retries_with_chrome_on_bot_error(self):
        fake_popen, cmds = make_fake_popen([
            (["ERROR: Sign in to confirm you're not a bot"], 1),
            ([str(self.video_path)], 0),
        ])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            video_path, _ = pipeline.download_video(
                "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
            )

        self.assertEqual(len(cmds), 2)
        first_cmd, second_cmd = cmds
        self.assertNotIn("--cookies", first_cmd)
        self.assertNotIn("--cookies-from-browser", first_cmd)

        assert_chrome_cookie_args_well_formed(self, second_cmd, self.cookies_path)
        self.assertEqual(video_path, self.video_path)

    def test_no_cookies_file_does_not_retry_on_unrelated_error(self):
        # ボット/クッキー関連キーワードを含まない失敗ではブラウザ抽出にフォールバックしない
        fake_popen, cmds = make_fake_popen([(["ERROR: network unreachable"], 1)])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            with self.assertRaises(RuntimeError):
                pipeline.download_video(
                    "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
                )

        self.assertEqual(len(cmds), 1)

    def test_cached_cookies_file_used_first_and_refreshed_on_failure(self):
        # 既存のcookies.txtがある場合は最初からそれを使い、失敗時のみブラウザ再取得にフォールバックする（回帰確認）
        self.cookies_path.write_text("cached")
        fake_popen, cmds = make_fake_popen([
            (["ERROR: cookie database could not copy"], 1),
            ([str(self.video_path)], 0),
        ])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            pipeline.download_video(
                "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
            )

        first_cmd, second_cmd = cmds
        self.assertIn("--cookies", first_cmd)
        self.assertNotIn("--cookies-from-browser", first_cmd)
        self.assertIn(str(self.cookies_path), first_cmd)
        assert_chrome_cookie_args_well_formed(self, second_cmd, self.cookies_path)

    def test_suffix_matched_cookies_file_used_first(self):
        # "127.0.0.1_cookies.txt" のような拡張機能出力ファイルも、初回からクッキーとして使われる
        suffix_cookies_path = self.data_dir / "127.0.0.1_cookies.txt"
        suffix_cookies_path.write_text("cached")
        fake_popen, cmds = make_fake_popen([([str(self.video_path)], 0)])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            pipeline.download_video(
                "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
            )

        self.assertEqual(len(cmds), 1)
        self.assertIn("--cookies", cmds[0])
        self.assertIn(str(suffix_cookies_path), cmds[0])
        self.assertNotIn("--cookies-from-browser", cmds[0])


class PipelineDownloadChatOnlyCookieFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        os.environ["KIRINUKI_DATA_DIR"] = str(self.data_dir)
        self.addCleanup(os.environ.pop, "KIRINUKI_DATA_DIR", None)

        self.output_dir = self.data_dir / "downloads"
        self.output_dir.mkdir(parents=True)
        self.chat_path = self.output_dir / "video_abc123.live_chat.json"
        # "replayChatItemAction" を含まない = slim_live_chat が no-op で通過する
        self.chat_path.write_text("{}\n")
        self.cookies_path = self.data_dir / "cookies.txt"

    def test_no_cookies_file_retries_with_chrome_on_bot_error(self):
        fake_popen, cmds = make_fake_popen([
            (["ERROR: Sign in to confirm you're not a bot"], 1),
            ([], 0),
        ])

        with patch("pipeline.subprocess.Popen", side_effect=fake_popen):
            chat_path = pipeline.download_chat_only(
                "https://example.com/watch?v=abc123", self.output_dir, log=lambda _: None
            )

        self.assertEqual(len(cmds), 2)
        first_cmd, second_cmd = cmds
        self.assertNotIn("--cookies", first_cmd)
        self.assertNotIn("--cookies-from-browser", first_cmd)
        assert_chrome_cookie_args_well_formed(self, second_cmd, self.cookies_path)
        self.assertEqual(chat_path, self.chat_path)


class KirinukiDownloadVideoCookieFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fake_project_dir = Path(tmp.name)
        # kirinuki.download_video derives cookies_path from Path(__file__).parent,
        # so point the module's __file__ at our throwaway dir instead of touching
        # the real project root's cookies.txt.
        patcher = patch.object(kirinuki, "__file__", str(self.fake_project_dir / "kirinuki.py"))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.output_dir = self.fake_project_dir / "downloads"
        self.output_dir.mkdir(parents=True)
        self.video_path = self.output_dir / "abc123.mp4"
        self.video_path.write_bytes(b"fake")
        self.cookies_path = self.fake_project_dir / "cookies.txt"

    def _completed(self, stdout="", stderr="", returncode=0):
        from subprocess import CompletedProcess
        return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_no_cookies_file_tries_without_cookies_first(self):
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return self._completed(stdout=str(self.video_path) + "\n", returncode=0)

        with patch("kirinuki.subprocess.run", side_effect=fake_run):
            video_path, _ = kirinuki.download_video(
                "https://example.com/watch?v=abc123", self.output_dir
            )

        self.assertEqual(len(cmds), 1)
        self.assertNotIn("--cookies", cmds[0])
        self.assertNotIn("--cookies-from-browser", cmds[0])
        self.assertEqual(video_path, self.video_path)

    def test_no_cookies_file_retries_with_chrome_on_bot_error(self):
        responses = [
            self._completed(stderr="ERROR: Sign in to confirm you're not a bot", returncode=1),
            self._completed(stdout=str(self.video_path) + "\n", returncode=0),
        ]
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return responses[len(cmds) - 1]

        with patch("kirinuki.subprocess.run", side_effect=fake_run):
            video_path, _ = kirinuki.download_video(
                "https://example.com/watch?v=abc123", self.output_dir
            )

        self.assertEqual(len(cmds), 2)
        first_cmd, second_cmd = cmds
        self.assertNotIn("--cookies", first_cmd)
        self.assertNotIn("--cookies-from-browser", first_cmd)
        assert_chrome_cookie_args_well_formed(self, second_cmd, self.cookies_path)
        self.assertEqual(video_path, self.video_path)

    def test_suffix_matched_cookies_file_used_first(self):
        suffix_cookies_path = self.fake_project_dir / "127.0.0.1_cookies.txt"
        suffix_cookies_path.write_text("cached")
        cmds = []

        def fake_run(cmd, **kwargs):
            cmds.append(cmd)
            return self._completed(stdout=str(self.video_path) + "\n", returncode=0)

        with patch("kirinuki.subprocess.run", side_effect=fake_run):
            kirinuki.download_video("https://example.com/watch?v=abc123", self.output_dir)

        self.assertEqual(len(cmds), 1)
        self.assertIn("--cookies", cmds[0])
        self.assertIn(str(suffix_cookies_path), cmds[0])
        self.assertNotIn("--cookies-from-browser", cmds[0])


if __name__ == "__main__":
    unittest.main()
