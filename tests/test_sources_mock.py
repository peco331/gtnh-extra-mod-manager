"""sources.py / downloader.py 测试：本地 http.server 模拟 GitHub API。"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod import net  # noqa: E402
from gtnhmod.downloader import VerifyError, update_with_backup, verify_jar  # noqa: E402
from gtnhmod.sources import (  # noqa: E402
    DownloadCandidate, GitHubSource, LocalFolderSource, ManualSource,
    extract_version, pick_assets,
)

MAIN_JAR = "FakeMod-1.7.10-0.9.0.jar"
RELEASE = {
    "tag_name": "0.9.0",
    "body": "更新日志：修了bug",
    "published_at": "2026-07-23T13:32:36Z",
    "assets": [
        {"name": MAIN_JAR, "browser_download_url": "http://127.0.0.1:PORT/download/main.jar", "size": 100},
        {"name": "FakeMod-1.7.10-0.9.0-sources.jar",
         "browser_download_url": "http://127.0.0.1:PORT/download/src.jar", "size": 50},
    ],
}
TAGS = [{"name": "0.8.0"}, {"name": "0.9.1"}, {"name": "dev-build"}]
TAGS_RELEASE = {
    "tag_name": "0.9.1",
    "body": "tags fallback release",
    "assets": [{"name": "FakeMod-1.7.10-0.9.1.jar",
                "browser_download_url": "http://127.0.0.1:PORT/download/main.jar", "size": 100}],
}

JAR_BYTES = b"PK\x03\x04" + b"\x00" * 100


class MockHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        key = (self.command, path)
        etag = self.headers.get("If-None-Match")
        if key not in self.routes:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"message": "Not Found"}')
            return
        body, extra = self.routes[key]
        if extra.get("etag") and etag == extra["etag"]:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(extra.get("status", 200))
        for k, v in extra.get("headers", {}).items():
            self.send_header(k, v)
        if extra.get("etag"):
            self.send_header("ETag", extra["etag"])
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server(routes):
    handler = type("H", (MockHandler,), {"routes": routes})
    srv = HTTPServer(("127.0.0.1", 0), handler)  # 自动分配空闲端口
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


def release_body(port):
    return json.dumps(RELEASE).encode().replace(
        b"127.0.0.1:PORT", f"127.0.0.1:{port}".encode())


def make_source(port, tmp: Path, with_token=False):
    return GitHubSource("owner", "FakeMod", api_base=f"http://127.0.0.1:{port}",
                        cache_dir=tmp / "cache", token="tok" if with_token else "",
                        ttl_hours=0, exclude_regex="sources")


class TestGitHubSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_src_"))
        routes = {}
        cls.srv, cls.port = start_server(routes)
        routes[("GET", "/repos/owner/FakeMod/releases/latest")] = (
            release_body(cls.port), {"etag": '"e1"', "headers": {"X-RateLimit-Remaining": "59"}})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_check_latest(self):
        src = make_source(self.port, self.tmp)
        info = src.check("0.8.0")
        self.assertEqual(info.latest_version, "0.9.0")
        self.assertEqual(info.release_body, "更新日志：修了bug")
        self.assertEqual(info.published_at, "2026-07-23T13:32:36Z")
        self.assertEqual(info.candidates[0].file_name, MAIN_JAR)  # sources包被排除
        self.assertEqual(len(info.candidates), 1)

    def test_rate_remaining_tracked(self):
        self.assertEqual(net.rate_remaining, 59)

    def test_version_from_mc_tag(self):
        self.assertEqual(extract_version("1.7.10-0.8.0"), "0.8.0")
        self.assertEqual(extract_version("0.8.0"), "0.8.0")

    def test_version_from_modver_mc_tag(self):
        # 第二段是已知 MC 版本 → 首段是 mod 版本，整体保留
        self.assertEqual(extract_version("1.0.1-1.7.10-GTNH"), "1.0.1-1.7.10-GTNH")
        self.assertEqual(extract_version("1.0.1-1.12.2"), "1.0.1-1.12.2")

    def test_cached_fresh(self):
        # ttl=0 下第二次请求应 304 并返回缓存数据
        src = make_source(self.port, self.tmp)
        info1 = src.check("0.8.0")
        info2 = src.check("0.8.0")
        self.assertEqual(info1.latest_version, info2.latest_version)


class TestGitHubTagsFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_src2_"))
        body = json.dumps(TAGS).encode()
        routes = {}
        cls.srv, cls.port = start_server(routes)
        rel = json.dumps(TAGS_RELEASE).encode().replace(
            b"127.0.0.1:PORT", f"127.0.0.1:{cls.port}".encode())
        routes.update({
            ("GET", "/repos/owner/FakeMod/releases/latest"):
                (b'{"message": "Not Found"}', {"status": 404}),
            ("GET", "/repos/owner/FakeMod/tags"): (body, {}),
            ("GET", "/repos/owner/FakeMod/releases/tags/0.9.1"): (rel, {}),
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_tags_fallback(self):
        src = make_source(self.port, self.tmp)
        info = src.check("0.8.0")
        self.assertEqual(info.latest_version, "0.9.1")  # dev-build 无法解析被跳过
        self.assertEqual(info.candidates[0].file_name, "FakeMod-1.7.10-0.9.1.jar")

    def test_tags_fallback_second_check_within_ttl(self):
        # 首次 404 后写入 not_found 标记缓存；ttl 内再次检查不得崩溃，
        # 应同样走 tags 兜底（回归：标记缓存 data=None 导致 _from_release(None)）
        src = GitHubSource("owner", "FakeMod", api_base=f"http://127.0.0.1:{self.port}",
                           cache_dir=self.tmp / "cache_ttl", token="",
                           ttl_hours=6, exclude_regex="sources")
        info1 = src.check("0.8.0")
        info2 = src.check("0.8.0")
        self.assertEqual(info2.latest_version, info1.latest_version)


class TestGitHubTagFilter(unittest.TestCase):
    """tag_regex：仓库混装多MC版本时只取匹配的最新版本（如 GTNH 版）。"""

    RELEASES = [
        {"tag_name": "1.0.2.1-1.21.1", "body": "其他MC版本",
         "assets": [{"name": "advanced_memory_card-1.0.2.1-1.21.1.jar",
                     "browser_download_url": "http://127.0.0.1:PORT/download/a.jar",
                     "size": 100}]},
        {"tag_name": "1.0.1-1.7.10-GTNH", "body": "GTNH版本",
         "assets": [{"name": "advanced_memory_card-1.0.1-1.7.10-GTNH.jar",
                     "browser_download_url": "http://127.0.0.1:PORT/download/b.jar",
                     "size": 100}]},
    ]

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_src3_"))
        cls.routes = {}
        cls.srv, cls.port = start_server(cls.routes)  # 共享同一个 dict，后续可加路由

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _route_releases(self, releases):
        body = json.dumps(releases).encode().replace(
            b"127.0.0.1:PORT", f"127.0.0.1:{self.port}".encode())
        self.routes[("GET", "/repos/owner/FakeMod/releases")] = (body, {})

    def _make(self, tag_regex):
        # 每个测试独立缓存目录，避免前一个用例的缓存干扰路由变化
        return GitHubSource("owner", "FakeMod", api_base=f"http://127.0.0.1:{self.port}",
                            cache_dir=self.tmp / self._testMethodName, ttl_hours=0,
                            tag_regex=tag_regex)

    def test_filtered_picks_gtnh_release(self):
        self._route_releases(self.RELEASES)
        src = self._make("GTNH")
        info = src.check("1.0.1-1.7.10-GTNH")
        self.assertEqual(info.latest_version, "1.0.1-1.7.10-GTNH")
        self.assertEqual(info.candidates[0].file_name,
                         "advanced_memory_card-1.0.1-1.7.10-GTNH.jar")

    def test_filtered_list_versions(self):
        self._route_releases(self.RELEASES)
        src = self._make("GTNH")
        options = src.list_versions(force=True)
        self.assertEqual([o.version for o in options], ["1.0.1-1.7.10-GTNH"])

    def test_filtered_no_match_falls_back_to_tags(self):
        # release 列表无匹配 → 退 tags 列表取带 GTNH 的最新 tag
        self._route_releases([self.RELEASES[0]])
        tags = [{"name": "1.0.2-1.7.10-GTNH"}, {"name": "0.9.0"}]
        self.routes[("GET", "/repos/owner/FakeMod/tags")] = (json.dumps(tags).encode(), {})
        rel = json.dumps({
            "tag_name": "1.0.2-1.7.10-GTNH", "body": "tags fallback",
            "assets": [{"name": "advanced_memory_card-1.0.2-1.7.10-GTNH.jar",
                        "browser_download_url": "http://127.0.0.1:PORT/download/c.jar",
                        "size": 100}]}).encode()
        self.routes[("GET", "/repos/owner/FakeMod/releases/tags/1.0.2-1.7.10-GTNH")] = (rel, {})
        src = self._make("GTNH")
        info = src.check("1.0.1-1.7.10-GTNH")
        self.assertEqual(info.latest_version, "1.0.2-1.7.10-GTNH")


class TestLocalFolder(unittest.TestCase):
    def test_local_latest(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_local_"))
        try:
            for v in ("0.1.0", "0.10.0", "0.9.0"):
                (tmp / f"SomeMod-1.7.10-{v}.jar").write_bytes(b"PK\x03\x04")
            (tmp / "README.md").write_text("x", encoding="utf-8")
            src = LocalFolderSource(str(tmp), name_regex=r"^SomeMod")
            info = src.check(None)
            self.assertEqual(info.latest_version, "0.10.0")  # 数值比较，0.10 > 0.9
            self.assertEqual(info.candidates[0].file_name, "SomeMod-1.7.10-0.10.0.jar")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_local_missing(self):
        src = LocalFolderSource("Z:/不存在")
        info = src.check(None)
        self.assertIsNone(info.latest_version)


class TestManual(unittest.TestCase):
    def test_manual(self):
        info = ManualSource().check("1.0.0")
        self.assertIsNone(info.latest_version)
        self.assertIsNone(info.candidates)


class TestVerifyAndUpdate(unittest.TestCase):
    def test_verify_ok(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_ver_"))
        try:
            p = tmp / "a.jar"
            p.write_bytes(JAR_BYTES)
            verify_jar(p)  # 不抛错
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_html(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_ver_"))
        try:
            p = tmp / "a.jar"
            p.write_bytes(b"<html><body>rate limit exceeded</body></html>")
            with self.assertRaises(VerifyError):
                verify_jar(p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_verify_bad_magic(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_ver_"))
        try:
            p = tmp / "a.jar"
            p.write_bytes(b"not a jar at all")
            with self.assertRaises(VerifyError):
                verify_jar(p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_update_with_backup(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_upd_"))
        try:
            mods = tmp / "mods"
            mods.mkdir()
            old = mods / "SomeMod-1.7.10-1.0.0.jar"
            old.write_bytes(b"PK\x03\x04old")
            # 本地"下载"：把新 jar 放进下载目录，candidate url 指向它
            dl = tmp / "dl"
            dl.mkdir()
            new_jar = dl / "SomeMod-1.7.10-1.1.0.jar"
            new_jar.write_bytes(JAR_BYTES)
            cand = DownloadCandidate(new_jar.as_uri(), new_jar.name)
            result = update_with_backup(cand, mods, tmp / "backup",
                                        old_file=old, backup_keep=2,
                                        dl_cache_dir=tmp / "dlcache")
            self.assertTrue(result.exists())
            self.assertFalse(old.exists())  # 旧文件已移除（防双jar）
            backups = list((tmp / "backup").glob("*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue(backups[0].name.endswith("SomeMod-1.7.10-1.0.0.jar"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
