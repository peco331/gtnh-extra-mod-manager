"""图标的读取、缓存与网络边界，不访问真实游戏目录或上游。"""
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from gtnhmod import gui, mod_icons, wiki
from gtnhmod.db import ModsDB


def picture(color='red', fmt='PNG', size=(32, 24)):
    data = io.BytesIO()
    Image.new('RGB', size, color).save(data, format=fmt)
    return data.getvalue()


class TestModIconFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jar = Path(self.temp.name) / 'Demo.jar'

    def write_jar(self, metadata, files):
        with zipfile.ZipFile(self.jar, 'w') as archive:
            archive.writestr('mcmod.info', json.dumps(metadata))
            for name, data in files.items():
                archive.writestr(name, data)

    def test_reads_logo_from_mcmod_info_without_extracting(self):
        self.write_jar([{'name': 'Demo', 'modid': 'demo', 'logoFile': '/assets/demo/logo.jpg'}],
                       {'assets/demo/logo.jpg': picture(fmt='JPEG')})
        result = mod_icons.jar_icon(self.jar, ('demo',))
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (16, 16))
            self.assertEqual(image.mode, 'RGBA')
        self.assertEqual([p.name for p in self.jar.parent.iterdir()], ['Demo.jar'])

    def test_selects_matching_mod_in_multi_mod_jar(self):
        self.write_jar({'modList': [
            {'modid': 'other', 'logoFile': 'other.png'},
            {'modid': 'demo', 'logoFile': 'demo.png'}]},
            {'other.png': picture('red'), 'demo.png': picture('blue')})
        image = Image.open(io.BytesIO(mod_icons.jar_icon(self.jar, ('demo',))))
        self.assertEqual(image.getpixel((8, 8)), (0, 0, 255, 255))

    def test_unknown_multi_mod_identity_does_not_choose_another_mod_logo(self):
        self.write_jar([{'modid': 'a', 'logoFile': 'a.png'}, {'modid': 'b', 'logoFile': 'b.png'}],
                       {'a.png': picture(), 'b.png': picture()})
        self.assertIsNone(mod_icons.jar_icon(self.jar, ('unknown',)))

    def test_malformed_archive_and_missing_logo_are_optional(self):
        self.jar.write_bytes(b'not a zip')
        self.assertIsNone(mod_icons.jar_icon(self.jar))
        self.write_jar([{'modid': 'demo', 'logoFile': 'missing.png'}], {})
        self.assertIsNone(mod_icons.jar_icon(self.jar))

    def test_traversal_logo_and_oversized_member_are_ignored(self):
        self.write_jar([{'modid': 'demo', 'logoFile': '../outside.png'}],
                       {'../outside.png': picture(), 'logo.png': b'x' * (mod_icons.MAX_IMAGE_BYTES + 1)})
        self.assertIsNone(mod_icons.jar_icon(self.jar, ('demo',)))

    def test_thumbnail_rejects_large_input_and_large_dimensions(self):
        with self.assertRaises(ValueError):
            mod_icons.thumbnail_png(b'x' * (mod_icons.MAX_IMAGE_BYTES + 1))
        with self.assertRaises(ValueError):
            mod_icons.thumbnail_png(picture(size=(4097, 1)))

    def test_preserves_wiki_icon_filename_and_resolved_url_on_refresh(self):
        text = '''== 星门规则模组 ==
=== 功能增强 ===
{{可添加MOD表格行
|模组英文名=Demo
|图标=Example icon.png
}}'''
        fresh = wiki.parse_wikitext(text)[0]
        self.assertEqual(fresh[0]['icon'], 'Example icon.png')
        db = ModsDB(Path(self.temp.name) / 'mods.json')
        db.mods = [{**fresh[0], 'icon_url': 'https://gtnh.huijiwiki.com/images/icon.png'}]
        db.merge_wiki(fresh)
        self.assertEqual(db.mods[0]['icon_url'], 'https://gtnh.huijiwiki.com/images/icon.png')
        fresh[0]['icon'] = 'Replacement.png'
        db.merge_wiki(fresh)
        self.assertNotIn('icon_url', db.mods[0])

    def test_resolved_wiki_icons_defer_during_a_task_and_do_not_change_fetch_evidence(self):
        item = {'id': 'demo'}
        db = SimpleNamespace(wiki_mods=lambda: [item], save=mock.Mock(), meta={'wiki_fetched_at': 'KEEP'})
        app = SimpleNamespace(busy=True, cfg=SimpleNamespace(wiki_url='https://gtnh.huijiwiki.com/api.php'),
                              db=db, _refresh_mod_icons=mock.Mock())
        icons = [{'id': 'demo', 'url': 'https://gtnh.huijiwiki.com/images/demo.png'}]
        gui.GuiApp._apply_wiki_icons(app, icons)
        self.assertNotIn('icon_url', item)
        db.save.assert_not_called()
        app.busy = False
        gui.GuiApp._apply_wiki_icons(app, icons)
        self.assertEqual(item['icon_url'], icons[0]['url'])
        self.assertEqual(db.meta, {'wiki_fetched_at': 'KEEP'})
        app._refresh_mod_icons.assert_called_once()
        gui.GuiApp._apply_wiki_icons(app, [{'id': 'demo', 'url': 'file:///private.png'}])
        self.assertEqual(item['icon_url'], icons[0]['url'])


class TestIconNetwork(unittest.TestCase):
    def test_cdn_rejection_uses_existing_optional_browser_tls_channel(self):
        url, site = 'https://gtnh.huijiwiki.com/images/icon.png', 'https://gtnh.huijiwiki.com/api.php'
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(url, 567, 'blocked', {}, None)
        with mock.patch.object(mod_icons.net, '_opener_for', return_value=opener), \
             mock.patch.object(mod_icons, '_download_browser_tls', return_value=b'PNG') as alternate:
            self.assertEqual(mod_icons.download_icon(url, site, None), b'PNG')
        alternate.assert_called_once_with(url, site, None)

    def test_browser_tls_download_is_bounded_and_checks_each_redirect(self):
        site = 'https://gtnh.huijiwiki.com/api.php'
        session = mock.MagicMock()
        response = mock.Mock(status_code=200, headers={})
        response.iter_content.return_value = [picture()]
        session.get.return_value = response
        requests = mock.MagicMock()
        requests.Session.return_value.__enter__.return_value = session
        with mock.patch.dict('sys.modules', {'curl_cffi': SimpleNamespace(requests=requests)}):
            result = mod_icons._download_browser_tls('https://gtnh.huijiwiki.com/image.png', site, {'host': ''})
            self.assertEqual(Image.open(io.BytesIO(result)).size, (16, 16))
            requests.Session.assert_called_with(trust_env=False, proxies=None)
            response.status_code = 302
            response.headers = {'Location': 'https://example.invalid/unrelated.png'}
            session.get.reset_mock()
            with self.assertRaises(ValueError):
                mod_icons._download_browser_tls('https://gtnh.huijiwiki.com/image.png', site, None)
            session.get.assert_called_once()
            response.status_code, response.headers = 200, {}
            response.iter_content.return_value = [b'x' * (mod_icons.MAX_IMAGE_BYTES + 1)]
            with self.assertRaises(ValueError):
                mod_icons._download_browser_tls('https://gtnh.huijiwiki.com/image.png', site, None)

    def test_accepts_only_configured_wiki_or_wiki_image_cdn(self):
        site = 'https://gtnh.huijiwiki.com/api.php'
        self.assertTrue(mod_icons.allowed_icon_url('https://huiji-public.huijistatic.com/gtnh/uploads/a/aa/mod.png', site))
        self.assertTrue(mod_icons.allowed_icon_url('https://huiji-thumb.huijistatic.com/gtnh/uploads/thumb/a/aa/mod.png/64px-mod.png', site))
        self.assertTrue(mod_icons.allowed_icon_url('https://gtnh.huijiwiki.com/images/mod.png', site))
        for url in ('file:///private.png', 'https://evil.invalid/a.png',
                    'https://huiji-public.huijistatic.com/other/private.png',
                    'https://user:secret@gtnh.huijiwiki.com/a.png',
                    'http://gtnh.huijiwiki.com/a.png', 'http://['):
            self.assertFalse(mod_icons.allowed_icon_url(url, site))

    def test_local_http_image_is_bounded_and_redirects_are_checked(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'https://example.invalid/unrelated.png')
                    self.end_headers()
                    return
                body = picture('blue', 'JPEG')
                self.send_response(200)
                self.send_header('Content-Length', str(len(body) if self.path != '/huge' else mod_icons.MAX_IMAGE_BYTES + 1))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        data = mod_icons.download_icon(base + '/image', base + '/api.php', {'host': ''})
        self.assertEqual(Image.open(io.BytesIO(data)).size, (16, 16))
        with self.assertRaises(ValueError):
            mod_icons.download_icon(base + '/huge', base + '/api.php', {'host': ''})
        with self.assertRaises(ValueError):
            mod_icons.download_icon(base + '/redirect', base + '/api.php', {'host': ''})
        self.assertTrue(all('Cookie' not in headers and 'Authorization' not in headers for headers in seen))


if __name__ == '__main__':
    unittest.main()
