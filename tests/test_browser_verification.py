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

if __name__ == "__main__":
    unittest.main()
