"""资源包 Wiki 的在线证据、桥接边界和可选真实浏览器验收。"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from gtnhmod import gui, packs, packs_browser, packs_wiki
from gtnhmod.config import Config
from tests.test_packs_manager import fake_entry, make_zip

TEXT = '''== 资源包 ==
=== 无障碍与实用性 ===
{| class="wikitable"
! 图标 !! 名字 !! 作者/维护者 !! 范围 !! 说明 !! 链接
|-
| [[File:Demo.png]]
| Demo Pack
| Tester
| GTNH
| 资源包夹具
| [https://example.invalid/pack 下载]
|}
== 光影 ==
=== 推荐列表 ===
{| class="wikitable"
! 图标 !! 名字 !! 作者/维护者 !! 适用加载器 !! 说明 !! 链接
|-
| [[File:Demo.png]]
| Demo Shader
| Tester
| Angelica
| 光影夹具
| [https://example.invalid/shader 下载]
|-
| [[File:Demo.png]]
| Demo Shader + Patch
| Tester
| Angelica
| 共享上游链接的换行变体名称
| [https://example.invalid/shader 下载]
|}
'''
HTML = '''<!doctype html><meta charset="utf-8"><title>资源包与光影隔离验收</title>
<style>body{font:15px sans-serif;margin:24px}td{padding:12px;border:1px solid #888}img{width:32px}</style>
<div id="mw-content-text"><h2><span class="mw-headline">资源包</span></h2>
<table class="wikitable"><tr><th>图标</th><th>名字</th><th>作者/维护者</th><th>范围</th><th>说明</th><th>链接</th></tr>
<tr><td><img src="/icon.jpg"></td><td>Demo Pack</td><td>Tester</td><td>GTNH</td><td>资源包夹具</td>
<td><a href="https://example.invalid/pack">下载</a></td></tr></table>
<h2><span class="mw-headline">光影</span></h2>
<table class="wikitable"><tr><th>图标</th><th>名字</th><th>作者/维护者</th><th>适用加载器</th><th>说明</th><th>链接</th></tr>
<tr><td><img src="/icon.jpg"></td><td>Demo Shader</td><td>Tester</td><td>Angelica</td><td>光影夹具</td>
<td><a href="https://example.invalid/shader">下载</a></td></tr></table></div>'''
HTML = HTML.replace('</tr></table></div>', '</tr><tr><td><img src="/icon.jpg"></td>'
    '<td>Demo Shader<br/> + Patch</td><td>Tester</td><td>Angelica</td><td>换行变体</td>'
    '<td><a href="https://example.invalid/shader">下载</a></td></tr></table></div>')


class TestPackEmbeddedFetch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(Path(self.temp.name))
        self.view = packs_browser.EmbeddedPacksBrowser(self.cfg, mock.Mock())
        self.view._world = mock.Mock(return_value={'url': self.view.page_url})

    def test_fetches_pack_page_with_fresh_request_and_separate_validated_cache(self):
        self.view._evaluate = mock.Mock(return_value={'status': 200, 'text': TEXT})
        result = self.view._fetch()
        self.assertEqual(result.source, 'online')
        self.assertEqual(result.channel, 'embedded_browser')
        self.assertEqual(result.request_count, 1)
        self.assertEqual(len(packs_wiki.parse_packs_wikitext(result.text)[0]), 3)
        self.assertEqual(packs_wiki.packs_cache_file(self.cfg).read_text(encoding='utf-8'), TEXT)
        self.assertFalse((self.cfg.cache_dir / 'wiki_wikitext.txt').exists())
        script = self.view._evaluate.call_args.args[0]
        self.assertIn('gtnh_refresh=', script)
        self.assertIn("cache:'no-store'", script)
        self.assertIn('%E8%B5%84%E6%BA%90%E5%8C%85', script)

    def test_challenge_empty_and_header_only_content_never_replace_cache(self):
        cache = packs_wiki.packs_cache_file(self.cfg)
        cache.parent.mkdir(parents=True)
        cache.write_text('KEEP', encoding='utf-8')
        for text in ('', '<html>Just a moment...</html>', '== 资源包 ==\n== 光影 =='):
            with self.subTest(text=text):
                self.view._evaluate = mock.Mock(return_value={'status': 200, 'text': text})
                with self.assertRaises(Exception):
                    self.view._fetch()
                self.assertEqual(cache.read_text(encoding='utf-8'), 'KEEP')


class TestPackGuiBridge(unittest.TestCase):
    def setUp(self):
        self.entry = fake_entry()
        self.view = SimpleNamespace(page_url='https://gtnh.huijiwiki.com/wiki/资源包与光影')
        self.app = SimpleNamespace(busy=False, root=mock.Mock(),
            packs=SimpleNamespace(get=lambda mid: self.entry if mid == self.entry['id'] else None),
            _pack_view=self.view, _install_packs=mock.Mock(), _pack_detail_dialog=mock.Mock(),
            refresh_packs_wiki=mock.Mock(), _handle_wiki_action=mock.Mock())
        self.app.root.grab_current.return_value = None

    def action(self, action='install', **extra):
        gui.GuiApp._handle_pack_wiki_action(self.app, self.view,
            {'page': self.view.page_url, 'id': self.entry['id'], 'action': action, **extra})

    def test_only_local_entry_is_used_for_install_and_detail(self):
        self.action(source='UNTRUSTED', path='UNTRUSTED', kind='shader')
        self.app._install_packs.assert_called_once_with([self.entry])
        self.action('detail')
        self.app._pack_detail_dialog.assert_called_once_with(self.entry)

    def test_builtin_custom_unknown_other_page_and_busy_cannot_install(self):
        for flag in ('local', 'custom', 'wiki_removed'):
            self.entry[flag] = True
            self.action()
            self.entry[flag] = False
        self.action(id='missing')
        self.action(page='https://example.invalid')
        self.app.busy = True
        self.action()
        self.app._install_packs.assert_not_called()

    def test_modal_dialog_blocks_actions_and_stale_browser_events_are_ignored(self):
        self.app.root.grab_current.return_value = object()
        self.action()
        self.app._install_packs.assert_not_called()
        self.app._handle_pack_wiki_action = mock.Mock()
        gui.GuiApp._on_pack_view_event(self.app, object(), 'action', {})
        self.app._handle_pack_wiki_action.assert_not_called()

    def test_icon_metadata_defers_while_busy_and_does_not_forge_online_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            db = packs.PacksDB(Path(directory) / 'packs_db.json')
            db.packs = [self.entry]
            db.meta['wiki_fetched_at'] = 'KEEP'
            app = SimpleNamespace(busy=True, packs=db,
                cfg=SimpleNamespace(wiki_url='https://gtnh.huijiwiki.com/api.php'), _refresh_pack_icons=mock.Mock())
            icons = [{'id': self.entry['id'], 'url': 'https://huiji-public.huijistatic.com/gtnh/uploads/a/icon.png'}]
            gui.GuiApp._apply_pack_icons(app, icons)
            self.assertNotIn('icon_url', self.entry)
            app.busy = False
            gui.GuiApp._apply_pack_icons(app, icons)
            stored = packs.PacksDB(db.path)
            self.assertEqual(stored.get(self.entry['id'])['icon_url'], icons[0]['url'])
            self.assertEqual(stored.meta['wiki_fetched_at'], 'KEEP')


@unittest.skipUnless(os.name == 'nt' and os.environ.get('GTNHMOD_TEST_WIKI_BROWSER') == '1',
                     '显式启用的资源包本地 HTTP/真实浏览器嵌入验收')
class TestRealPackBrowser(unittest.TestCase):
    def test_native_pack_wiki_sync_status_icons_and_shutdown(self):
        from tests.test_mod_icons import picture
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                raw = self.path.startswith('/index.php?')
                icon = self.path.startswith('/icon.jpg')
                body = picture(fmt='JPEG') if icon else (TEXT if raw else HTML).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg' if icon else ('text/plain' if raw else 'text/html') + '; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except ConnectionError:
                    pass
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            cfg = Config(root)
            cfg.data.update(wiki_url=f'http://127.0.0.1:{server.server_port}/api.php', proxy={'host': ''})
            for kind, name, probe in (('resourcepack', 'Demo.Pack.v1.0.zip', 'pack.mcmeta'),
                                      ('shader', 'Demo.Shader.v1.1.zip', 'shaders/main.fsh')):
                folder = root / kind
                folder.mkdir()
                cfg.data['pack_folders'][kind] = str(folder)
                make_zip(folder / name, [probe])
            cfg.save()
            app = gui.GuiApp(root)
            self.addCleanup(app.root.destroy)
            self.addCleanup(app._close_pack_view)
            app.nb.select(4)
            app.pack_nb.select(1)
            def pump(predicate, seconds=25):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    app.root.update()
                    if predicate():
                        return True
                    time.sleep(.03)
                return False
            self.assertTrue(pump(lambda: len(app.packs.by_kind()) == 3 and not app.busy), app.pack_wiki_status.get())
            view = app._pack_view
            self.assertTrue(pump(lambda: '3 个已匹配' in app.pack_wiki_status.get()), app.pack_wiki_status.get())
            self.assertEqual(view._host.api.GetParent(view._host.hwnd), app.pack_wiki_frame.winfo_id())
            self.assertEqual(len(app.pack_inst_tree.get_children()), 2)
            self.assertEqual({e['id'] for e in view._snapshot[0] if e['installed']},
                             {'pack-demo-pack', 'shader-demo-shader'})
            self.assertTrue(pump(lambda: all(e.get('icon_url') for e in app.packs.by_kind())))
            raw = next(path for path in requests if path.startswith('/index.php?'))
            self.assertEqual(parse_qs(urlsplit(raw).query)['title'], [packs_wiki.PACK_PAGE])
            self.assertIn('gtnh_refresh=', raw)
            self.assertTrue(app.packs.meta['wiki_fetched_at'])
            packs.set_enabled(app.cfg, app.packs, 'resourcepack', 'pack-demo-pack', False)
            packs.set_locked(app.packs, 'shader', 'shader-demo-shader', True)
            app.refresh_packs()
            payload = {e['id']: e for e in view._snapshot[0]}
            self.assertIn('文件已禁用', [b['text'] for b in payload['pack-demo-pack']['badges']])
            self.assertIn('已锁定更新', [b['text'] for b in payload['shader-demo-shader']['badges']])
            self.assertNotIn(str(root), json.dumps(view._snapshot, ensure_ascii=False))
            app.pack_view_mode.set('offline')
            app._show_pack_view()
            app.root.update()
            self.assertTrue(app.pack_tree.winfo_ismapped())
            proc = view._proc
            app._close_pack_view()
            self.assertTrue(view.wait_closed(timeout=10))
            self.assertIsNotNone(proc.poll())


if __name__ == '__main__':
    unittest.main()
