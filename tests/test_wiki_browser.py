"""网页桥接只把已知条目的意图交回原有安装/管理流程。"""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gtnhmod import gui, wiki, wiki_browser as browser
from gtnhmod.config import Config

PAGE = 'https://gtnh.huijiwiki.com/wiki/%E5%8F%AF%E6%B7%BB%E5%8A%A0MOD'
RAW = '''== 星门规则模组 ==
=== 功能增强 ===
{{可添加MOD表格行
|模组英文名=Demo
|模组中文名=演示模组
|运行环境=客户端
|简述=用于隔离测试
|相关地址=[https://example.invalid/demo 项目页]
}}
'''
HTML = '''<!doctype html><meta charset="utf-8"><title>可添加MOD：隔离测试</title>
<style>body{font:15px sans-serif;margin:28px}.extra-mod{border:2px solid #9aa0a6;padding:16px}
h2{color:#10679a}</style><h1>可添加MOD</h1><h2>星门规则模组</h2><h3>功能增强</h3>
<div id="mw-content-text"><div class="extra-mod">
<div class="extra-mod-aside-name"><div>Demo</div><div>演示模组</div></div>
<div class="extra-mod-body"><p>用于验证 Wiki 网页与本地管理的连接。</p>
<div class="extra-mod-links"><a href="https://example.invalid/demo">项目页</a></div>
</div></div></div>'''


def entry(mid='demo'):
    return {'id': mid, 'name_en': 'Demo', 'name_cn': '演示模组',
            'group': '星门规则', 'category': '功能增强',
            'urls': {'github': 'https://github.com/demo/Demo',
                     'links': [{'url': 'https://example.invalid/demo'}]},
            'source': {'path': 'PRIVATE_PATH'}, 'secret': 'PRIVATE_TOKEN'}


class TestWikiPageBoundary(unittest.TestCase):
    def test_allows_current_article_and_anchors(self):
        for url in (PAGE, PAGE + '#AE2增强', PAGE + '?action=view',
                    'https://gtnh.huijiwiki.com/wiki/可添加MOD'):
            with self.subTest(url=url):
                self.assertTrue(browser.is_catalog_url(url, PAGE))

    def test_rejects_other_origins_pages_and_noncurrent_views(self):
        for url in ('http://gtnh.huijiwiki.com/wiki/可添加MOD', PAGE + '?action=edit',
                    PAGE + '?oldid=123', PAGE + '?action=raw', PAGE + '?action=',
                    PAGE.replace('gtnh.huijiwiki.com', 'evil.invalid'),
                    PAGE.replace('gtnh.huijiwiki.com', 'gtnh.huijiwiki.com.evil.invalid'),
                    PAGE.replace('/wiki/', ':443/wiki/'),
                    PAGE + 'suffix', 'file:///tmp/wiki.html', None):
            with self.subTest(url=url):
                self.assertFalse(browser.is_catalog_url(url, PAGE))

    def test_payload_contains_only_public_catalog_fields(self):
        installed = {'install_side': 'client', 'sides': {'client': {
            'version': '1.2.3', 'enabled': True, 'file_name': 'PRIVATE_FILE',
            'path': 'PRIVATE_PATH', 'locked': False}}}
        data = browser.catalog_payload([entry()], {'demo': installed})
        self.assertEqual(data[0]['installed'], 'client')
        self.assertEqual(set(data[0]), {'id', 'name', 'cn', 'links', 'installed', 'badges'})
        self.assertNotIn('PRIVATE', json.dumps(data))

    def test_uninstalled_entry_has_an_explicit_badge(self):
        payload = browser.catalog_payload([entry()], {})[0]
        self.assertEqual(payload['badges'], [{'text': '未安装', 'kind': 'absent'}])
        self.assertEqual(payload['installed'], '')

    def test_both_sides_show_versions_and_independent_disabled_and_locked_flags(self):
        state = {'install_side': 'both', 'sides': {
            'client': {'version': '1.2', 'enabled': False},
            'server': {'version': 'v1.1', 'enabled': True, 'locked': True}}}
        self.assertEqual(browser.installation_badges(state), [
            {'text': '已安装：双端', 'kind': 'installed'},
            {'text': '客户端：v1.2（已禁用）', 'kind': 'disabled'},
            {'text': '服务端：v1.1（已锁定）', 'kind': 'locked'}])

    def test_unknown_local_version_stays_explicit(self):
        for version in (None, '未知', 'Unknown'):
            with self.subTest(version=version):
                state = {'install_side': 'server', 'sides': {'server': {'version': version, 'enabled': True}}}
                self.assertEqual(browser.installation_badges(state)[1]['text'], '服务端：版本未知')


class TestWikiGuiBridge(unittest.TestCase):
    def setUp(self):
        self.item = entry()
        self.view = SimpleNamespace(page_url=PAGE)
        self.app = SimpleNamespace(
            _wiki_view=self.view, busy=False, root=mock.Mock(),
            db=SimpleNamespace(wiki_mods=lambda: [self.item]),
            nb=mock.Mock(), refresh_installed=mock.Mock(),
            install_addable=mock.Mock(), show_addable_detail=mock.Mock(),
            _show_inst_detail=mock.Mock(), _merged_registry=mock.Mock(return_value=[]),
            refresh_wiki=mock.Mock())
        self.app.root.grab_current.return_value = None

    def act(self, action, **kw):
        event = {'action': action, 'page': PAGE, 'id': 'demo', **kw}
        gui.GuiApp._handle_wiki_action(self.app, self.view, event)

    def test_install_consumes_local_entry_without_changing_offline_selection(self):
        self.act('install', source={'path': 'UNTRUSTED'}, side='server')
        self.app.install_addable.assert_called_once_with(entries=[self.item])

    def test_details_do_not_depend_on_offline_category_or_search(self):
        self.act('detail')
        self.app.show_addable_detail.assert_called_once_with(entry=self.item)

    def test_manage_reuses_installed_view(self):
        installed = {'mod_id': 'demo', 'sides': {}}
        self.app._merged_registry.return_value = [installed]
        self.act('manage')
        self.app.nb.select.assert_called_once_with(0)
        self.app._show_inst_detail.assert_called_once_with(installed)

    def test_rejects_unknown_custom_or_removed_ids(self):
        self.act('install', id='custom')
        self.app.install_addable.assert_not_called()

    def test_rejects_action_from_non_catalog_document(self):
        self.act('install', page='https://example.invalid')
        self.app.install_addable.assert_not_called()
        self.act('sync', page=PAGE + '?action=edit')
        self.app.refresh_wiki.assert_not_called()

    def test_ignores_actions_while_busy_or_dialog_has_grab(self):
        self.app.busy = True
        self.act('install')
        self.app.busy = False
        self.app.root.grab_current.return_value = mock.Mock()
        self.act('install')
        self.app.install_addable.assert_not_called()

    def test_only_http_links_open_in_system_browser(self):
        with mock.patch.object(gui.webbrowser, 'open') as opened:
            for url in ('file:///private', 'javascript:alert(1)', 'shell:Downloads',
                        'https://user:pass@example.invalid', 'http://['):
                self.act('open', url=url)
            opened.assert_not_called()
            self.act('open', url='https://github.com/demo/Demo')
            opened.assert_called_once_with('https://github.com/demo/Demo')

    def test_stale_browser_cannot_dispatch_actions(self):
        self.app._handle_wiki_action = mock.Mock()
        gui.GuiApp._on_wiki_view_event(self.app, object(), 'action', {'action': 'install'})
        self.app._handle_wiki_action.assert_not_called()

    def test_browser_starts_lazily_only_when_the_page_is_visible(self):
        self.app._wiki_view = None
        self.app.add_view_mode = mock.Mock()
        self.app.add_view_mode.get.return_value = 'wiki'
        self.app.wiki_frame = mock.Mock()
        self.app.wiki_frame.winfo_ismapped.return_value = False
        with mock.patch.object(browser, 'EmbeddedWikiBrowser') as factory:
            gui.GuiApp._ensure_wiki_view(self.app)
        factory.assert_not_called()

    def test_browser_failure_keeps_catalog_and_exposes_offline_view(self):
        self.app._close_wiki_view = mock.Mock()
        self.app.wiki_view_status = mock.Mock()
        self.app.add_view_mode = mock.Mock()
        self.app._show_addable_view = mock.Mock()
        gui.GuiApp._on_wiki_view_event(self.app, self.view, 'error', '连接失败')
        self.assertEqual(self.app.db.wiki_mods(), [self.item])
        self.app.add_view_mode.set.assert_called_once_with('offline')
        self.app._show_addable_view.assert_called_once()

    def test_reentering_wiki_restarts_only_after_saved_proxy_change(self):
        self.view.proxy = None
        self.app.cfg = SimpleNamespace(proxy=None)
        self.app.add_view_mode = mock.Mock()
        self.app.add_view_mode.get.return_value = 'wiki'
        self.app.wiki_frame = mock.Mock()
        self.app.wiki_frame.winfo_ismapped.return_value = True
        self.app.wiki_view_status = mock.Mock()
        self.app._sync_wiki_view = mock.Mock()
        self.app._on_wiki_view_event = mock.Mock()
        self.app._close_wiki_view = mock.Mock()
        with mock.patch.object(browser, 'EmbeddedWikiBrowser') as factory:
            gui.GuiApp._ensure_wiki_view(self.app)
            factory.assert_not_called()
            self.app.cfg.proxy = {'host': ''}
            gui.GuiApp._ensure_wiki_view(self.app)
            self.app._close_wiki_view.assert_called_once()
            factory.return_value.start.assert_called_once()

    def test_explicit_install_keeps_side_confirmation_and_version_lookup(self):
        self.app.cfg, self.app.db = object(), object()
        self.app._selected_addable = mock.Mock()
        self.app._set_busy = mock.Mock()
        self.app._log = mock.Mock()
        self.app._run_async = mock.Mock()
        self.app._on_versions_for_install = mock.Mock()
        with mock.patch.object(gui.updater, 'auto_install_sides', return_value=(['client'], '')) as sides, \
             mock.patch.object(gui.messagebox, 'askyesno', return_value=True) as confirm, \
             mock.patch.object(gui.updater, 'list_install_options', return_value=([], None)) as versions:
            gui.GuiApp.install_addable(self.app, entries=[self.item])
            self.app._selected_addable.assert_not_called()
            sides.assert_called_once_with(self.item, self.app.cfg)
            confirm.assert_called_once()
            self.app._run_async.call_args.args[0]()
            versions.assert_called_once_with(self.item, self.app.cfg, self.app.db, force=True)

    def test_refresh_uses_embedded_online_fetch_and_existing_merge_callback(self):
        self.app._log = mock.Mock()
        self.app._set_busy = mock.Mock()
        self.app._run_async = mock.Mock()
        self.app._on_wiki_done = mock.Mock()
        self.app.queue = mock.Mock()
        self.view.ready = True
        self.view.fetch_wiki = mock.Mock(return_value='online-result')
        gui.GuiApp.refresh_wiki(self.app)
        args = self.app._run_async.call_args
        self.assertEqual(args.args[0](), 'online-result')
        self.assertIs(args.kwargs['on_done'], self.app._on_wiki_done)


class TestEmbeddedFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(Path(self.tmp.name))
        self.view = browser.EmbeddedWikiBrowser(self.cfg, mock.Mock())
        self.view._world = mock.Mock(return_value={'url': PAGE})

    def test_network_response_is_validated_and_cache_written(self):
        self.view._evaluate = mock.Mock(return_value={'status': 200, 'text': RAW})
        result = self.view._fetch()
        self.assertEqual(result.source, 'online')
        self.assertEqual(result.channel, 'embedded_browser')
        self.assertEqual(result.request_count, 1)
        self.assertEqual(result.mods[0]['name_en'], 'Demo')
        self.assertEqual((self.cfg.data_dir / 'cache/wiki_wikitext.txt').read_text(encoding='utf-8'), RAW)
        script = self.view._evaluate.call_args.args[0]
        self.assertIn("cache:'no-store'", script)
        self.assertIn('gtnh_refresh=', script)

    def test_error_and_challenge_never_replace_cache(self):
        cache = self.cfg.data_dir / 'cache/wiki_wikitext.txt'
        cache.parent.mkdir()
        cache.write_text('KEEP', encoding='utf-8')
        for response in ({'status': 403}, {'status': 200, 'text': '<html>Just a moment...</html>'},
                         {'status': 200, 'text': '{{可添加MOD表格行/头}}'}):
            with self.subTest(response=response):
                self.view._evaluate = mock.Mock(return_value=response)
                with self.assertRaises(Exception):
                    self.view._fetch()
                self.assertEqual(cache.read_text(encoding='utf-8'), 'KEEP')

    def test_fetch_requires_the_catalog_origin(self):
        self.view._world.return_value = {'url': 'https://example.invalid'}
        self.view._evaluate = mock.Mock()
        with self.assertRaises(RuntimeError):
            self.view._fetch()
        self.view._evaluate.assert_not_called()


class TestBrowserLifecycle(unittest.TestCase):
    def test_browser_launch_respects_all_proxy_modes_and_keeps_credentials_out_of_command(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = Config(Path(folder))
            for proxy, expected in ((None, None), ({'host': ''}, '--no-proxy-server'),
                                   ({'host': '127.0.0.1', 'port': 8080, 'user': 'PRIVATE_USER',
                                     'pass': 'PRIVATE_PASSWORD'}, '--proxy-server=http://127.0.0.1:8080')):
                with self.subTest(proxy=proxy):
                    cfg.data['proxy'] = proxy
                    view = browser.EmbeddedWikiBrowser(cfg, mock.Mock())
                    cfg.data['proxy'] = {'host': 'changed.invalid'}
                    def launch(command, **kwargs):
                        view.close()
                        return mock.Mock()
                    with mock.patch.object(wiki, '_find_system_browser', return_value=Path('browser.exe')), \
                         mock.patch.object(wiki, '_get_free_port', return_value=12345), \
                         mock.patch.object(browser.subprocess, 'Popen', side_effect=launch) as opened:
                        view._worker()
                    command = opened.call_args.args[0]
                    flags = [arg for arg in command if arg.startswith(('--proxy-server', '--no-proxy'))]
                    self.assertEqual(flags, [expected] if expected else [])
                    self.assertNotIn('PRIVATE_', ' '.join(command))
                    self.assertTrue(view.wait_closed(timeout=0))

    def test_close_wakes_a_pending_cdp_receive(self):
        view = browser.EmbeddedWikiBrowser(SimpleNamespace(proxy=None, wiki_url=PAGE,
                                                           wiki_page='可添加MOD'), mock.Mock())
        receiving, released = threading.Event(), threading.Event()
        errors = []
        def recv():
            receiving.set()
            released.wait(2)
            raise OSError('closed')
        view._ws = mock.Mock()
        view._ws.recv.side_effect = recv
        view._ws.abort.side_effect = released.set
        def call():
            try:
                view._call('Runtime.evaluate', timeout=20)
            except OSError as exc:
                errors.append(exc)
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        self.assertTrue(receiving.wait(1))
        view.close()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)


@unittest.skipUnless(os.name == 'nt' and os.environ.get('GTNHMOD_TEST_WIKI_BROWSER') == '1',
                     '显式启用的本地 HTTP/真实浏览器嵌入验收')
class TestRealEmbeddedBrowser(unittest.TestCase):
    def test_native_embedding_online_sync_resize_and_close(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                is_raw = self.path.startswith('/index.php?')
                body = (RAW if is_raw else HTML).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', ('text/plain' if is_raw else 'text/html') + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except ConnectionError:
                    pass  # 关闭专用浏览器时，尚未完成的资源请求可以正常中断。

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            cfg = Config(Path(folder))
            cfg.data['wiki_url'] = f'http://127.0.0.1:{server.server_port}/api.php'
            client_folder = Path(folder) / 'client'
            client_folder.mkdir()
            existing = client_folder / 'Demo-0.9.jar'
            existing.write_bytes(b'isolated scan fixture before first Wiki sync')
            cfg.data['mods_folders']['client'] = str(client_folder)
            cfg.save()
            app = gui.GuiApp(Path(folder))
            self.addCleanup(app.root.destroy)
            self.addCleanup(app._close_wiki_view)
            app.nb.select(1)

            def pump(predicate, seconds=25):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    app.root.update()
                    if predicate():
                        return True
                    time.sleep(.03)
                return False

            self.assertTrue(pump(lambda: bool(app.db.wiki_mods()) and not app.busy),
                            app.wiki_view_status.get())
            view = app._wiki_view
            self.assertIsNotNone(view)
            self.assertTrue(pump(lambda: '1 个已匹配' in app.wiki_view_status.get()))
            self.assertEqual(view._host.api.GetParent(view._host.hwnd), app.wiki_frame.winfo_id())
            self.assertTrue(any(p.startswith('/index.php?') for p in requests))
            self.assertTrue(app.db.meta['wiki_fetched_at'])
            self.assertIn('embedded_browser', app.log.get('1.0', 'end'))
            self.assertEqual(view._snapshot[0][0]['installed'], 'client')
            self.assertEqual(view._snapshot[0][0]['badges'][1]['text'], '客户端：v0.9')
            existing.unlink()
            app.refresh_all()
            self.assertEqual(view._snapshot[0][0]['badges'][0]['text'], '未安装')
            # 仅在临时目录放置扫描夹具，证明状态来自原有双端扫描结果。
            for side in ('client', 'server'):
                target = Path(folder) / side
                target.mkdir(exist_ok=True)
                app.cfg.data['mods_folders'][side] = str(target)
            client_file = app.cfg.mods_dir('client') / 'Demo-1.2.3.jar.disabled'
            server_file = app.cfg.mods_dir('server') / 'Demo-1.2.0.jar'
            client_file.write_bytes(b'isolated scan fixture')
            server_file.write_bytes(b'isolated scan fixture')
            app.installed.set('server', 'demo', locked=True)
            app.refresh_all()
            badges = view._snapshot[0][0]['badges']
            self.assertEqual([b['text'] for b in badges], [
                '已安装：双端', '客户端：v1.2.3（已禁用）', '服务端：v1.2.0（已锁定）'])
            self.assertEqual(view._snapshot[0][0]['installed'], 'both')
            client_file.unlink()
            server_file.unlink()
            app.refresh_all()
            self.assertEqual(view._snapshot[0][0]['badges'][0]['text'], '未安装')
            app.root.geometry('980x640')
            app.root.update()
            rect = view._host.viewport_rect()
            self.assertIsNotNone(rect)
            self.assertEqual(rect.right - rect.left, app.wiki_frame.winfo_width())
            self.assertEqual(rect.bottom - rect.top, app.wiki_frame.winfo_height())
            app.add_view_mode.set('offline')
            app._show_addable_view()
            app.root.update()
            self.assertTrue(app.add_tree.winfo_ismapped())
            proc = view._proc
            app._close_wiki_view()
            self.assertTrue(view.wait_closed(timeout=10))
            self.assertTrue(pump(lambda: proc.poll() is not None, seconds=10))


if __name__ == '__main__':
    unittest.main()
