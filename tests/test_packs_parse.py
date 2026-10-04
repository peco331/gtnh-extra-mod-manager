"""「资源包与光影」页面解析回归。

fixture ``packs_shader_sample.txt`` 是 2026 年从 gtnh.huijiwiki.com 抓取的
该页面真实 wikitext（含 Cloudflare 通过后的原始正文），因此断言可以覆盖
真实表格写法：图标列里的 ``[[File:…|link=…]]``、空单元格导致的列错位、
``{{Label|2.8.0以下}}`` 版本门槛、``<br>`` 换行以及多行说明。
"""
import unittest
from pathlib import Path

from gtnhmod import packs_wiki as W

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "packs_shader_sample.txt"


def load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class TestPacksValidation(unittest.TestCase):
    def test_rejects_cloudflare_challenge(self):
        with self.assertRaises(W.net.HttpError):
            W.validate_packs_wikitext("<html>Just a moment...</html>")

    def test_rejects_cf_challenge_marker(self):
        with self.assertRaises(W.net.HttpError):
            W.validate_packs_wikitext("var x = window._cf_chl_opt = {};")

    def test_rejects_empty(self):
        with self.assertRaises(W.net.HttpError):
            W.validate_packs_wikitext("")

    def test_rejects_mods_page_text(self):
        """抓到的是「可添加MOD」页面时必须拒绝（不是本页正文）。"""
        with self.assertRaises(W.net.HttpError):
            W.validate_packs_wikitext("== 星门规则模组 ==\n{{可添加MOD表格行|模组英文名=X}}")

    def test_accepts_real_wikitext(self):
        W.validate_packs_wikitext(load_fixture())   # 不抛异常即通过

    def test_profile_points_at_pack_page(self):
        self.assertEqual(W.PACKS_PROFILE.page, "资源包与光影")
        self.assertEqual(W.PACKS_PROFILE.validate, W.validate_packs_wikitext)
        self.assertEqual(W.PACKS_PROFILE.cache_name, "packs_wikitext.txt")


class TestCellSplitting(unittest.TestCase):
    """单元格切分必须忽略 [[File:…|64px|link=…]] 里的竖线。"""

    def test_file_link_pipes_do_not_split(self):
        raw = ("|[[File:pack-Fox.png|64px|frameless|center|alt=Icon|"
               "link=https://github.com/DylanTaylor1/GTNH-ResourcePack]]\n"
               "|BetterClick\n|Fox\n|说明文字\n|\n|"
               "[https://github.com/DylanTaylor1/GTNH-ResourcePack/releases 下载]")
        cells = W.split_table_cells(raw)
        self.assertEqual(len(cells), 6)
        self.assertTrue(cells[0].startswith("[[File:pack-Fox.png"))
        self.assertEqual(cells[1], "BetterClick")
        self.assertIn("link=https://github.com", cells[0])

    def test_skips_row_and_table_markers(self):
        raw = "|-\n|A\n|-\n|B\n|}"
        self.assertEqual(W.split_table_cells(raw), ["A", "B"])

    def test_empty_cell_is_preserved(self):
        cells = W.split_table_cells("|A\n|\n|C")
        self.assertEqual(cells, ["A", "", "C"])

    def test_single_cell(self):
        self.assertEqual(W.split_table_cells("|只有一格"), ["只有一格"])


class TestCellMarkup(unittest.TestCase):
    def test_version_label_keeps_version(self):
        self.assertEqual(W.strip_cell_markup("{{Label|2.8.0以下}}一个简单的资源包"),
                         "2.8.0以下一个简单的资源包")

    def test_br_becomes_newline(self):
        self.assertEqual(W.strip_cell_markup("第一行<br>第二行"), "第一行\n第二行")

    def test_external_link_label_kept(self):
        self.assertEqual(
            W.strip_cell_markup("[https://github.com/a/b/releases 下载]"), "下载")

    def test_file_link_removed(self):
        self.assertEqual(
            W.strip_cell_markup("[[File:pack-X.png|64px|link=https://x.test]]名字"), "名字")

    def test_internal_link_keeps_target_text(self):
        self.assertEqual(
            W.strip_cell_markup("请参阅[[可添加MOD#你的模组文件夹|此页面]]"), "请参阅此页面")

    def test_ref_removed(self):
        self.assertEqual(
            W.strip_cell_markup("说明<ref>参见其官网[https://x.test 此页面]</ref>。"), "说明。")

    def test_bold_and_item_templates(self):
        self.assertEqual(W.strip_cell_markup("'''Fast Leaves Fix'''：说明"), "Fast Leaves Fix：说明")
        self.assertEqual(W.strip_cell_markup("{{item|弧光灯}}冲突"), "弧光灯冲突")

    def test_html_tag_removed_but_text_kept(self):
        self.assertEqual(W.strip_cell_markup('点击<code>shaderpacks</code>目录'),
                         "点击shaderpacks目录")


class TestVersionRequirements(unittest.TestCase):
    def test_below_relation(self):
        self.assertEqual(W.version_requirements("2.8.0以下，不适用"),
                         [{"version": "2.8.0", "relation": "below"}])

    def test_above_relation(self):
        self.assertEqual(W.version_requirements("2.8.0以上"),
                         [{"version": "2.8.0", "relation": "above"}])

    def test_plain_version_is_compatible(self):
        self.assertEqual(W.version_requirements("适用于 2.8.4"),
                         [{"version": "2.8.4", "relation": "compatible"}])

    def test_no_version(self):
        self.assertEqual(W.version_requirements("某些说明"), [])


class TestPackLinks(unittest.TestCase):
    def test_classifies_and_dedupes(self):
        cell = ("[https://github.com/Ranzuu/Shadow-UI/releases 下载]<br>"
                "[https://github.com/Ranzuu/Shadow-UI 源码]")
        info = W.pack_links(cell, "[[File:x.png|64px|link=https://github.com/Ranzuu/Shadow-UI]]")
        self.assertEqual(info["github"], "https://github.com/Ranzuu/Shadow-UI/releases")
        self.assertIn("https://github.com/Ranzuu/Shadow-UI", info["direct"])
        # 图标 link= 与正文链接重复时不重复登记
        urls = [x["url"] for x in info["links"]]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(info["primary"], urls[0])

    def test_curseforge_is_not_direct(self):
        info = W.pack_links(
            "[https://www.curseforge.com/minecraft/texture-packs/zedtech-gtnh Curseforge]", "")
        self.assertEqual(info["curseforge"],
                         "https://www.curseforge.com/minecraft/texture-packs/zedtech-gtnh")
        self.assertEqual(info["direct"], [])

    def test_modrinth_classified(self):
        info = W.pack_links("[https://modrinth.com/shader/complementary-unbound modrinth]", "")
        self.assertEqual(info["modrinth"], "https://modrinth.com/shader/complementary-unbound")
        self.assertEqual(info["direct"], ["https://modrinth.com/shader/complementary-unbound"])

    def test_modrinth_ref(self):
        self.assertEqual(W.modrinth_ref("https://modrinth.com/shader/bsl-shaders/versions"),
                         ("shader", "bsl-shaders"))
        self.assertEqual(W.modrinth_ref("https://modrinth.com/mod/euphoria-patches"),
                         ("mod", "euphoria-patches"))
        self.assertIsNone(W.modrinth_ref("https://github.com/a/b"))

    def test_icon_extraction(self):
        self.assertEqual(W.extract_icon("[[File:pack-Fox.png|64px|link=https://x]]"), "pack-Fox.png")
        self.assertIsNone(W.extract_icon("没有图片"))

    def test_manual_hosts_note(self):
        self.assertTrue(W.net is not None)     # 占位：确保模块导入路径正确


class TestPackIds(unittest.TestCase):
    def test_id_is_prefixed_by_kind(self):
        self.assertEqual(W.pack_id("Shadow UI", W.KIND_RESOURCE), "pack-shadow-ui")
        self.assertEqual(W.pack_id("BSL Shaders", W.KIND_SHADER), "shader-bsl-shaders")

    def test_same_name_different_kind_no_clash(self):
        self.assertNotEqual(W.pack_id("X Pack", W.KIND_RESOURCE),
                            W.pack_id("X Pack", W.KIND_SHADER))

    def test_non_ascii_name_hashed(self):
        got = W.pack_id("", W.KIND_RESOURCE)
        self.assertTrue(got.startswith("pack-"))
        self.assertEqual(got, W.pack_id("", W.KIND_RESOURCE))


class TestRealPageParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries, cls.warnings = W.parse_packs_wikitext(load_fixture())
        cls.by_name = {e["name_en"]: e for e in cls.entries}

    def test_no_warnings(self):
        self.assertEqual(self.warnings, [])

    def test_counts_by_kind(self):
        kinds = {}
        for e in self.entries:
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        # 26 个可下载资源包 + 8 个整合包自带；5 个光影包
        self.assertEqual(kinds[W.KIND_RESOURCE], 34)
        self.assertEqual(kinds[W.KIND_SHADER], 5)
        self.assertEqual(len(self.entries), 39)

    def test_every_entry_has_id_and_kind(self):
        for e in self.entries:
            self.assertTrue(e["id"])
            self.assertIn(e["kind"], (W.KIND_RESOURCE, W.KIND_SHADER))
            self.assertTrue(e["name_en"])

    def test_no_section_text_leaked_as_entry(self):
        """「摘要」「安装方法」「光影加载器」等说明小节不能变成条目。"""
        for bad in ("摘要", "安装方法", "光影加载器", "推荐列表", "资源包", "光影"):
            self.assertNotIn(bad, self.by_name)

    def test_resource_categories(self):
        cats = W.parse_packs_wikitext(load_fixture())[0]
        got = {e["category"] for e in cats if e["kind"] == W.KIND_RESOURCE}
        self.assertEqual(got, {"无障碍与实用性", "深色模式", "升级与重制", "游戏自带资源包"})

    def test_shader_category(self):
        got = {e["category"] for e in self.entries if e["kind"] == W.KIND_SHADER}
        self.assertEqual(got, {"推荐列表"})

    def test_builtin_packs_are_readonly(self):
        builtin = [e for e in self.entries if e["category"] == "游戏自带资源包"]
        self.assertEqual(len(builtin), 8)
        for e in builtin:
            self.assertTrue(e["local"])
            self.assertEqual(e["urls"]["links"], [])
            self.assertEqual(e["urls"]["direct"], [])

    def test_builtin_description_parsed(self):
        e = self.by_name["Fast Leaves Fix"]
        self.assertIn("树叶", e["desc"])

    def test_builtin_version_requirement(self):
        e = self.by_name["Legacy Coin Textures"]
        self.assertEqual(e["version_requirements"],
                         [{"version": "2.9.0", "relation": "below"}])

    def test_author_and_desc_alignment(self):
        """范围/说明列在页面里位置不一致，但作者列必须准确。"""
        e = self.by_name["BetterClick"]
        self.assertEqual(e["author"], "Fox")
        self.assertEqual(e["scope"], "")
        self.assertIn("点击声音", e["desc"])

    def test_short_row_desc_not_lost(self):
        """少写空单元格的行（说明左移到范围列）不能丢掉简介。"""
        e = self.by_name["GTNH Colored Machines"]
        self.assertIn("GT6", e["scope"])
        self.assertIn("部分指定的材质", e["desc"])

    def test_row_with_empty_scope_keeps_both(self):
        """说明列有内容、范围列为空的行（BetterClick）说明不能丢。"""
        e = self.by_name["GTNH Machine Textures"]
        self.assertIn("GT++", e["scope"])
        self.assertIn("GT5", e["desc"])

    def test_shader_loader_column(self):
        """光影表第 4 列是「适用加载器」，不能读成范围/说明。"""
        e = self.by_name["BSL Shaders"]
        self.assertEqual(e["loader"], "Angelica\nOptifine")
        self.assertEqual(e["scope"], "")
        e2 = self.by_name["Sildur's Vibrant shaders"]
        self.assertEqual(e2["loader"], "Optifine")
        self.assertIn("推荐设置", e2["desc"])

    def test_shader_loader_has_no_list_markers(self):
        for e in self.entries:
            self.assertNotIn("*", e["loader"])

    def test_shader_desc_keeps_bullets(self):
        e = self.by_name["SEUS 11.0"]
        self.assertIn("composite1.vsh", e["desc"])

    def test_github_link_detected(self):
        e = self.by_name["Shadow UI"]
        self.assertEqual(e["urls"]["github"], "https://github.com/Ranzuu/Shadow-UI/releases")
        self.assertTrue(e["urls"]["direct"])

    def test_curseforge_only_entry_has_no_direct(self):
        e = self.by_name["Zedtech-GTNH"]
        self.assertIsNone(e["urls"]["github"])
        self.assertTrue(e["urls"]["curseforge"])
        self.assertEqual(e["urls"]["direct"], [])

    def test_modrinth_shader_detected(self):
        e = self.by_name["Complementary"]
        self.assertEqual(e["urls"]["modrinth"],
                         "https://modrinth.com/shader/complementary-unbound")
        self.assertTrue(e["urls"]["direct"])

    def test_discord_only_entry(self):
        e = self.by_name["Outlined Ores"]
        self.assertEqual(e["urls"]["direct"], [])
        self.assertTrue(e["urls"]["links"])
        self.assertIn("discord.com", e["urls"]["primary"])

    def test_gitlab_entry_has_no_direct(self):
        e = self.by_name["Ivelitex"]
        self.assertEqual(e["urls"]["direct"], [])

    def test_icon_recorded(self):
        self.assertEqual(self.by_name["BetterClick"]["icon"], "pack-Fox.png")

    def test_icon_link_discord_becomes_link(self):
        """图标列的 link= 是 Discord 帖子时也要作为可用链接保留。"""
        e = self.by_name["Outlined Ores"]
        self.assertTrue(any("discord.com" in l["url"] for l in e["urls"]["links"]))

    def test_ids_unique(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_names_have_no_line_breaks(self):
        for e in self.entries:
            self.assertNotIn("\n", e["name_en"])

    def test_multiline_author_kept_on_one_line(self):
        e = self.by_name["Minimalist Technology"]
        self.assertNotIn("\n", e["author"])
        self.assertIn("Fogy", e["author"])

    def test_detail_combines_scope_and_desc(self):
        e = self.by_name["Modern Outlined Ores"]
        self.assertIn("Modernity", e["detail"])

    def test_version_requirement_from_label(self):
        e = self.by_name["Colored GregTech Screwdriver and File"]
        rels = {r["relation"] for r in e["version_requirements"]}
        self.assertIn("below", rels)


class TestEmptyAndDegenerate(unittest.TestCase):
    def test_empty_page_reports_warning(self):
        entries, warnings = W.parse_packs_wikitext("== 资源包 ==\n== 光影 ==\n")
        self.assertEqual(entries, [])
        self.assertTrue(warnings)

    def test_table_without_name_column_ignored(self):
        text = ("== 资源包 ==\n=== 某节 ===\n"
                "{| class=\"wikitable\"\n! 图标 !! 说明\n|-\n|x\n|y\n|}\n")
        entries, _ = W.parse_packs_wikitext(text)
        self.assertEqual(entries, [])

    def test_unknown_subsection_becomes_category(self):
        text = ("== 资源包 ==\n=== 新分类 ===\n"
                "{| class=\"wikitable\"\n! 名字\n! 链接\n|-\n|新包\n|"
                "[https://github.com/a/b/releases 下载]\n|}\n== 光影 ==\n")
        entries, warnings = W.parse_packs_wikitext(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "新分类")
        self.assertEqual(warnings, [])

    def test_nested_table_inside_cell_does_not_break(self):
        """单元格里出现嵌套表格时必须整段保留，不能把内层表尾当外层的结束。"""
        text = ("== 资源包 ==\n=== 深色模式 ===\n"
                "{| class=\"wikitable\"\n! 名字\n! 说明\n|-\n|包A\n|外说明\n"
                "{| class=\"inner\"\n|-\n|内表格\n|}\n|}\n== 光影 ==\n")
        entries, _ = W.parse_packs_wikitext(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name_en"], "包A")


class TestCacheHelpers(unittest.TestCase):
    def _cfg(self):
        import tempfile
        from types import SimpleNamespace
        return SimpleNamespace(data_dir=Path(tempfile.mkdtemp(prefix="gtnh_pk_")))

    def test_write_then_read_roundtrip(self):
        cfg = self._cfg()
        text = load_fixture()
        W.write_packs_cache(cfg, text)
        self.assertEqual(W.read_packs_cache(cfg), text)

    def test_read_rejects_corrupt_cache(self):
        cfg = self._cfg()
        path = W.packs_cache_file(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>Just a moment...</html>", encoding="utf-8")
        self.assertIsNone(W.read_packs_cache(cfg))

    def test_read_missing_cache(self):
        self.assertIsNone(W.read_packs_cache(self._cfg()))

    def test_import_parses_and_caches(self):
        cfg = self._cfg()
        entries, warnings, text_hash = W.import_packs_text(cfg, load_fixture())
        self.assertEqual(len(entries), 39)
        self.assertEqual(warnings, [])
        self.assertEqual(len(text_hash), 64)
        self.assertEqual(W.read_packs_cache(cfg), load_fixture())

    def test_cache_file_is_not_mods_cache(self):
        cfg = self._cfg()
        self.assertEqual(W.packs_cache_file(cfg).name, "packs_wikitext.txt")


if __name__ == "__main__":
    unittest.main()
