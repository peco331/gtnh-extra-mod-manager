import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtnhmod.config import Config
from gtnhmod import wiki

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
