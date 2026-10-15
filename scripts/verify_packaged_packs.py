"""Verify packaged CLI -> online pack Wiki -> parser -> database in temporary directories.

Usage: py scripts/verify_packaged_packs.py dist/gtnh-cli.exe [--live]
Local acceptance requests two different revisions; --live starts with an empty cache.
No existing configuration, browser profile or game directory is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    executable = Path(sys.argv[1]).resolve()
    command = [sys.executable, str(executable)] if executable.suffix == '.py' else [str(executable)]
    state = {'revision': 1, 'requests': []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append(self.path)
            text = (f'== 资源包 ==\n=== 无障碍与实用性 ===\n'
                '{| class="wikitable"\n! 图标 !! 名字 !! 作者 !! 范围 !! 说明 !! 链接\n|-\n'
                '|\n| Network Pack\n| Tester\n| GTNH\n'
                f"| upstream-revision-{state['revision']}\n|\n|}}\n"
                '== 光影 ==\n=== 推荐列表 ===\n{| class="wikitable"\n'
                '! 图标 !! 名字 !! 作者 !! 适用加载器 !! 说明 !! 链接\n|-\n'
                '|\n| Network Shader\n| Tester\n| Angelica\n| Shader fixture\n|\n|}\n')
            if 'action=parse' in self.path:
                text = json.dumps({'parse': {'wikitext': {'*': text}}})
            body = text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='gtnh_packaged_packs_') as directory:
            root = Path(directory)
            mods = root / 'mods'
            mods.mkdir()
            live = '--live' in sys.argv[2:]
            cfg = {'mods_folders': {'client': str(mods), 'server': ''},
                   'wiki_url': 'https://gtnh.huijiwiki.com/api.php' if live else f'http://127.0.0.1:{server.server_port}/api.php',
                   'proxy': None if live else {'host': ''}}
            (root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
            env = dict(os.environ, GTNHMOD_DATA_DIR=str(root), PYTHONIOENCODING='utf-8')
            for revision in ((1,) if live else (1, 2)):
                state['revision'] = revision
                before = len(state['requests'])
                result = subprocess.run(command, input=b'11\n1\n0\n12\n', env=env, capture_output=True,
                                        timeout=180 if live else 60)
                assert result.returncode == 0, result.stderr.decode(errors='replace')
                assert '在线完成' in result.stdout.decode('utf-8'), 'CLI did not complete an online pack refresh'
                saved = json.loads((root / 'packs_db.json').read_text(encoding='utf-8'))
                assert saved.get('meta', {}).get('wiki_fetched_at')
                assert saved['packs'], 'No packs persisted'
                if live:
                    print(f"live Wiki -> packaged CLI -> database: {len(saved['packs'])} entries PASS")
                else:
                    assert len(state['requests']) > before, 'No network request was made'
                    entry = next(p for p in saved['packs'] if p['name_en'] == 'Network Pack')
                    assert f'upstream-revision-{revision}' in entry['desc'], entry
                    assert {p['kind'] for p in saved['packs']} == {'resourcepack', 'shader'}
                    print(f'revision={revision}: actual HTTP -> resourcepack/shader persistence PASS')
            print('Pack Wiki refresh PASS; production data untouched')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
