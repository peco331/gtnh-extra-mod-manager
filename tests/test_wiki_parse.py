"""wiki.py 解析测试（离线，用真实 wikitext 样例）。"""
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod.wiki import (  # noqa: E402
    parse_side, parse_urls, parse_wikitext, strip_markup, github_repo_from_url, make_id,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wiki_sample.txt"


class TestWikiParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = FIXTURE.read_text(encoding="utf-8")
        cls.mods, cls.warnings = parse_wikitext(text)

    def test_total_count(self):
        # 调研基线：功能增强13 + 性能优化6 + 视听增强19 + 旧版本限定11 + 非星门规则30 = 79
        self.assertEqual(len(self.mods), 79)

    def test_category_counts(self):
        c = Counter(m["category"] for m in self.mods)
        self.assertEqual(c["功能增强"], 13)
        self.assertEqual(c["性能优化"], 6)
        self.assertEqual(c["视听增强"], 19)
        self.assertEqual(c["旧版本限定"], 11)
        self.assertEqual(c["非星门规则"], 30)

    def test_group_counts(self):
        c = Counter(m["group"] for m in self.mods)
        self.assertEqual(c["星门规则"], 49)
        self.assertEqual(c["非星门规则"], 30)

    def test_side_counts(self):
        c = Counter(m["side"] for m in self.mods)
        # 客户端 9+5+15+7=36；双端 3+1+1+4+4+30=43；无纯服务端
        self.assertEqual(c["client"], 36)
        self.assertEqual(c["both"], 43)
        self.assertEqual(c["server"], 0)

    def test_non_star_gate_all_both(self):
        for m in self.mods:
            if m["group"] == "非星门规则":
                self.assertEqual(m["side"], "both", m["name_en"])

    def test_uncertain_marked(self):
        # 功能增强里有一个 客户端（？）/服务端（？）
        uncertain = [m for m in self.mods if m["side_uncertain"]]
        self.assertTrue(uncertain)

    def test_source_types(self):
        c = Counter(m["source_type"] for m in self.mods)
        # 有 github 链接的 56 个；其余 curseforge 19（仅浏览器跳转）、manual 4
        self.assertEqual(c["github"], 56)
        self.assertEqual(c["curseforge"], 19)
        self.assertEqual(c["manual"], 4)

    def test_github_repo_extraction(self):
        # 每个 github 源都有 owner/repo
        for m in self.mods:
            if m["source_type"] == "github":
                self.assertTrue(m["source"]["owner"])
                self.assertTrue(m["source"]["repo"])

    def test_ids_unique(self):
        ids = [m["id"] for m in self.mods]
        self.assertEqual(len(ids), len(set(ids)))

    def test_names_present(self):
        named = [m for m in self.mods if m["name_en"]]
        self.assertEqual(len(named), 79)

    def test_known_sample(self):
        # 抽查具体条目
        by_id = {m["id"]: m for m in self.mods}
        inp = by_id.get("inputfix")
        self.assertIsNotNone(inp)
        self.assertEqual(inp["side"], "client")
        self.assertEqual(inp["category"], "功能增强")
        self.assertIsNotNone(inp["urls"]["curseforge"])
        ae2 = by_id.get("ae2-auto-pattern-upload")
        self.assertEqual(ae2["side"], "both")
        self.assertEqual(ae2["source"]["owner"] + "/" + ae2["source"]["repo"],
                         "GaLicn/AE2-Auto-Pattern-Upload")
        sf = by_id.get("smooth-font")
        self.assertIsNotNone(sf)  # <ref>标签应被剥掉，不影响id
        self.assertEqual(sf["name_en"], "Smooth Font")

    def test_preferred_download_link(self):
        # OmniOcular 的 "OO修复版(推荐使用该版)" 链接应成为默认下载源
        by_id = {m["id"]: m for m in self.mods}
        om = by_id.get("omniocular")
        self.assertIsNotNone(om)
        self.assertIsNotNone(om["urls"]["preferred"])
        self.assertIn("Taskeren", om["urls"]["preferred"])
        self.assertEqual(om["source"]["owner"], "Taskeren")
        self.assertEqual(om["source"]["repo"], "OmniOcular-Unofficial")
        # WorldEdit 默认仍是 GTNH特供版
        we = by_id.get("worldedit")
        self.assertEqual(we["source"]["repo"], "worldedit-gtnh")

    def test_no_warnings_for_known_sections(self):
        # LiteLoader/模组文件夹 无模板条目，不应产生警告
        self.assertEqual(self.warnings, [])


class TestFetchFallback(unittest.TestCase):
    """api.php 被限流时自动切换备用通道（curl/raw/本地缓存）。"""

    def _cfg(self):
        import tempfile
        from types import SimpleNamespace
        return SimpleNamespace(proxy=None,
                               wiki_url="https://gtnh.huijiwiki.com/api.php",
                               wiki_page="可添加MOD",
                               data_dir=Path(tempfile.mkdtemp(prefix="gtnh_wf_")))

    @staticmethod
    def _no_curl():
        """测试中禁用真实 curl 通道（避免打到真实网络）。"""
        import gtnhmod.wiki as W
        return mock.patch.object(W, "_fetch_curl_wikitext",
                                 side_effect=W.net.HttpError(-1, "no curl"))

    def test_fallback_to_raw_on_403(self):
        import gtnhmod.wiki as W
        calls = []

        def fake_get(url, *, headers=None, timeout=30, retries=2, binary=False, proxy=None):
            calls.append(url)
            if "api.php" in url:
                raise W.net.HttpError(403, "Forbidden")
            return "== 星门规则模组 ==\nok"

        with mock.patch.object(W.net, "http_get", side_effect=fake_get), \
                mock.patch.object(W.time, "sleep"), self._no_curl():
            text, cached = W.fetch_wikitext(self._cfg())
        self.assertEqual(text, "== 星门规则模组 ==\nok")
        self.assertIsNone(cached)
        self.assertTrue(any("action=raw" in c for c in calls))

    def test_fallback_on_bad_json(self):
        import gtnhmod.wiki as W
        calls = []

        def fake_get(url, *, headers=None, timeout=30, retries=2, binary=False, proxy=None):
            calls.append(url)
            if "api.php" in url:
                return "<html>blocked page</html>"  # 200但非JSON
            return "raw content"

        with mock.patch.object(W.net, "http_get", side_effect=fake_get), \
                mock.patch.object(W.time, "sleep"), self._no_curl():
            text, cached = W.fetch_wikitext(self._cfg())
        self.assertEqual(text, "raw content")

    def test_all_fail_uses_cache(self):
        import gtnhmod.wiki as W
        cfg = self._cfg()
        cache = W._wiki_cache_file(cfg)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("cached wikitext", encoding="utf-8")

        def fake_get(url, *, headers=None, timeout=30, retries=2, binary=False, proxy=None):
            raise W.net.HttpError(403, "Forbidden")

        with mock.patch.object(W.net, "http_get", side_effect=fake_get), \
                mock.patch.object(W.time, "sleep"), self._no_curl():
            text, cached = W.fetch_wikitext(cfg)
        self.assertEqual(text, "cached wikitext")
        self.assertIn("限流", cached)

    def test_all_fail_raises_friendly(self):
        import gtnhmod.wiki as W

        def fake_get(url, *, headers=None, timeout=30, retries=2, binary=False, proxy=None):
            raise W.net.HttpError(403, "Forbidden")

        with mock.patch.object(W.net, "http_get", side_effect=fake_get), \
                mock.patch.object(W.time, "sleep"), self._no_curl():
            with self.assertRaises(W.net.HttpError) as ctx:
                W.fetch_wikitext(self._cfg())
        self.assertIn("限流", str(ctx.exception))


class TestHelpers(unittest.TestCase):
    def test_parse_side(self):
        self.assertEqual(parse_side("客户端")[0], "client")
        self.assertEqual(parse_side("客户端<br>服务端")[0], "both")
        self.assertEqual(parse_side("服务端")[0], "server")
        self.assertEqual(parse_side("")[0], "both")  # 默认双端
        side, unc = parse_side("客户端（？）<br>服务端（？）")
        self.assertEqual(side, "both")
        self.assertTrue(unc)

    def test_parse_urls(self):
        urls = parse_urls("[https://github.com/asdflj/NeverEnoughCharacters-Rework github]<br>"
                          "[https://www.curseforge.com/minecraft/mc-mods/inputfix Curseforge]")
        self.assertEqual(urls["github"], "https://github.com/asdflj/NeverEnoughCharacters-Rework")
        self.assertEqual(urls["curseforge"], "https://www.curseforge.com/minecraft/mc-mods/inputfix")
        self.assertEqual(len(urls["links"]), 2)

    def test_parse_urls_keeps_all_links(self):
        # WorldEdit 有多个github链接，全部保留（含标签）
        raw = ("[https://github.com/GTNewHorizons/worldedit-gtnh GTNH特供版-github]<br>"
               "[https://github.com/enginehub/WorldEdit Github]<br>"
               "[https://www.curseforge.com/minecraft/mc-mods/worldedit Curseforge]")
        urls = parse_urls(raw)
        self.assertEqual(len(urls["links"]), 3)
        self.assertEqual(urls["links"][0]["label"], "GTNH特供版-github")
        self.assertEqual(urls["links"][1]["url"], "https://github.com/enginehub/WorldEdit")

    def test_github_repo(self):
        self.assertEqual(github_repo_from_url("https://github.com/Nxer/Twist-Space-Technology-Mod/releases"),
                         ("Nxer", "Twist-Space-Technology-Mod"))
        self.assertEqual(github_repo_from_url("https://github.com/wohaopa/OmniOcular-Unofficial"),
                         ("wohaopa", "OmniOcular-Unofficial"))

    def test_strip_markup(self):
        self.assertEqual(strip_markup("{{label|中文环境必备模组|type=danger}}<br>仅在使用Java8时安装。"),
                         "仅在使用Java8时安装。")

    def test_make_id(self):
        self.assertEqual(make_id("NeverEnoughCharacters-Rework", "NEI拼音搜索"), "neverenoughcharacters-rework")
        self.assertTrue(make_id("", "中文名").startswith("mod-"))


if __name__ == "__main__":
    unittest.main()
