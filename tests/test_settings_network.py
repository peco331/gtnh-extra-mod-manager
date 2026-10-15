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
        # 设置页新增的资源包/光影目录输入框（未显式传时按"留空=自动推断"）
        app.pack_resourcepack_entry = _Entry("")
        app.pack_shader_entry = _Entry("")
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

    def test_pack_dirs_saved_with_other_fields(self):
        """资源包/光影目录与 mods 目录同批提交，留空表示自动推断。"""
        app = self._app(mode="direct")
        app.pack_resourcepack_entry = _Entry(str(self.tmp / "rp"))
        app.pack_shader_entry = _Entry("")

        with mock.patch.object(gui.messagebox, "showinfo"):
            app.save_settings()

        self.assertEqual(self.cfg.pack_folders["resourcepack"], str(self.tmp / "rp"))
        self.assertEqual(self.cfg.pack_folders["shader"], "")

    def test_invalid_numeric_setting_does_not_persist_pack_dirs(self):
        app = self._app(interval="bad")
        app.pack_resourcepack_entry = _Entry(str(self.tmp / "rp"))
        app.pack_shader_entry = _Entry(str(self.tmp / "sp"))

        with mock.patch.object(gui.messagebox, "showerror"):
            app.save_settings()

        self.assertEqual(self.cfg.pack_folders, {"resourcepack": "", "shader": ""})

    def test_save_failure_does_not_apply_unsaved_settings(self):
        app = self._app()
        with mock.patch.object(self.cfg, 'save', side_effect=OSError('disk full')), \
                mock.patch.object(gui.messagebox, 'showerror') as error:
            app.save_settings()
        self.assertEqual(self.cfg.github_token, '')
        app.refresh_all.assert_not_called()
        error.assert_called_once()

    def test_busy_save_does_not_change_active_configuration(self):
        app = self._app()
        app.busy = True
        with mock.patch.object(gui.messagebox, 'showinfo'):
            app.save_settings()
        self.assertEqual(self.cfg.github_token, '')

    def test_save_and_test_persists_only_network_fields_before_testing(self):
        app = self._app(interval='invalid', mode='direct')
        app.busy = False
        app.test_network_settings = mock.Mock()
        app.save_and_test_network_settings()
        self.assertEqual(Config(self.cfg.data_dir).github_token, 'new-token')
        self.assertEqual(self.cfg.proxy, {'host': '', 'port': 0})
        self.assertIsNone(self.cfg.client_mods_dir)  # 无关目录输入不顺带保存
        self.assertEqual(self.cfg.check_interval_hours, 6)
        app.test_network_settings.assert_called_once()

    def test_network_save_failure_does_not_start_test_or_apply_token(self):
        app = self._app()
        app.busy = False
        app.test_network_settings = mock.Mock()
        with mock.patch.object(self.cfg, 'save', side_effect=OSError('disk full')), \
                mock.patch.object(gui.messagebox, 'showerror'):
            app.save_and_test_network_settings()
        self.assertEqual(self.cfg.github_token, '')
        app.test_network_settings.assert_not_called()

    def test_nan_and_zero_backup_count_rejected(self):
        for interval, keep in (('nan', '3'), ('inf', '3'), ('6', '0')):
            with self.subTest(interval=interval, keep=keep), \
                    mock.patch.object(gui.messagebox, 'showerror'):
                self._app(interval=interval, backup=keep).save_settings()
            self.assertFalse(self.cfg.path.exists())


class TestNetworkSettingsGui(unittest.TestCase):
    def _test_result(self, *, saved='', entered='new-token', remaining=4999):
        app = object.__new__(gui.GuiApp)
        app.cfg = mock.Mock(github_token=saved, proxy=None)
        app.token_entry = _Entry(entered)
        app.proxy_mode = _Mode('system')
        app.proxy_entry = _Entry('')
        app.busy = False
        app._set_busy = mock.Mock()
        app._log = mock.Mock()
        app._run_async = lambda job, on_done: on_done(job())
        payload = {'resources': {'core': {'remaining': remaining, 'limit': 5000}}}
        with mock.patch.object(net, 'http_get', return_value=json.dumps(payload)), \
                mock.patch.object(gui.messagebox, 'showinfo') as info, \
                mock.patch.object(gui.messagebox, 'showwarning') as warning:
            app.test_network_settings()
        return info, warning

    def test_unsaved_token_test_explicitly_requires_save(self):
        info, warning = self._test_result()
        self.assertIn('尚未保存', info.call_args.args[1])
        warning.assert_not_called()

    def test_saved_token_test_reports_saved_state(self):
        info, _warning = self._test_result(saved='new-token')
        self.assertIn('已保存', info.call_args.args[1])
        self.assertNotIn('尚未保存', info.call_args.args[1])

    def test_exhausted_rate_limit_is_warning(self):
        info, warning = self._test_result(remaining=0)
        warning.assert_called_once()
        self.assertIn('耗尽', warning.call_args.args[0])
        info.assert_not_called()
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
