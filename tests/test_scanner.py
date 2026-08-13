"""scanner.py 测试：假 mods 目录扫描与匹配。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod import db as dbmod  # noqa: E402
from gtnhmod.installed import InstalledDB  # noqa: E402
from gtnhmod.scanner import match_all, match_db, reconcile, scan_folder  # noqa: E402
from gtnhmod.wiki import parse_wikitext  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wiki_sample.txt"

# 手工条目（覆盖三档匹配）
ENTRIES = [
    {"id": "foamfix", "name_en": "FoamFix", "name_cn": "", "aliases": []},
    {"id": "foamfix-animations", "name_en": "FoamFix-Animations", "name_cn": "", "aliases": []},
    {"id": "nec-rework", "name_en": "NeverEnoughCharacters-Rework", "name_cn": "NEI拼音搜索", "aliases": []},
    {"id": "some-mod", "name_en": "SomeMod", "name_cn": "", "aliases": ["MyAlias"]},
]


class TestScanFolder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_scan_"))
        files = [
            "GardenOfGlass-1.7.10-1.9.5.jar",
            "NeverEnoughCharacters-Rework-1.7.10-2.0.0.jar.disabled",
            "SomeMod-1.2.3.jar",
            "FoamFix-Animations-1.7.10-0.4.1.jar",
            "core-mod.jar",              # 未受管
            "readme.txt",                # 非jar忽略
            "notes.jar.bak",             # 非.jar结尾忽略
        ]
        for name in files:
            (cls.tmp / name).write_bytes(b"PK\x03\x04test")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_scan(self):
        files = scan_folder(self.tmp)
        self.assertEqual(len(files), 5)  # readme.txt / notes.jar.bak 被忽略
        by_name = {f.file_name: f for f in files}
        f = by_name["GardenOfGlass-1.7.10-1.9.5.jar"]
        self.assertTrue(f.enabled)
        self.assertEqual(f.version, "1.9.5")
        d = by_name["NeverEnoughCharacters-Rework-1.7.10-2.0.0.jar.disabled"]
        self.assertFalse(d.enabled)
        self.assertEqual(d.name_part, "NeverEnoughCharacters-Rework")
        self.assertEqual(d.version, "2.0.0")

    def test_scan_empty(self):
        self.assertEqual(scan_folder(Path("Z:/不存在的目录")), [])


class TestMatch(unittest.TestCase):
    def test_exact(self):
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "NeverEnoughCharacters-Rework", "1.7.10", "2.0.0")
        self.assertEqual(match_db(f, ENTRIES), ("nec-rework", "exact"))

    def test_exact_alias(self):
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "MyAlias", None, "1.0.0")
        self.assertEqual(match_db(f, ENTRIES), ("some-mod", "exact"))

    def test_longest_prefix_wins(self):
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "FoamFix-Animations", "1.7.10", "0.4.1")
        self.assertEqual(match_db(f, ENTRIES), ("foamfix-animations", "exact"))
        # 名字段更长时仍应最长前缀命中 foamfix-animations 而非 foamfix
        f2 = InstalledFile(Path("x.jar"), "x.jar", True, "FoamFix-Animations-Extra", None, None)
        self.assertEqual(match_db(f2, ENTRIES), ("foamfix-animations", "prefix"))

    def test_abbrev(self):
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "NEC-Rework", None, "1.0.0")
        # NEC-Rework → 缩写命中 NeverEnoughCharacters-Rework
        self.assertEqual(match_db(f, ENTRIES)[0], "nec-rework")

    def test_no_match(self):
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "Totally-Unknown", None, "1.0.0")
        self.assertEqual(match_db(f, ENTRIES), (None, "none"))


class TestReconcile(unittest.TestCase):
    def test_reconcile_cleanup(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_recon_"))
        try:
            mods_dir = tmp / "mods"
            mods_dir.mkdir()
            (mods_dir / "SomeMod-1.2.3.jar").write_bytes(b"PK\x03\x04")
            inst = InstalledDB(tmp / "installed.json")
            # 预置一条已不存在的记录 + 一条存在的记录
            inst.set("client", "ghost", file_name="Ghost-1.0.0.jar", parsed_version="1.0.0")
            inst.set("client", "some-mod", file_name="SomeMod-1.0.0.jar", parsed_version="1.0.0")
            scan = {"client": scan_folder(mods_dir)}
            match_all(scan["client"], ENTRIES)
            live = reconcile(scan, inst)
            self.assertIn("some-mod", live["client"])
            self.assertIsNone(inst.get("client", "ghost"))
            # 磁盘为准：文件版本被校正
            self.assertEqual(inst.get("client", "some-mod")["parsed_version"], "1.2.3")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMatchRealDb(unittest.TestCase):
    """用真实 wiki 数据跑一遍匹配，检查会不会大量误配。"""

    @classmethod
    def setUpClass(cls):
        mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
        cls.entries = mods

    def test_real_jar_names(self):
        from gtnhmod.scanner import InstalledFile
        cases = {
            "NeverEnoughCharacters-Rework-1.7.10-2.0.0": "neverenoughcharacters-rework",
            "AE2-Auto-Pattern-Upload-1.7.10-1.0.0": "ae2-auto-pattern-upload",
            "Untranslator-1.7.10-1.0.0": "untranslator",
        }
        for stem, want in cases.items():
            f = InstalledFile(Path(stem + ".jar"), stem + ".jar", True, None, None, None)
            from gtnhmod.versions import split_mc_mod_version
            f.name_part, f.mc_version, f.version = split_mc_mod_version(stem)
            got, quality = match_db(f, self.entries)
            self.assertEqual(got, want, f"{stem} → {got}({quality})")

    def test_nei_core_mods_not_mismatched(self):
        # 整合包自带 NEI* mod 不应被中文名残片"nei"误配到 Not Enough Characters
        from gtnhmod.scanner import InstalledFile
        from gtnhmod.versions import split_mc_mod_version
        for stem in ("NEIAddons-1.18.1", "NEICustomDiagram-1.8.29", "NEIIntegration-1.5.3"):
            f = InstalledFile(Path(stem + ".jar"), stem + ".jar", True, None, None, None)
            f.name_part, f.mc_version, f.version = split_mc_mod_version(stem)
            got, quality = match_db(f, self.entries)
            self.assertIsNone(got, f"{stem} 不应被匹配 → {got}({quality})")

    def test_short_jar_name_not_abbrev_matched(self):
        # 短名字（如核心mod NEI）不应缩写匹配到 NEI-RecipeTree
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("NEI-1.7.10-2.5.jar"), "NEI-1.7.10-2.5.jar", True,
                          "NEI", "1.7.10", "2.5")
        got, quality = match_db(f, self.entries)
        self.assertIsNone(got, f"NEI 不应被匹配 → {got}({quality})")

    def test_name_cn_not_used_for_matching(self):
        # 中文名不参与匹配（"NEI 拼音搜索"归一化后是"nei"，会误配 NEI* mod）
        from gtnhmod.scanner import InstalledFile
        f = InstalledFile(Path("x.jar"), "x.jar", True, "NEI 拼音搜索", None, "1.0.0")
        got, quality = match_db(f, self.entries)
        self.assertIsNone(got, f"中文名不应参与匹配 → {got}({quality})")


if __name__ == "__main__":
    unittest.main()
