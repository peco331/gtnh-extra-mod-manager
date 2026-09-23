"""Exercise the shipped CLI's network -> parser -> database path in isolation.

Usage: py scripts/verify_packaged_wiki.py dist/gtnh-cli.exe
       py scripts/verify_packaged_wiki.py launcher_cli.py
No production configuration, browser profile or mod directory is used.
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
    command = ([sys.executable, str(executable)] if executable.suffix == ".py"
               else [str(executable)])
    state = {"revision": 1, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            text = ("== 非星门规则模组 ==\n{{额外项目\n"
                    "|模组英文名=NetworkProof\n|模组中文名=联网验证\n"
                    f"|简述=upstream-revision-{state['revision']}\n"
                    "|相关地址=https://example.invalid/\n}}\n")
            if "action=parse" in self.path:
                text = json.dumps({"parse": {"wikitext": {"*": text}}})
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="gtnh_packaged_wiki_") as tmp:
            root = Path(tmp)
            mods = root / "mods"
            mods.mkdir()
            live = "--live" in sys.argv[2:]
            cfg = {"mods_folders": {"client": str(mods), "server": ""},
                   "wiki_url": f"http://127.0.0.1:{server.server_port}/api.php",
                   "wiki_page": "Proof", "proxy": {"host": "", "port": 0}}
            if live:
                cfg.update(wiki_url="https://gtnh.huijiwiki.com/api.php",
                           wiki_page="可添加MOD", proxy=None)
            (root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            env = dict(os.environ, GTNHMOD_DATA_DIR=str(root), PYTHONIOENCODING="utf-8")
            for revision in ((1,) if live else (1, 2)):
                state["revision"] = revision
                before = len(state["requests"])
                proc = subprocess.run(command, input=b"1\n11\n", env=env,
                                      capture_output=True, timeout=180 if live else 60)
                assert proc.returncode == 0, proc.stderr.decode(errors="replace")
                saved = json.loads((root / "mods_db.json").read_text(encoding="utf-8"))
                assert saved.get("meta", {}).get("wiki_fetched_at"), (
                    "online Wiki timestamp missing; refresh did not persist an online result")
                if live:
                    assert saved["mods"], "no upstream entries persisted"
                    print(f"live upstream -> clean process -> database: {len(saved['mods'])} entries")
                    print("No pre-existing Wiki cache was available in this run.")
                    continue
                assert len(state["requests"]) > before, "refresh made no network request"
                entry = next(m for m in saved["mods"] if m["name_en"] == "NetworkProof")
                assert entry["desc"] == f"upstream-revision-{revision}", entry
                print(f"revision={revision}: actual HTTP request and persisted content PASS")
            print("Wiki refresh PASS; production data untouched")
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
