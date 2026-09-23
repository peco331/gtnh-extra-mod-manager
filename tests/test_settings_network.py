"""设置保存和代理模式的回归测试。"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gtnhmod import gui, net
from gtnhmod.config import Config


class _Entry:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class _Mode(_Entry):
    pass


class TestNetworkProxyModes(unittest.TestCase):
    def test_direct_proxy_disables_environment_proxy_discovery(self):
        """直接连接必须显式安装空代理处理器，不能回退到系统代理。"""
        with mock.patch.object(net.urllib.request, "build_opener") as build:
            net._opener_for({"host": "", "port": 0})

        handler = build.call_args.args[0]
        self.assertIsInstance(handler, net.urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})


class TestSettingsSave(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gtnh_settings_net_"))
        self.client = self.tmp / "client" / "mods"
        self.server = self.tmp / "server" / "mods"
        self.client.mkdir(parents=True)
        self.server.mkdir(parents=True)
        self.cfg = Config(self.tmp / "data")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _app(self, *, interval="6", backup="3", mode="system", proxy=""):
        app = object.__new__(gui.GuiApp)
        app.cfg = self.cfg
        app.client_entry = _Entry(str(self.client))
        app.server_entry = _Entry(str(self.server))
        app.token_entry = _Entry("new-token")
        app.proxy_mode = _Mode(mode)
        app.proxy_entry = _Entry(proxy)
        app.interval_entry = _Entry(interval)
        app.backup_entry = _Entry(backup)
        app.gtnh_entry = _Entry("2.9.0")
        app.wiki_cookie_entry = _Entry("")
        app.wiki_ua_entry = _Entry("")
        app._log = mock.Mock()
        app.refresh_all = mock.Mock()
        return app

    def test_invalid_numeric_setting_does_not_persist_directory_or_token(self):
        app = self._app(interval="not-a-number")

        with mock.patch.object(gui.messagebox, "showerror"):
            app.save_settings()

        self.assertEqual(self.cfg.data["mods_folders"], {"client": "", "server": ""})
        self.assertEqual(self.cfg.github_token, "")
        self.assertFalse(self.cfg.path.exists())

    def test_valid_save_commits_all_fields_together(self):
        app = self._app(mode="direct")

        with mock.patch.object(gui.messagebox, "showinfo"):
            app.save_settings()

        self.assertEqual(self.cfg.client_mods_dir, self.client)
        self.assertEqual(self.cfg.server_mods_dir, self.server)
        self.assertEqual(self.cfg.github_token, "new-token")
        self.assertEqual(self.cfg.proxy, {"host": "", "port": 0})
        self.assertTrue(self.cfg.path.exists())


class TestNetworkSettingsGui(unittest.TestCase):
    def test_network_test_parses_http_get_text_response(self):
        app = object.__new__(gui.GuiApp)
        app.token_entry = _Entry("")
        app.proxy_mode = _Mode("system")
        app.proxy_entry = _Entry("")
        app.cfg = mock.Mock(proxy=None)
        app.busy = False
        app._set_busy = mock.Mock()
        app._log = mock.Mock()
        seen = {}

        def run_now(job, on_done=None):
            seen["result"] = job()
            seen["done"] = on_done

        app._run_async = run_now
        payload = {"resources": {"core": {"remaining": 42, "limit": 60}}}
        with mock.patch.object(net, "http_get", return_value=json.dumps(payload)):
            app.test_network_settings()

        self.assertEqual(seen["result"], payload["resources"]["core"])


if __name__ == "__main__":
    unittest.main()
