import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtnhmod import net
from gtnhmod.config import Config
from gtnhmod import wiki

REAL_WIKITEXT = "{{可添加MOD表格行|X}}"


def _profile():
    return wiki.WikiProfile(page="可添加MOD", validate=wiki._validate_wikitext,
                            cache_name="wiki_wikitext.txt")


class TestPollingThroughChallenge(unittest.TestCase):
    """轮询拿到 200 但内容不是正文（Cloudflare 挑战页）时必须继续等。

    回归：改成 profile.validate 后，任何一次校验失败都会抛 HttpError
    穿透轮询循环 → 浏览器窗口被杀、整个交互验证流程中止。
    """

    def _run_browser_poll(self, responses, timeout_seconds=30, startup_collision=False,
                          fallback_exit=None):
        """打桩浏览器验证流程，responses 依次作为每轮 CDP fetch 的返回值。

        返回 (fetch_wikitext_via_browser 的返回值/异常, cfg)。
        """
        import shutil
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_browser_test_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        cfg = Config(tmp)

        saved_profile = cfg.data_dir / "cache" / "browser_profile"
        saved_profile.mkdir(parents=True)
        marker = saved_profile / "saved-verification"
        marker.write_text("keep", encoding="utf-8")
        browser_proc = MagicMock()
        browser_proc.poll.return_value = fallback_exit
        forwarded_proc = MagicMock()
        forwarded_proc.poll.return_value = 21
        targets = [{"type": "page", "url": "x", "webSocketDebuggerUrl": "ws://x"}]
        launches = []

        def launch(cmd):
            launches.append(list(cmd))
            return forwarded_proc if startup_collision and len(launches) == 1 else browser_proc

        with patch.object(wiki, "_find_system_browser", return_value=Path("C:/browser.exe")), \
             patch("subprocess.Popen") as popen, \
             patch.object(wiki, "_get_free_port", return_value=9333), \
             patch.object(wiki, "_read_local_cdp_json",
                          side_effect=[OSError("profile in use"),
                                       OSError("startup failed") if fallback_exit is not None else targets]
                          if startup_collision else None, return_value=targets), \
             patch.object(wiki, "_select_wiki_cdp_target",
                          return_value={"webSocketDebuggerUrl": "ws://x"}), \
             patch("websocket.create_connection", return_value=MagicMock()), \
             patch.object(wiki, "_cdp_eval", side_effect=responses), \
             patch.object(wiki.time, "sleep"):
            popen.side_effect = launch
            try:
                got, err = wiki.fetch_wikitext_via_browser(
                    cfg, timeout_seconds=timeout_seconds, profile=_profile()), None
            except Exception as e:
                got, err = None, e
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.launches = launches
        self.forwarded_proc = forwarded_proc
        return got, err, cfg

    def test_busy_profile_retries_in_independent_session(self):
        got, err, cfg = self._run_browser_poll(
            [{"status": 200, "text": REAL_WIKITEXT}], startup_collision=True)
        self.assertIsNone(err)
        self.assertEqual(got, REAL_WIKITEXT)
        self.assertEqual(len(self.launches), 2)
        profiles = [next(arg.split("=", 1)[1] for arg in cmd
                         if arg.startswith("--user-data-dir=")) for cmd in self.launches]
        self.assertNotEqual(profiles[0], profiles[1])
        self.assertFalse(Path(profiles[1]).exists())
        self.forwarded_proc.terminate.assert_not_called()

    def test_failed_independent_session_reports_exit_without_more_retries(self):
        got, err, _cfg = self._run_browser_poll(
            [], startup_collision=True, fallback_exit=21)
        self.assertIsNone(got)
        self.assertIsInstance(err, net.HttpError)
        self.assertIn("退出码 21", str(err))
        self.assertEqual(len(self.launches), 2)
        profile_arg = next(arg for arg in self.launches[1]
                           if arg.startswith("--user-data-dir="))
        self.assertFalse(Path(profile_arg.split("=", 1)[1]).exists())

    def test_challenge_page_keeps_polling_until_real_body(self):
        responses = [
            {"status": 200, "text": "<html>Just a moment...</html>"},
            {"status": 200, "text": REAL_WIKITEXT},
        ]
        got, err, cfg = self._run_browser_poll(responses)
        self.assertIsNone(err)
        self.assertEqual(got, REAL_WIKITEXT)
        cache = cfg.data_dir / "cache" / "wiki_wikitext.txt"
        self.assertEqual(cache.read_text(encoding="utf-8"), REAL_WIKITEXT)

    def test_permanent_challenge_times_out_instead_of_erroring_early(self):
        calls = []

        def always_challenge(_ws, _js):
            calls.append(1)
            return {"status": 200, "text": "Just a moment..."}

        got, err, _cfg = self._run_browser_poll(always_challenge, timeout_seconds=1)
        self.assertIsNone(got)
        self.assertIsInstance(err, net.HttpError)
        self.assertIn("超时", str(err))
        self.assertGreater(len(calls), 1)   # 确实轮询多次而非首轮即败


class TestBrowserVerification(unittest.TestCase):
    def test_find_system_browser(self):
        from gtnhmod.wiki import _find_system_browser
        browser = _find_system_browser()
        self.assertTrue(browser is None or Path(browser).exists())

    def test_cdp_eval_skips_unrelated_events(self):
        """CDP 事件可能先于命令响应到达，读取器必须继续等目标 id。"""
        class FakeWebSocket:
            def __init__(self):
                self.request_id = None
                self.messages = [
                    {"method": "Runtime.executionContextCreated"},
                    {"result": {"result": {"value": {"status": 200}}}},
                ]

            def settimeout(self, _seconds):
                return None

            def send(self, payload):
                self.request_id = json.loads(payload)["id"]
                self.messages[-1]["id"] = self.request_id

            def recv(self):
                return json.dumps(self.messages.pop(0))

        result = wiki._cdp_eval(FakeWebSocket(), "1 + 1")
        self.assertEqual(result, {"status": 200})

    def test_select_cdp_target_requires_the_requested_wiki_page(self):
        tabs = [
            {"type": "page", "url": "https://example.com/", "webSocketDebuggerUrl": "wrong"},
            {"type": "page", "url": "https://gtnh.huijiwiki.com/wiki/%E5%8F%AF%E6%B7%BB%E5%8A%A0MOD",
             "webSocketDebuggerUrl": "right"},
        ]
        target = wiki._select_wiki_cdp_target(
            tabs, "https://gtnh.huijiwiki.com/wiki/%E5%8F%AF%E6%B7%BB%E5%8A%A0MOD")
        self.assertEqual(target["webSocketDebuggerUrl"], "right")

    def test_local_cdp_requests_bypass_system_proxy(self):
        response = MagicMock()
        response.read.return_value = b"[]"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(wiki._read_local_cdp_json(9222), [])
        handlers = build.call_args.args
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].proxies, {})

if __name__ == "__main__":
    unittest.main()
