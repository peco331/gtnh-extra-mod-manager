"""资源包/光影管理回归：目录识别、扫描匹配、下载源解析、安装与更新、备份。

全部离线：GitHub/Modrinth 响应用合成 JSON 打桩，文件操作在 tmp 目录里进行，
不读取或改动真实游戏目录。
"""
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from gtnhmod import net, packs
from gtnhmod import packs_wiki as W
from gtnhmod.config import Config, detect_instance_paths


def make_cfg(data_dir: Path) -> Config:
    cfg = Config(data_dir)
    cfg.data["data_dir"] = str(data_dir)
    return cfg


def fake_entry(pack_id="pack-demo", kind=W.KIND_RESOURCE, name="Demo Pack", **kw):
    entry = {
        "id": pack_id, "kind": kind, "name_en": name, "category": "测试",
        "author": "", "scope": "", "loader": "", "desc": "", "detail": "",
        "version_requirements": [], "icon": None,
        "urls": {"links": [], "github": None, "modrinth": None,
                 "curseforge": None, "primary": None, "direct": []},
        "aliases": [], "local": False, "wiki_removed": False,
    }
    entry.update(kw)
    return entry


def make_zip(path: Path, entries) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name in entries:
            zf.writestr(name, "x")
    return path


class TestDirDetection(unittest.TestCase):
    def test_instance_root_from_mods_dir(self):
        self.assertEqual(packs.instance_root_from(Path("D:/inst/.minecraft/mods")),
                         Path("D:/inst/.minecraft"))

    def test_instance_root_from_resourcepacks(self):
        self.assertEqual(packs.instance_root_from(Path("D:/inst/resourcepacks")),
                         Path("D:/inst"))

    def test_instance_root_from_shaderpacks(self):
        self.assertEqual(packs.instance_root_from(Path("D:/inst/shaderpacks")),
                         Path("D:/inst"))

    def test_instance_root_none(self):
        self.assertIsNone(packs.instance_root_from(None))

    def test_resolve_from_client_mods_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp) / "data")
            base = Path(tmp) / "inst" / ".minecraft"
            (base / "mods").mkdir(parents=True)
            cfg.set_mods_dir("client", base / "mods")
            self.assertEqual(packs.resolve_pack_dir(cfg, W.KIND_RESOURCE),
                             base / "resourcepacks")
            self.assertEqual(packs.resolve_pack_dir(cfg, W.KIND_SHADER),
                             base / "shaderpacks")

    def test_explicit_setting_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp) / "data")
            cfg.set_mods_dir("client", Path(tmp) / "inst" / "mods")
            cfg.set_pack_dir(W.KIND_RESOURCE, Path(tmp) / "custom" / "rp")
            self.assertEqual(packs.resolve_pack_dir(cfg, W.KIND_RESOURCE),
                             Path(tmp) / "custom" / "rp")

    def test_resolve_returns_none_without_client_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp) / "data")
            self.assertIsNone(packs.resolve_pack_dir(cfg, W.KIND_RESOURCE))

    def test_detect_pack_dirs_from_instance_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "inst" / ".minecraft"
            (base / "resourcepacks").mkdir(parents=True)
            (base / "shaderpacks").mkdir(parents=True)
            got = packs.detect_pack_dirs(Path(tmp) / "inst")
            self.assertEqual(got["resourcepacks"], base / "resourcepacks")
            self.assertEqual(got["shaderpacks"], base / "shaderpacks")

    def test_detect_pack_dirs_from_pack_dir_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "inst" / "resourcepacks"
            target.mkdir(parents=True)
            got = packs.detect_pack_dirs(target)
            self.assertEqual(got["resourcepacks"], target)
            self.assertEqual(got["root"], Path(tmp) / "inst")

    def test_detect_pack_dirs_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            got = packs.detect_pack_dirs(Path(tmp))
            self.assertIsNone(got["resourcepacks"])


class TestConfigPackFolders(unittest.TestCase):
    def test_defaults_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp))
            self.assertEqual(cfg.pack_folders,
                             {"resourcepack": "", "shader": ""})
            self.assertIsNone(cfg.pack_dir("resourcepack"))

    def test_set_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            cfg = make_cfg(data)
            cfg.set_pack_dir("shader", Path(tmp) / "sp")
            reloaded = Config(data)
            self.assertEqual(reloaded.pack_dir("shader"), Path(tmp) / "sp")

    def test_instances_do_not_share_pack_folders(self):
        """深拷贝回归：两个 Config 实例不能共享同一个 pack_folders 字典。"""
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            a = Config(data)
            b = Config(data)
            a.pack_folders["resourcepack"] = "X"
            self.assertEqual(b.pack_folders["resourcepack"], "")

    def test_detect_instance_paths_reports_pack_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mods").mkdir()
            (root / "resourcepacks").mkdir()
            got = detect_instance_paths(root)
            self.assertEqual(got["resourcepacks"], root / "resourcepacks")
            self.assertIsNone(got["shaderpacks"])


class TestValidatePack(unittest.TestCase):
    def test_resourcepack_zip_with_mcmeta(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_zip(Path(tmp) / "a.zip", ["pack.mcmeta", "assets/x.png"])
            self.assertEqual(packs.validate_pack(p, W.KIND_RESOURCE), (True, ""))

    def test_resourcepack_zip_without_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_zip(Path(tmp) / "a.zip", ["readme.md"])
            ok, note = packs.validate_pack(p, W.KIND_RESOURCE)
            self.assertFalse(ok)
            self.assertIn("pack.mcmeta", note)

    def test_shader_zip_with_shaders_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_zip(Path(tmp) / "s.zip", ["shaders/gbuffers.vsh", "shaders.txt"])
            self.assertEqual(packs.validate_pack(p, W.KIND_SHADER), (True, ""))

    def test_shader_zip_wrapped_in_folder_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_zip(Path(tmp) / "s.zip", ["PackName/shaders/a.vsh"])
            ok, note = packs.validate_pack(p, W.KIND_SHADER)
            self.assertTrue(ok)
            self.assertIn("多了一层文件夹", note)

    def test_shader_zip_without_shaders(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = make_zip(Path(tmp) / "s.zip", ["assets/a.png"])
            ok, note = packs.validate_pack(p, W.KIND_SHADER)
            self.assertFalse(ok)
            self.assertIn("shaders", note)

    def test_non_zip_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.txt"
            p.write_text("x", encoding="utf-8")
            ok, note = packs.validate_pack(p, W.KIND_RESOURCE)
            self.assertFalse(ok)
            self.assertIn("zip", note)

    def test_folder_resourcepack(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "PackFolder"
            (d / "assets").mkdir(parents=True)
            self.assertEqual(packs.validate_pack(d, W.KIND_RESOURCE), (True, ""))

    def test_corrupt_zip_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.zip"
            p.write_bytes(b"not a zip at all")
            ok, _ = packs.validate_pack(p, W.KIND_RESOURCE)
            self.assertFalse(ok)


class TestScanPacks(unittest.TestCase):
    def _folder(self, tmp) -> Path:
        folder = Path(tmp) / "resourcepacks"
        folder.mkdir()
        make_zip(folder / "Shadow.UI.v5.45.zip", ["pack.mcmeta"])
        make_zip(folder / "GTNH-Faithful-x32.v2.2.0.zip", ["pack.mcmeta"])
        (folder / "SomePackFolder").mkdir()
        (folder / "SomePackFolder" / "pack.mcmeta").write_text("{}", encoding="utf-8")
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        (folder / ".hidden.zip").write_bytes(b"")
        (folder / "old.zip.disabled").write_bytes(b"")
        return folder

    def test_scans_zips_and_folders_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            files = packs.scan_pack_dir(folder, W.KIND_RESOURCE)
            names = {f.file_name for f in files}
            self.assertEqual(names, {"Shadow.UI.v5.45.zip",
                                     "GTNH-Faithful-x32.v2.2.0.zip",
                                     "SomePackFolder"})

    def test_version_extracted_from_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            files = {f.file_name: f for f in packs.scan_pack_dir(folder, W.KIND_RESOURCE)}
            self.assertEqual(files["Shadow.UI.v5.45.zip"].version, "v5.45")
            self.assertEqual(files["GTNH-Faithful-x32.v2.2.0.zip"].version, "v2.2.0")

    def test_folder_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            files = {f.file_name: f for f in packs.scan_pack_dir(folder, W.KIND_RESOURCE)}
            self.assertTrue(files["SomePackFolder"].is_dir)
            self.assertEqual(files["SomePackFolder"].ext, "folder")

    def test_missing_dir_returns_empty(self):
        self.assertEqual(packs.scan_pack_dir(Path("Z:/nope"), W.KIND_RESOURCE), [])

    def test_size_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._folder(tmp)
            files = packs.scan_pack_dir(folder, W.KIND_RESOURCE)
            self.assertTrue(all(f.size > 0 for f in files))


class TestMatchPack(unittest.TestCase):
    def _match(self, file_name, entries):
        return packs.match_pack(packs.PackFile(Path(file_name), file_name, False), entries)

    def test_exact_normalized_name(self):
        e = fake_entry(name="Shadow UI")
        self.assertEqual(self._match("ShadowUI.zip", [e]), ("pack-demo", "exact"))

    def test_exact_with_version_suffix(self):
        """去掉版本段后与条目名全等 → exact（Shadow.Ui.v5.45 的版本段是 v5.45）。"""
        e = fake_entry(name="Shadow UI")
        self.assertEqual(self._match("Shadow.UI.v5.45.zip", [e]), ("pack-demo", "exact"))

    def test_contains_with_version(self):
        """额外变体词存在时仍能匹配上（exact/contains 都算命中，不能是 none）。"""
        e = fake_entry(name="Shadow UI")
        pack_id, quality = self._match("Shadow.UI.v5.45-Modernity.zip", [e])
        self.assertEqual(pack_id, "pack-demo")
        self.assertIn(quality, ("exact", "contains"))

    def test_faithful_extra_words(self):
        """条目名多一个词（Textures）也应匹配到真实文件名。"""
        e = fake_entry(pack_id="pack-faithful", name="GTNH Faithful Textures")
        self.assertEqual(self._match("GTNH-Faithful-x32.v2.2.0-Reupload.zip", [e]),
                         ("pack-faithful", "contains"))

    def test_faithful_match(self):
        e = fake_entry(pack_id="pack-faithful", name="GTNH Faithful Textures")
        got = self._match("GTNH-Faithful-x32.v2.2.0.zip", [e])
        self.assertEqual(got[1], "contains")

    def test_alias_used(self):
        e = fake_entry(name="Complementary", aliases=["ComplementaryShaders"])
        self.assertEqual(self._match("ComplementaryShaders_v4.6.zip", [e]),
                         ("pack-demo", "exact"))

    def test_no_match(self):
        e = fake_entry(name="Shadow UI")
        self.assertEqual(self._match("TotallyDifferent.zip", [e]), (None, "none"))

    def test_short_name_not_matched(self):
        """过短的条目名不参与包含匹配，避免误配。"""
        e = fake_entry(name="UI")
        self.assertEqual(self._match("SomeUIPack.zip", [e]), (None, "none"))


class TestMatchPackOnRealNames(unittest.TestCase):
    """真实用户 resourcepacks/shaderpacks 目录里的命名回归。

    这批用例来自实际安装目录（GTNH 2.9.0-beta-3 Prism 实例）。修复前
    ``Modernity-GTNH-UI`` / ``Modernity-GTNH-Dark-UI`` / ``ModernityAdjunct-f1``
    会全部折叠到 ``Modernity-GTNH`` 一条记录，用户已装的
    ``GTNH-OutlinedOres.2.1.-.Modernity.version.zip`` 完全检测不出来。
    """

    ENTRIES = [
        ("pack-outlined-ores", "Outlined Ores"),
        ("pack-modern-outlined-ores", "Modern Outlined Ores"),
        ("pack-modernity-gtnh", "Modernity-GTNH"),
        ("pack-modernity-gtnh-dark-ui", "Modernity-GTNH-Dark-UI"),
        ("pack-fast-leaves-fix", "Fast Leaves Fix"),
    ]
    SHADERS = [
        ("shader-complementary", "Complementary"),
        ("shader-complementary-euphoria", "Complementary +Euphoria Patches"),
    ]

    def _entries(self, entries):
        return [fake_entry(pid, name=name) for pid, name in entries]

    def _match(self, file_name, entries):
        f = packs.PackFile(Path(file_name), file_name, not file_name.endswith(".zip"))
        return packs.match_pack(f, self._entries(entries))

    def test_outlined_ores_variant_detected(self):
        """用户装的 GTNH-OutlinedOres.2.1.-.Modernity.version.zip 必须被识别。"""
        got = self._match("GTNH-OutlinedOres.2.1.-.Modernity.version.zip", self.ENTRIES)
        self.assertEqual(got[0], "pack-outlined-ores")

    def test_addon_outlined_ores_goes_to_modern_variant(self):
        got = self._match("Modernity-GTNHAddon-OutlinedOres.zip", self.ENTRIES)
        self.assertEqual(got[0], "pack-modern-outlined-ores")

    def test_dark_ui_not_collapsed_into_main_pack(self):
        got = self._match("Modernity-GTNH-Dark-UI-2.9.X.zip", self.ENTRIES)
        self.assertEqual(got[0], "pack-modernity-gtnh-dark-ui")

    def test_main_and_ui_asset_go_to_main_pack(self):
        for name in ("Modernity-GTNH-2.9.X.zip", "Modernity-GTNH-UI-2.9.X.zip"):
            with self.subTest(name=name):
                self.assertEqual(self._match(name, self.ENTRIES)[0], "pack-modernity-gtnh")

    def test_unrelated_modernity_files_are_not_matched(self):
        """只命中 modernity 一个词的文件不能算作 Modernity-GTNH。"""
        for name in ("Modernity-f1-3.10.2.zip", "ModernityAdjunct-f1-1.6.zip"):
            with self.subTest(name=name):
                self.assertEqual(self._match(name, self.ENTRIES), (None, "none"))

    def test_complementary_variants_split(self):
        self.assertEqual(self._match("ComplementaryUnbound_r5.8.1.zip", self.SHADERS)[0],
                         "shader-complementary")
        self.assertEqual(
            self._match("ComplementaryUnbound_r5.8.1 + EuphoriaPatches_1.9.3",
                        self.SHADERS)[0],
            "shader-complementary-euphoria")

    def test_scan_and_reconcile_end_to_end(self):
        """整目录扫描后每个包落到不同条目（不能有两条记录互相顶替）。"""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "resourcepacks"
            folder.mkdir()
            for name in ("Modernity-GTNH-2.9.X.zip", "Modernity-GTNH-Dark-UI-2.9.X.zip",
                         "GTNH-OutlinedOres.2.1.-.Modernity.version.zip",
                         "Modernity-f1-3.10.2.zip"):
                make_zip(folder / name, ["pack.mcmeta"])
            cfg = make_cfg(Path(tmp) / "data")
            cfg.set_pack_dir(W.KIND_RESOURCE, folder)
            db = packs.PacksDB(Path(tmp) / "data" / "packs_db.json")
            db.merge_wiki(self._entries(self.ENTRIES))
            scan = packs.scan_pack_dir(folder, W.KIND_RESOURCE, with_size=False)
            matched = {f.file_name: packs.match_pack(f, db.by_kind())[0] for f in scan}
            self.assertEqual(matched["Modernity-GTNH-2.9.X.zip"], "pack-modernity-gtnh")
            self.assertEqual(matched["Modernity-GTNH-Dark-UI-2.9.X.zip"],
                             "pack-modernity-gtnh-dark-ui")
            self.assertEqual(matched["GTNH-OutlinedOres.2.1.-.Modernity.version.zip"],
                             "pack-outlined-ores")
            self.assertIsNone(matched["Modernity-f1-3.10.2.zip"])
            live = packs.reconcile_installed(cfg, db, kinds=[W.KIND_RESOURCE])
            self.assertEqual(len(live[W.KIND_RESOURCE]), 3)


class TestExtractPackVersion(unittest.TestCase):
    """文件名版本提取：真实 GTNH 资源包/光影包命名回归。"""

    CASES = {
        "Shadow.UI.v5.45": "v5.45",
        "GTNH-Faithful-x32.v2.2.0-Reupload": "v2.2.0",
        "DarkReimagined_2.0.1": "2.0.1",
        "Modernity-GTNH-Dark-UI-2026-09-28": "2026-09-28",
        "Shadow.UI.v5.45-Modernity.version": "v5.45",
        "BSL_v10.1.8": "v10.1.8",
        "AE2-Dark-Mode.v.1.22": "1.22",
        "Minimalistic-GTNH-repair-1.1.3": "1.1.3",
        "GTNH-OutlinedOres.2.1.-.Modernity.version": "2.1",
        # Modrinth 光影版本：带 r 前缀要整体取出（取成 9.3 会与上游 5.9.3 不可比）
        "ComplementaryUnbound_r5.9.3": "r5.9.3",
        # 空格分隔的版本（GTNH 自带资源包的真实命名）：不能从 1.0.0 内部起匹配成 0.0
        "Glow Lines Only Galacticraft Thermal Padding GT V1.0.0": "V1.0.0",
        "Hidden Galacticraft Thermal Padding GT V1.0.0": "V1.0.0",
        # 空格后的普通数字/分辨率不能算版本
        "Texture Pack 4K": None,
        "Some Pack 2": None,
        "Realistic Sky GT New Horizons": None,
        "USERNM.only.colored.machines": None,
        "BetterClick": None,
        "FoxTex": None,
    }

    def test_real_filenames(self):
        for name, want in self.CASES.items():
            with self.subTest(name=name):
                self.assertEqual(packs.extract_pack_version(name), want)

    def test_first_segment_wins_not_last(self):
        """取第一个匹配：取最后一个会把 DarkReimagined_2.0.1 读成 0.1。"""
        self.assertEqual(packs.extract_pack_version("DarkReimagined_2.0.1"), "2.0.1")
        self.assertEqual(packs.extract_pack_version("BSL_v10.1.8"), "v10.1.8")

    def test_resolution_fragment_is_not_version(self):
        """名字里的分辨率残片（x32/x64）不能当成版本段起点。"""
        self.assertEqual(packs.extract_pack_version("GTNH-Faithful-x32"), None)
        self.assertEqual(packs.extract_pack_version("Pack-x64"), None)
        # 有真版本号时不能被 x32 抢走
        self.assertEqual(packs.extract_pack_version("GTNH-Faithful-x32.v2.2.0"), "v2.2.0")

    def test_empty_input(self):
        self.assertIsNone(packs.extract_pack_version(""))
        self.assertIsNone(packs.extract_pack_version(None))


class TestVersionStatus(unittest.TestCase):
    def test_newer(self):
        self.assertEqual(packs.version_status("v5.44", "v5.45"), "newer")

    def test_same(self):
        self.assertEqual(packs.version_status("2.2.0", "2.2.0"), "same")

    def test_older(self):
        self.assertEqual(packs.version_status("2.3.0", "2.2.0"), "older")

    def test_unknown_without_remote(self):
        self.assertEqual(packs.version_status("1.0", None), "unknown")

    def test_no_local_means_unknown(self):
        self.assertEqual(packs.version_status(None, "1.0"), "unknown")

    def test_unparseable_is_unknown(self):
        self.assertEqual(packs.version_status("abc", "def"), "unknown")

    def test_non_version_tag_is_unknown(self):
        """上游把 Stable / machines / weekly-日期 当 tag 时不能当作版本比较。"""
        for tag in ("Stable", "machines", "weekly-2026-09-28"):
            with self.subTest(tag=tag):
                self.assertEqual(packs.version_status("v1.0", tag), "unknown")
                # 本地无版本、上游 tag 不是版本 → 也不能断言有新版
                self.assertEqual(packs.version_status(None, tag), "unknown")

    def test_both_sides_plain_number(self):
        self.assertTrue(packs._looks_like_version("1.0"))
        self.assertTrue(packs._looks_like_version("v5.45"))
        self.assertTrue(packs._looks_like_version("2.2.0-Reupload"))
        self.assertFalse(packs._looks_like_version(""))
        self.assertFalse(packs._looks_like_version("machines"))

    def test_unsupported_version_prefix_is_stripped_for_comparison(self):
        """Modrinth 的 r5.9.3 前缀是装饰：归一化后能与 5.9.3 比较。"""
        self.assertEqual(packs.normalize_version("r5.9.3"), "5.9.3")
        self.assertEqual(packs.normalize_version("v5.45"), "v5.45")
        self.assertEqual(packs.version_status("5.9.3", "r5.9.3"), "same")
        self.assertEqual(packs.version_status("5.9.2", "r5.9.3"), "newer")

    def test_prerelease_markers_are_not_stripped(self):
        """beta1/rc2 不能被当成装饰前缀拆掉。"""
        self.assertEqual(packs.normalize_version("beta1"), "beta1")
        self.assertEqual(packs.normalize_version("rc2"), "rc2")
        self.assertEqual(packs.version_status("1.0.0", "1.0.0-beta1"), "older")

    def test_normalize_version_none(self):
        self.assertIsNone(packs.normalize_version(""))
        self.assertIsNone(packs.normalize_version(None))


class TestHostRouting(unittest.TestCase):
    def test_github_is_auto(self):
        self.assertEqual(packs.host_note("https://github.com/a/b/releases"), "")

    def test_modrinth_is_auto(self):
        self.assertEqual(packs.host_note("https://modrinth.com/shader/x"), "")

    def test_curseforge_note(self):
        self.assertIn("API key", packs.host_note("https://www.curseforge.com/x"))

    def test_discord_note(self):
        self.assertIn("Discord", packs.host_note("https://discord.com/channels/1/2"))

    def test_unknown_host_note(self):
        self.assertIn("不支持自动下载", packs.host_note("https://example.invalid/x"))

    def test_entry_auto_url_prefers_github(self):
        e = fake_entry(urls={"links": [], "github": "https://github.com/a/b/releases",
                             "modrinth": "https://modrinth.com/shader/c",
                             "curseforge": None, "primary": None,
                             "direct": ["https://github.com/a/b/releases"]})
        self.assertEqual(packs.entry_auto_url(e), "https://github.com/a/b/releases")

    def test_entry_auto_url_none_when_only_curseforge(self):
        e = fake_entry(urls={"links": [], "github": None, "modrinth": None,
                             "curseforge": "https://www.curseforge.com/x",
                             "primary": "https://www.curseforge.com/x", "direct": []})
        self.assertIsNone(packs.entry_auto_url(e))

    def test_bound_source_respected(self):
        e = fake_entry(bound_source="https://modrinth.com/shader/zzz",
                       urls={"links": [], "github": "https://github.com/a/b/releases",
                             "modrinth": None, "curseforge": None, "primary": None,
                             "direct": []})
        self.assertEqual(packs.entry_auto_url(e), "https://modrinth.com/shader/zzz")


class TestAssetPicking(unittest.TestCase):
    def _assets(self, *names):
        return [packs.PackAsset(url=f"https://x/{n}", file_name=n,
                                version=n.rsplit(".", 1)[0], source="github")
                for n in names]

    def test_metadata_asset_ignored(self):
        e = fake_entry(name="Shadow UI")
        got = packs._pick_asset(self._assets("gtnh-pack-update.json", "Shadow.UI.v5.45.zip"), e)
        self.assertEqual(got.file_name, "Shadow.UI.v5.45.zip")

    def test_single_asset_chosen(self):
        e = fake_entry(name="Whatever")
        self.assertEqual(packs._pick_asset(self._assets("FoxTex.zip"), e).file_name,
                         "FoxTex.zip")

    def test_latest_release_group_wins_over_history(self):
        """Modrinth 会返回全部历史版本：必须收敛到最新一次发布，否则永远判歧义。

        真实回归：Complementary 有 25 个版本文件、BSL 有 49 个，旧逻辑只看
        "候选总数 > 1" 就返回 None，导致无法一键安装。
        """
        def ver(num, fname, published):
            return packs.PackAsset(url=f"https://cdn/{fname}", file_name=fname,
                                   version=num, tag=num, published_at=published,
                                   source="modrinth")
        assets = [
            ver("5.9.3", "ComplementaryUnbound_r5.9.3.zip", "2026-03-01T00:00:00Z"),
            ver("5.9.2", "ComplementaryUnbound_r5.9.2.zip", "2026-01-01T00:00:00Z"),
            ver("5.9.1", "ComplementaryUnbound_r5.9.1.zip", "2025-11-01T00:00:00Z"),
        ]
        e = fake_entry(pack_id="shader-complementary", kind=W.KIND_SHADER,
                       name="Complementary")
        got = packs._pick_asset(assets, e)
        self.assertEqual(got.file_name, "ComplementaryUnbound_r5.9.3.zip")
        self.assertEqual(got.version, "5.9.3")

    def test_latest_release_with_two_files_is_ambiguous(self):
        """最新发布里确有两个文件（主包 + Modernity 变体）→ 仍要求用户绑定。"""
        def gh(fname, tag="v5.45", published="2026-09-24T00:00:00Z"):
            return packs.PackAsset(url=f"https://gh/{fname}", file_name=fname,
                                   version=tag, tag=tag, published_at=published,
                                   source="github")
        assets = [gh("Shadow.UI.v5.45.zip"),
                  gh("Shadow.UI.v5.45-Modernity.version.zip"),
                  gh("Shadow.UI.v5.44.zip", tag="v5.44", published="2026-08-01T00:00:00Z")]
        e = fake_entry(name="Unrelated")
        self.assertIsNone(packs._pick_asset(assets, e))
        # 绑定正则后能确定
        got = packs._pick_asset(assets, e, asset_regex="Modernity")
        self.assertIn("Modernity", got.file_name)

    def test_release_group_helpers(self):
        def gh(fname, tag, published):
            return packs.PackAsset(url="u", file_name=fname, tag=tag,
                                   published_at=published, source="github")
        groups = packs._release_groups([
            gh("a.zip", "v2", "2026-02-01"), gh("b.zip", "v2", "2026-02-01"),
            gh("c.zip", "v1", "2026-01-01")])
        self.assertEqual(len(groups), 2)
        self.assertEqual([a.file_name for a in groups[0]], ["a.zip", "b.zip"])
        self.assertEqual([a.file_name for a in groups[1]], ["c.zip"])

    def test_ambiguous_same_score_requires_choice(self):
        """两条候选各命中一个词（repair / gameUI 变体）时不猜，交给用户选。"""
        e = fake_entry(name="Minimalistic Technology")
        assets = self._assets("betterquesting-UI-V2.zip",
                              "Minimalistic-GTNH-repair-1.1.3.zip",
                              "Minimalistic-GTNH-gameUI-1.1.3.zip")
        self.assertIsNone(packs._pick_asset(assets, e))

    def test_partial_name_hit_requires_user_choice(self):
        """条目名只有一个词命中（Minimalistic ← Minimalistic-GTNH-repair）时不敢猜。

        真实仓库 Fogy-F/Minimalistic-GTNH-repair 里同时有 repair / gameUI / extend
        等多个包，按“部分命中”自动选会装错，因此要求用户绑定；绑定后即可确定。
        """
        e = fake_entry(name="Minimalistic Technology")
        assets = self._assets("betterquesting-UI-V2.zip",
                              "Minimalistic-GTNH-repair-1.1.3.zip")
        self.assertIsNone(packs._pick_asset(assets, e))
        chosen = packs._pick_asset(assets, e, asset_regex="repair")
        self.assertIn("repair", chosen.file_name)

    def test_unambiguous_name_match(self):
        e = fake_entry(name="Shadow UI")
        assets = self._assets("Shadow.UI.v5.45.zip", "SomethingElse.zip")
        self.assertEqual(packs._pick_asset(assets, e).file_name, "Shadow.UI.v5.45.zip")

    def test_ambiguous_returns_none(self):
        e = fake_entry(name="Unrelated")
        self.assertIsNone(packs._pick_asset(self._assets("a.zip", "b.zip"), e))

    def test_asset_regex_override(self):
        """绑定筛选正则后，多候选仓库也能确定唯一资产（不再要求用户手选）。"""
        e = fake_entry(name="Unrelated")
        got = packs._pick_asset(self._assets("Shadow.UI.v5.45.zip",
                                             "Shadow.UI.v5.45-Modernity.version.zip"),
                                e, asset_regex="Modernity")
        self.assertIn("Modernity", got.file_name)

    def test_bad_regex_raises(self):
        e = fake_entry(name="Unrelated")
        with self.assertRaises(packs.PacksError):
            packs._pick_asset(self._assets("a.zip"), e, asset_regex="(unclosed")

    def test_pick_asset_reads_regex_from_entry(self):
        """pick_asset 从条目字段读取绑定正则（GUI「选择下载资产」写入这里）。"""
        e = fake_entry(name="Unrelated", asset_regex="Modernity")
        result = packs.PackSourceResult(
            source="github", status="ok",
            assets=self._assets("Shadow.UI.v5.45.zip",
                                "Shadow.UI.v5.45-Modernity.version.zip"),
            url="https://github.com/a/b")
        got = packs.pick_asset(result, e)
        self.assertIn("Modernity", got.file_name)


class TestListAssets(unittest.TestCase):
    """下载源查询：合成 GitHub / Modrinth 响应，不联网。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = make_cfg(Path(self.tmp.name) / "data")

    def _patch_get(self, payload, code=None):
        def fake(url, *, cache_file, ttl_hours=6.0, headers=None, proxy=None, force=False):
            if code:
                raise net.HttpError(code, "boom")
            return payload, "fresh"
        return mock.patch.object(net, "http_get_cached", side_effect=fake)

    def test_github_release_assets_parsed(self):
        payload = [{
            "tag_name": "v5.45", "published_at": "2026-09-24T22:50:47Z", "prerelease": False,
            "assets": [
                {"name": "gtnh-pack-update.json", "size": 10,
                 "browser_download_url": "https://github.com/x/y/releases/download/v5.45/gtnh-pack-update.json"},
                {"name": "Shadow.UI.v5.45.zip", "size": 1000,
                 "browser_download_url": "https://github.com/x/y/releases/download/v5.45/Shadow.UI.v5.45.zip"},
            ]}]
        e = fake_entry(name="Shadow UI",
                       urls={"links": [], "github": "https://github.com/Ranzuu/Shadow-UI/releases",
                             "modrinth": None, "curseforge": None, "primary": None, "direct": []})
        with self._patch_get(payload):
            res = packs.list_assets(self.cfg, e)
        self.assertTrue(res.ok)
        self.assertEqual(res.source, "github")
        self.assertEqual(len(res.assets), 1)          # json 元数据被排除
        self.assertEqual(res.assets[0].version, "v5.45")
        self.assertEqual(res.latest_version, "v5.45")

    def test_github_no_release(self):
        e = fake_entry(name="X",
                       urls={"links": [], "github": "https://github.com/a/b",
                             "modrinth": None, "curseforge": None, "primary": None, "direct": []})
        with self._patch_get([]):
            res = packs.list_assets(self.cfg, e)
        self.assertEqual(res.status, "none")
        self.assertIn("Release", res.note)

    def test_github_404_is_none(self):
        e = fake_entry(name="X",
                       urls={"links": [], "github": "https://github.com/a/b",
                             "modrinth": None, "curseforge": None, "primary": None, "direct": []})
        with self._patch_get(None, code=404):
            res = packs.list_assets(self.cfg, e)
        self.assertEqual(res.status, "none")

    def test_github_error_reported(self):
        e = fake_entry(name="X",
                       urls={"links": [], "github": "https://github.com/a/b",
                             "modrinth": None, "curseforge": None, "primary": None, "direct": []})
        with self._patch_get(None, code=403):
            res = packs.list_assets(self.cfg, e)
        self.assertEqual(res.status, "error")
        self.assertIn("GitHub 查询失败", res.note)

    def test_modrinth_versions_parsed(self):
        payload = [{
            "version_number": "r5.9.3", "date_published": "2026-01-01T00:00:00Z",
            "files": [{"filename": "ComplementaryUnbound_r5.9.3.zip", "url": "https://cdn/x.zip",
                       "size": 500}]}]
        e = fake_entry(pack_id="shader-complementary", kind=W.KIND_SHADER,
                       name="Complementary",
                       urls={"links": [], "github": None,
                             "modrinth": "https://modrinth.com/shader/complementary-unbound",
                             "curseforge": None,
                             "primary": "https://modrinth.com/shader/complementary-unbound",
                             "direct": ["https://modrinth.com/shader/complementary-unbound"]})
        with self._patch_get(payload):
            res = packs.list_assets(self.cfg, e)
        self.assertTrue(res.ok)
        self.assertEqual(res.source, "modrinth")
        self.assertEqual(res.assets[0].file_name, "ComplementaryUnbound_r5.9.3.zip")
        self.assertEqual(res.latest_version, "r5.9.3")

    def test_unsupported_host_is_manual(self):
        e = fake_entry(urls={"links": [], "github": None, "modrinth": None,
                             "curseforge": "https://www.curseforge.com/x",
                             "primary": "https://www.curseforge.com/x", "direct": []})
        res = packs.list_assets(self.cfg, e)
        self.assertEqual(res.status, "unsupported")
        self.assertIn("API key", res.note)

    def test_no_links_at_all(self):
        res = packs.list_assets(self.cfg, fake_entry())
        self.assertEqual(res.status, "unsupported")


class TestPacksDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "packs_db.json"

    def test_merge_adds_and_reports(self):
        db = packs.PacksDB(self.path)
        changes = db.merge_wiki([fake_entry(name="A"), fake_entry("pack-b", name="B")])
        self.assertEqual(len(db.all()), 2)
        self.assertTrue(any("新增" in c for c in changes))
        self.assertTrue(db.meta.get("wiki_fetched_at"))

    def test_merge_rejects_empty(self):
        db = packs.PacksDB(self.path)
        with self.assertRaises(ValueError):
            db.merge_wiki([])

    def test_merge_preserves_aliases_and_binding(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A")])
        db.add_alias("pack-demo", "MyAlias")
        db.update_entry("pack-demo", {"bound_source": "https://github.com/x/y",
                                      "source_override": True, "asset_regex": "Modernity"})
        db.merge_wiki([fake_entry(name="A")])
        got = db.get("pack-demo")
        self.assertIn("MyAlias", got["aliases"])
        self.assertEqual(got["bound_source"], "https://github.com/x/y")
        self.assertEqual(got["asset_regex"], "Modernity")

    def test_merge_marks_removed(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A"), fake_entry("pack-b", name="B")])
        changes = db.merge_wiki([fake_entry(name="A")])
        self.assertTrue(db.get("pack-b")["wiki_removed"])
        self.assertTrue(any("移除" in c for c in changes))
        self.assertEqual([p["id"] for p in db.by_kind()], ["pack-demo"])
        self.assertEqual(len(db.by_kind(include_removed=True)), 2)

    def test_merge_reports_field_change(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A", category="旧")])
        changes = db.merge_wiki([fake_entry(name="A", category="新")])
        self.assertTrue(any("分类" in c for c in changes))

    def test_installable_excludes_local(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A"),
                       fake_entry("pack-b", name="B", local=True)])
        self.assertEqual([p["id"] for p in db.installable()], ["pack-demo"])

    def test_categories_builtin_last(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A", category="游戏自带资源包"),
                       fake_entry("pack-b", name="B", category="深色模式")])
        self.assertEqual(db.categories(W.KIND_RESOURCE), ["深色模式", "游戏自带资源包"])

    def test_installed_records_roundtrip(self):
        db = packs.PacksDB(self.path)
        db.set_installed(W.KIND_RESOURCE, "pack-demo", {"version": "1.0", "file_name": "a.zip"})
        self.assertEqual(db.installed(W.KIND_RESOURCE)["pack-demo"]["version"], "1.0")
        db.remove_installed(W.KIND_RESOURCE, "pack-demo")
        self.assertEqual(db.installed(W.KIND_RESOURCE), {})

    def test_corrupt_file_recovers_from_bak(self):
        db = packs.PacksDB(self.path)
        db.merge_wiki([fake_entry(name="A")])
        shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
        self.path.write_text("{ broken", encoding="utf-8")
        recovered = packs.PacksDB(self.path)
        self.assertEqual(len(recovered.all()), 1)


class TestReconcileInstalled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_cfg(self.root / "data")
        folder = self.root / "resourcepacks"
        folder.mkdir()
        make_zip(folder / "Shadow.UI.v5.45.zip", ["pack.mcmeta"])
        self.cfg.set_mods_dir("client", self.root / "mods")
        (self.root / "mods").mkdir(exist_ok=True)
        self.cfg.set_pack_dir(W.KIND_RESOURCE, folder)
        self.db = packs.PacksDB(self.root / "data" / "packs_db.json")

    def test_unrecorded_file_is_adopted(self):
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        live = packs.reconcile_installed(self.cfg, self.db)
        rec = self.db.installed(W.KIND_RESOURCE)["pack-demo"]
        self.assertEqual(rec["file_name"], "Shadow.UI.v5.45.zip")
        self.assertEqual(rec["version"], "v5.45")
        self.assertIn("pack-demo", live[W.KIND_RESOURCE])

    def test_same_file_preserves_recorded_upstream_version(self):
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v5.45.zip", "version": "v5.46"})
        packs.reconcile_installed(self.cfg, self.db)
        self.assertEqual(self.db.installed(W.KIND_RESOURCE)["pack-demo"]["version"], "v5.46")

    def test_replacement_without_version_clears_stale_version(self):
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        (self.root / "resourcepacks" / "Shadow.UI.v5.45.zip").unlink()
        make_zip(self.root / "resourcepacks" / "Shadow.UI.zip", ["pack.mcmeta"])
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v5.45.zip", "version": "v5.45"})
        packs.reconcile_installed(self.cfg, self.db)
        self.assertIsNone(self.db.installed(W.KIND_RESOURCE)["pack-demo"]["version"])

    def test_record_corrected_when_recorded_file_missing(self):
        """记录指向的文件没了、但同条目有文件可认领 → 校正为真实文件（不留幽灵）。"""
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo", {"file_name": "gone.zip"})
        packs.reconcile_installed(self.cfg, self.db)
        # 目录里的 Shadow.UI.v5.45.zip 仍能匹配到该条目 → 记录被校正为真实文件
        rec = self.db.installed(W.KIND_RESOURCE)["pack-demo"]
        self.assertEqual(rec["file_name"], "Shadow.UI.v5.45.zip")

    def test_missing_pack_dir_keeps_records(self):
        """目录不存在（实例未创建/盘暂不可用）时跳过校正，不清掉安装记录。

        扫不到文件≠没装：暂时性目录缺失不能把 installed_at/source_url/
        绑定信息一并抹掉。
        """
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v5.45.zip",
                               "version": "v5.45",
                               "installed_at": "T", "source_url": "https://x/y.zip"})
        gone = self.root / "vanished" / "resourcepacks"
        self.cfg.set_pack_dir(W.KIND_RESOURCE, gone)
        live = packs.reconcile_installed(self.cfg, self.db)
        self.assertEqual(live[W.KIND_RESOURCE], {})
        rec = self.db.installed(W.KIND_RESOURCE)["pack-demo"]
        self.assertEqual(rec["file_name"], "Shadow.UI.v5.45.zip")
        self.assertEqual(rec["version"], "v5.45")
        self.assertEqual(rec["source_url"], "https://x/y.zip")

    def test_record_for_absent_pack_cleared(self):
        self.db.merge_wiki([fake_entry(name="Other Pack")])
        self.db.set_installed(W.KIND_RESOURCE, "pack-other", {"file_name": "gone.zip"})
        packs.reconcile_installed(self.cfg, self.db)
        self.assertEqual(self.db.installed(W.KIND_RESOURCE), {})

    def test_metadata_preserved_on_rescan(self):
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"source_url": "https://x/y.zip", "installed_at": "T"})
        packs.reconcile_installed(self.cfg, self.db)
        rec = self.db.installed(W.KIND_RESOURCE)["pack-demo"]
        self.assertEqual(rec["source_url"], "https://x/y.zip")
        self.assertEqual(rec["installed_at"], "T")

    def test_installed_index_keys(self):
        self.db.merge_wiki([fake_entry(name="Shadow UI")])
        packs.reconcile_installed(self.cfg, self.db)
        idx = packs.installed_index(self.db)
        self.assertIn((W.KIND_RESOURCE, "pack-demo"), idx)


class TestInstallAndBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_cfg(self.root / "data")
        self.folder = self.root / "resourcepacks"
        self.folder.mkdir()
        self.cfg.set_pack_dir(W.KIND_RESOURCE, self.folder)
        self.db = packs.PacksDB(self.root / "data" / "packs_db.json")
        self.entry = fake_entry(name="Shadow UI")
        self.db.merge_wiki([self.entry])

    def _asset(self, name="Shadow.UI.v5.46.zip", version="v5.46"):
        return packs.PackAsset(url="https://x/y.zip", file_name=name,
                               version=version, source="github")

    def _patch_download(self, entries=("pack.mcmeta",)):
        def fake(url, dest, *, timeout=60, proxy=None, progress_cb=None):
            make_zip(Path(dest), list(entries))
        return mock.patch.object(net, "download", side_effect=fake)

    def test_install_writes_file_and_record(self):
        with self._patch_download():
            ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertTrue(ok, msg)
        self.assertTrue((self.folder / "Shadow.UI.v5.46.zip").exists())
        rec = self.db.installed(W.KIND_RESOURCE)["pack-demo"]
        self.assertEqual(rec["version"], "v5.46")
        self.assertTrue(rec["valid"])

    def test_invalid_download_preserves_file_and_record(self):
        dest = self.folder / "Shadow.UI.v5.46.zip"
        dest.write_bytes(b"original")
        before = self.db.installed(W.KIND_RESOURCE)
        with self._patch_download(entries=("index.html",)):
            ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertFalse(ok, msg)
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertEqual(self.db.installed(W.KIND_RESOURCE), before)
        self.assertEqual(packs.list_backups(self.cfg), [])

    def test_unsafe_asset_names_rejected_before_download(self):
        for name in ("../outside.zip", "..\\outside.zip", "C:\\outside.zip",
                     "sub/pack.zip", "pack.zip:stream", "pack.exe", "CON.zip"):
            with self.subTest(name=name), mock.patch.object(net, "download") as download:
                ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset(name))
                self.assertFalse(ok, msg)
                download.assert_not_called()

    def test_update_retires_recorded_old_filename_after_backup(self):
        old = self.folder / "Shadow.UI.v5.45.zip"
        old.write_bytes(b"old-version")
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": old.name, "path": str(old), "version": "v5.45"})
        unrelated = self.folder / "Other.zip"
        unrelated.write_bytes(b"unrelated")
        with self._patch_download():
            ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertTrue(ok, msg)
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        backups = packs.list_backups(self.cfg)
        self.assertEqual(backups[0]["path"].read_bytes(), b"old-version")
        packs.reconcile_installed(self.cfg, self.db)
        self.assertEqual(self.db.installed(W.KIND_RESOURCE)["pack-demo"]["version"], "v5.46")

    def test_old_backup_failure_prevents_new_install(self):
        old = self.folder / "Shadow.UI.v5.45.zip"
        old.write_bytes(b"old-version")
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": old.name, "version": "v5.45"})
        backup = packs.backup_existing
        def fail_old(cfg, kind, pack_id, source):
            if source == old:
                raise OSError("backup full")
            return backup(cfg, kind, pack_id, source)
        with self._patch_download(), mock.patch.object(packs, "backup_existing", side_effect=fail_old):
            ok, _ = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertFalse(ok)
        self.assertEqual(old.read_bytes(), b"old-version")
        self.assertFalse((self.folder / "Shadow.UI.v5.46.zip").exists())

    def test_failed_write_preserves_existing_file(self):
        dest = self.folder / "Shadow.UI.v5.46.zip"
        dest.write_bytes(b"original")
        with self._patch_download(), mock.patch.object(packs.os, "replace", side_effect=OSError("disk full")):
            ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertFalse(ok, msg)
        self.assertEqual(dest.read_bytes(), b"original")
        self.assertEqual(list(self.folder.glob(".pack-*.part")), [])

    def test_backup_retention_uses_backup_time(self):
        self.cfg.data["backup_keep"] = 1
        recent = self.folder / "recent.zip"
        recent.write_bytes(b"recent")
        packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo", recent)
        old = self.folder / "old.zip"
        old.write_bytes(b"old")
        packs.os.utime(old, (1, 1))
        target = packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo", old)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"old")

    def test_backup_collision_preserves_all_copies(self):
        source = self.folder / "same.zip"
        with mock.patch.object(packs.utils, "timestamp_str", return_value="same-time"):
            for i in range(3):
                source.write_bytes(str(i).encode())
                packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo", source)
        backups = packs.list_backups(self.cfg)
        self.assertEqual({b["path"].read_bytes() for b in backups}, {b"0", b"1", b"2"})

    def test_install_backs_up_existing_same_name(self):
        (self.folder / "Shadow.UI.v5.46.zip").write_bytes(b"old")
        with self._patch_download():
            ok, _ = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertTrue(ok)
        backups = packs.list_backups(self.cfg, W.KIND_RESOURCE)
        self.assertTrue(backups)
        self.assertEqual(backups[0]["file_name"], "Shadow.UI.v5.46.zip")
        self.assertEqual(backups[0]["path"].read_bytes(), b"old")

    def test_shader_install_records_structure_note(self):
        sp = self.root / "shaderpacks"
        sp.mkdir()
        self.cfg.set_pack_dir(W.KIND_SHADER, sp)
        entry = fake_entry("shader-x", kind=W.KIND_SHADER, name="Shader X")
        self.db.merge_wiki([entry])
        asset = packs.PackAsset(url="https://x/s.zip", file_name="S.zip", version="1.0")
        with self._patch_download(entries=("PackName/shaders/a.vsh",)):
            ok, msg = packs.apply_install(self.cfg, self.db, entry, asset)
        self.assertTrue(ok)
        self.assertIn("多了一层文件夹", msg)

    def test_install_without_target_dir_fails_cleanly(self):
        cfg = make_cfg(self.root / "data2")
        db = packs.PacksDB(self.root / "data2" / "packs_db.json")
        db.merge_wiki([self.entry])
        ok, msg = packs.apply_install(cfg, db, self.entry, self._asset())
        self.assertFalse(ok)
        self.assertIn("resourcepacks", msg)

    def test_download_failure_does_not_touch_target(self):
        def boom(url, dest, *, timeout=60, proxy=None, progress_cb=None):
            raise net.HttpError(-1, "网络断了")
        with mock.patch.object(net, "download", side_effect=boom):
            ok, msg = packs.apply_install(self.cfg, self.db, self.entry, self._asset())
        self.assertFalse(ok)
        self.assertIn("安装失败", msg)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_unknown_kind_rejected(self):
        bad = fake_entry(name="X", kind="mod")
        with self.assertRaises(packs.PacksError):
            packs.install_asset(self.cfg, bad, self._asset())

    def test_backup_pruned_to_keep(self):
        """备份目录整体按 backup_keep 上限轮换（与 mod 备份一致，不无限增长）。"""
        self.cfg.data["backup_keep"] = 2
        for i in range(4):
            (self.folder / f"a{i}.zip").write_bytes(f"old{i}".encode())
            packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo",
                                  self.folder / f"a{i}.zip")
        kept = packs.list_backups(self.cfg, W.KIND_RESOURCE)
        self.assertEqual(len(kept), 2)
        self.assertEqual({b["file_name"] for b in kept}, {"a2.zip", "a3.zip"})

    def test_backup_same_name_gets_timestamp_suffix(self):
        """同一文件多次更新不能互相覆盖，旧份保留为带时间戳的副本。"""
        for i in range(2):
            (self.folder / "same.zip").write_bytes(f"v{i}".encode())
            packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo",
                                  self.folder / "same.zip")
        names = {b["file_name"] for b in packs.list_backups(self.cfg, W.KIND_RESOURCE)}
        self.assertEqual(len(names), 2)
        self.assertIn("same.zip", names)

    def test_restore_backup(self):
        (self.folder / "Shadow.UI.v5.46.zip").write_bytes(b"old-bytes")
        packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo",
                              self.folder / "Shadow.UI.v5.46.zip")
        (self.folder / "Shadow.UI.v5.46.zip").write_bytes(b"new-bytes")
        rec = packs.list_backups(self.cfg, W.KIND_RESOURCE)[0]
        dest = packs.restore_backup(self.cfg, rec, self.folder)
        self.assertEqual(dest.read_bytes(), b"old-bytes")

    def test_restore_missing_backup_raises(self):
        with self.assertRaises(packs.PacksError):
            packs.restore_backup(self.cfg, {"path": self.root / "nope.zip",
                                            "file_name": "x.zip"}, self.folder)

    def test_list_backups_filtered_by_kind(self):
        (self.folder / "a.zip").write_bytes(b"x")
        packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo", self.folder / "a.zip")
        self.assertTrue(packs.list_backups(self.cfg, W.KIND_RESOURCE))
        self.assertEqual(packs.list_backups(self.cfg, W.KIND_SHADER), [])

    def test_folder_backup_copies_tree(self):
        d = self.folder / "PackFolder"
        (d / "assets").mkdir(parents=True)
        (d / "assets" / "x.png").write_bytes(b"png")
        target = packs.backup_existing(self.cfg, W.KIND_RESOURCE, "pack-demo", d)
        self.assertTrue((target / "assets" / "x.png").exists())


class TestPlanInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = make_cfg(self.root / "data")
        self.db = packs.PacksDB(self.root / "data" / "packs_db.json")
        self.entry = fake_entry(name="Shadow UI")
        self.db.merge_wiki([self.entry])

    def _result(self, name="Shadow.UI.v5.46.zip", version="v5.46"):
        a = packs.PackAsset(url="https://x/y.zip", file_name=name, version=version,
                            source="github")
        return packs.PackSourceResult(source="github", status="ok", assets=[a],
                                      latest_version=version, url="https://github.com/a/b")

    def test_plan_install_when_not_installed(self):
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": self._result()})
        self.assertEqual(plans[0].action, "install")
        self.assertEqual(plans[0].version, "v5.46")

    def test_plan_skip_when_same_version(self):
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v5.46.zip", "version": "v5.46"})
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": self._result()})
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("已是最新", plans[0].detail)

    def test_plan_update_when_newer_available(self):
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v5.44.zip", "version": "v5.44"})
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": self._result()})
        self.assertEqual(plans[0].action, "update")
        self.assertIn("v5.44", plans[0].detail)
        self.assertIn("v5.46", plans[0].detail)

    def test_plan_never_downgrades(self):
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.v9.0.zip", "version": "v9.0"})
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": self._result()})
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("本地版本更高", plans[0].detail)

    def test_plan_does_not_loop_when_versions_unknown(self):
        """上游 tag 不是版本号（Stable/machines）时，装完不能再报"可更新"。

        真实回归：安装 BetterClick.zip 后（上游 tag=Stable，文件名无版本号），
        旧逻辑每次都判为 update，形成"装完立刻又提示更新"的死循环。
        """
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "BetterClick.zip", "version": None})
        res = packs.PackSourceResult(
            source="github", status="ok",
            assets=[packs.PackAsset(url="https://x/BetterClick.zip",
                                    file_name="BetterClick.zip", version="Stable")],
            url="https://github.com/a/b")
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": res})
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("无法比较版本", plans[0].detail)

    def test_plan_skip_when_local_version_missing(self):
        """本地版本未知时无法证明上游更高，非交互更新不能冒险降级。"""
        self.db.set_installed(W.KIND_RESOURCE, "pack-demo",
                              {"file_name": "Shadow.UI.zip", "version": None})
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": self._result()})
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("无法比较版本", plans[0].detail)

    def test_plan_manual_for_curseforge(self):
        entry = fake_entry("pack-cf", name="CF Pack",
                           urls={"links": [], "github": None, "modrinth": None,
                                 "curseforge": "https://www.curseforge.com/x",
                                 "primary": "https://www.curseforge.com/x", "direct": []})
        plans = packs.plan_install(self.cfg, self.db, [entry])
        self.assertEqual(plans[0].action, "manual")
        self.assertIn("API key", plans[0].detail)

    def test_plan_skip_for_builtin(self):
        entry = fake_entry("pack-bi", name="Builtin", local=True)
        plans = packs.plan_install(self.cfg, self.db, [entry])
        self.assertEqual(plans[0].action, "skip")
        self.assertIn("自带", plans[0].detail)

    def test_plan_ambiguous_requires_manual(self):
        a1 = packs.PackAsset(url="https://x/a.zip", file_name="a.zip", version="1")
        a2 = packs.PackAsset(url="https://x/b.zip", file_name="b.zip", version="1")
        res = packs.PackSourceResult(source="github", status="ok", assets=[a1, a2],
                                     url="https://github.com/a/b")
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": res})
        self.assertEqual(plans[0].action, "manual")
        self.assertEqual(len(plans[0].ambiguous_assets), 2)

    def test_plan_reports_query_error(self):
        res = packs.PackSourceResult(source="github", status="error", note="查询失败")
        plans = packs.plan_install(self.cfg, self.db, [self.entry],
                                   results={"pack-demo": res})
        self.assertEqual(plans[0].action, "error")

    def test_plan_queries_when_no_prefetched_result(self):
        with mock.patch.object(packs, "list_assets", return_value=self._result()) as m:
            plans = packs.plan_install(self.cfg, self.db, [self.entry])
        self.assertEqual(m.call_count, 1)
        self.assertEqual(plans[0].action, "install")


class TestSetDownloadSource(unittest.TestCase):
    """手动指定下载源（wiki 链接过时时）。

    真实案例：wiki 的 Modernity-GTNH 写的是 ``github.com/ModernityGTNH``（组织页）
    和 ``ABKQPO/Modernity-GTNH``（旧仓库），自动解析不可用；用户给的
    ``github.com/ModernityGTNH/Modernity-GTNH/releases`` 才是当前发布页。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = packs.PacksDB(Path(self.tmp.name) / "packs_db.json")
        self.entry = fake_entry(
            "pack-modernity-gtnh", name="Modernity-GTNH",
            urls={"links": [], "github": "https://github.com/ABKQPO/Modernity-GTNH",
                  "modrinth": None, "curseforge": None,
                  "primary": "https://github.com/ModernityGTNH",
                  "direct": []})
        self.db.merge_wiki([self.entry])

    def test_bind_github_releases_url(self):
        res = packs.set_download_source(
            self.db, "pack-modernity-gtnh",
            "https://github.com/ModernityGTNH/Modernity-GTNH/releases")
        self.assertEqual(res["repo"], ("ModernityGTNH", "Modernity-GTNH"))
        self.assertEqual(packs.entry_auto_url(self.db.get("pack-modernity-gtnh")),
                         "https://github.com/ModernityGTNH/Modernity-GTNH/releases")

    def test_bind_owner_repo_shorthand(self):
        packs.set_download_source(self.db, "pack-modernity-gtnh",
                                  "ModernityGTNH/Modernity-GTNH")
        self.assertEqual(packs.entry_auto_url(self.db.get("pack-modernity-gtnh")),
                         "https://github.com/ModernityGTNH/Modernity-GTNH")

    def test_bind_modrinth(self):
        res = packs.set_download_source(self.db, "pack-modernity-gtnh",
                                        "https://modrinth.com/shader/complementary-unbound")
        self.assertEqual(res["kind"], "modrinth")

    def test_org_only_github_url_maps_to_same_name_repo(self):
        """github.com/<name> 视为 owner 与仓库同名（GitHub 的跳转规则）。"""
        res = packs.set_download_source(self.db, "pack-modernity-gtnh",
                                        "https://github.com/ModernityGTNH")
        self.assertEqual(res["repo"], ("ModernityGTNH", "ModernityGTNH"))

    def test_invalid_inputs_rejected(self):
        for bad in ("not a url", "ftp://example.com/x", "https://github.com"):
            with self.subTest(bad=bad):
                with self.assertRaises(packs.PacksError):
                    packs.set_download_source(self.db, "pack-modernity-gtnh", bad)

    def test_unknown_entry_rejected(self):
        with self.assertRaises(packs.PacksError):
            packs.set_download_source(self.db, "nope", "https://github.com/a/b")

    def test_clear_restores_wiki_link(self):
        packs.set_download_source(self.db, "pack-modernity-gtnh",
                                  "https://github.com/ModernityGTNH/Modernity-GTNH")
        packs.set_download_source(self.db, "pack-modernity-gtnh", "")
        entry = self.db.get("pack-modernity-gtnh")
        self.assertIsNone(entry.get("bound_source"))
        self.assertEqual(packs.entry_auto_url(entry),
                         "https://github.com/ABKQPO/Modernity-GTNH")

    def test_binding_survives_wiki_refresh(self):
        packs.set_download_source(self.db, "pack-modernity-gtnh",
                                  "https://github.com/ModernityGTNH/Modernity-GTNH")
        self.db.merge_wiki([self.entry])          # 模拟再次刷新 wiki
        self.assertEqual(packs.entry_auto_url(self.db.get("pack-modernity-gtnh")),
                         "https://github.com/ModernityGTNH/Modernity-GTNH")

    def test_source_kind(self):
        self.assertEqual(packs.source_kind("https://github.com/a/b"), "github")
        self.assertEqual(packs.source_kind("https://modrinth.com/shader/x"), "modrinth")
        self.assertEqual(packs.source_kind("https://www.curseforge.com/x"), "manual")


class TestBoundAssetStrictness(unittest.TestCase):
    """显式绑定过的资产筛选不能被自动推断顶替。"""

    def _assets(self, *names):
        return [packs.PackAsset(url=f"https://x/{n}", file_name=n, source="github")
                for n in names]

    def test_binding_wins_over_name_match(self):
        e = fake_entry(name="Shadow UI")
        got = packs._pick_asset(self._assets("Shadow.UI.v5.45.zip", "Other.zip"), e,
                                asset_regex="Other")
        self.assertEqual(got.file_name, "Other.zip")

    def test_stale_binding_does_not_silently_substitute(self):
        """绑定过但上游改了名 → 返回 None 让用户重选，不偷偷换文件。"""
        e = fake_entry(name="Shadow UI")
        got = packs._pick_asset(self._assets("Shadow.UI.v5.46.zip"), e,
                                asset_regex="v5.45")
        self.assertIsNone(got)

    def test_stale_binding_retries_within_latest_release(self):
        e = fake_entry(name="Shadow UI")
        assets = [packs.PackAsset(url="u1", file_name="Shadow.UI.v5.46-New.zip",
                                  tag="v5.46", published_at="2026-02-01", source="github"),
                  packs.PackAsset(url="u2", file_name="Shadow.UI.v5.45.zip",
                                  tag="v5.45", published_at="2026-01-01", source="github")]
        got = packs._pick_asset(assets, e, asset_regex="v5.46")
        self.assertEqual(got.file_name, "Shadow.UI.v5.46-New.zip")


class TestCustomPacks(unittest.TestCase):
    """登记 wiki 之外的包（Modrinth 的 Modernity / Modernity Adjunct）。

    真实场景：用户装了 ``Modernity-f1-3.10.2.zip`` 与 ``ModernityAdjunct-f1-1.6.zip``，
    它们不在 wiki「资源包与光影」页面里，工具本来完全不认识、也无法检查更新。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / "resourcepacks"
        self.folder.mkdir()
        for n in ("Modernity-f1-3.10.2.zip", "ModernityAdjunct-f1-1.6.zip"):
            make_zip(self.folder / n, ["pack.mcmeta"])
        self.cfg = make_cfg(self.root / "data")
        self.cfg.set_pack_dir(W.KIND_RESOURCE, self.folder)
        self.db = packs.PacksDB(self.root / "data" / "packs_db.json")

    def _register(self):
        ids = []
        for name, url in (
                ("Modernity", "https://modrinth.com/resourcepack/modernity/versions"),
                ("Modernity Adjunct",
                 "https://modrinth.com/resourcepack/modernity-adjunct/versions")):
            ids.append(self.db.add_custom({"kind": W.KIND_RESOURCE, "name_en": name,
                                           "source_url": url, "aliases": [name]}))
        return ids

    def test_add_custom_creates_entry(self):
        pid = self.db.add_custom({"kind": W.KIND_RESOURCE, "name_en": "Modernity",
                                  "source_url": "https://modrinth.com/resourcepack/modernity"})
        e = self.db.get(pid)
        self.assertTrue(e["custom"])
        self.assertEqual(e["kind"], W.KIND_RESOURCE)
        self.assertFalse(e["local"])            # 可安装，不是"整合包自带"
        self.assertEqual(e["urls"]["modrinth"],
                         "https://modrinth.com/resourcepack/modernity")
        self.assertEqual(packs.entry_auto_url(e),
                         "https://modrinth.com/resourcepack/modernity")

    def test_add_custom_requires_name_and_kind(self):
        with self.assertRaises(packs.PacksError):
            self.db.add_custom({"kind": W.KIND_RESOURCE, "name_en": "  "})
        with self.assertRaises(packs.PacksError):
            self.db.add_custom({"kind": "mod", "name_en": "X"})

    def test_add_custom_accepts_shorthand(self):
        pid = self.db.add_custom({"kind": W.KIND_SHADER, "name_en": "Shady",
                                  "source_url": "owner/repo"})
        self.assertEqual(packs.entry_auto_url(self.db.get(pid)),
                         "https://github.com/owner/repo")

    def test_unmatched_then_claimed_after_register(self):
        unknown = packs.unmatched_packs(self.cfg, self.db, W.KIND_RESOURCE)
        self.assertEqual({f.file_name for _k, f in unknown},
                         {"Modernity-f1-3.10.2.zip", "ModernityAdjunct-f1-1.6.zip"})
        self._register()
        live = packs.reconcile_installed(self.cfg, self.db, kinds=[W.KIND_RESOURCE])
        claimed = {self.db.get(pid)["name_en"]: f.file_name
                   for pid, f in live[W.KIND_RESOURCE].items()}
        self.assertEqual(claimed.get("Modernity"), "Modernity-f1-3.10.2.zip")
        self.assertEqual(claimed.get("Modernity Adjunct"), "ModernityAdjunct-f1-1.6.zip")

    def test_single_word_name_does_not_steal_other_files(self):
        """自定义条目叫 Modernity 不能把 ModernityAdjunct/Modernity-GTNH 抢走。"""
        self.db.add_custom({"kind": W.KIND_RESOURCE, "name_en": "Modernity",
                            "aliases": ["Modernity"]})
        self.db.add_custom({"kind": W.KIND_RESOURCE, "name_en": "Modernity Adjunct",
                            "aliases": ["Modernity Adjunct"]})
        entries = self.db.by_kind(W.KIND_RESOURCE)
        got = {}
        for f in packs.scan_pack_dir(self.folder, W.KIND_RESOURCE, with_size=False):
            got[f.file_name] = packs.match_pack(f, entries)[0]
        self.assertEqual(got["Modernity-f1-3.10.2.zip"], "custom-modernity")
        self.assertEqual(got["ModernityAdjunct-f1-1.6.zip"], "custom-modernity-adjunct")

    def test_custom_survives_wiki_merge_and_is_not_marked_removed(self):
        """自定义条目不在 wiki 里是常态，刷新后不能被打上 wiki_removed 而消失。"""
        self._register()
        wiki = [fake_entry("pack-outlined-ores", name="Outlined Ores")]
        self.db.merge_wiki(wiki)
        for pid in ("custom-modernity", "custom-modernity-adjunct"):
            e = self.db.get(pid)
            self.assertIsNotNone(e, f"{pid} 被 wiki 合并删掉了")
            self.assertFalse(e.get("wiki_removed"), f"{pid} 被误标为 wiki 已删除")
            self.assertIn(e, self.db.by_kind(W.KIND_RESOURCE))

    def test_wiki_entry_still_marked_removed(self):
        """wiki 条目从页面消失时仍要照常标记（别把上面那条修过头）。"""
        self.db.merge_wiki([fake_entry("pack-a", name="A"), fake_entry("pack-b", name="B")])
        self.db.merge_wiki([fake_entry("pack-a", name="A")])
        self.assertTrue(self.db.get("pack-b")["wiki_removed"])

    def test_remove_custom(self):
        ids = self._register()
        self.assertTrue(self.db.remove_custom(ids[0]))
        self.assertIsNone(self.db.get(ids[0]))
        self.assertFalse(self.db.remove_custom("pack-outlined-ores"))

    def test_suggest_pack_name(self):
        self.assertEqual(packs.suggest_pack_name("Modernity-f1-3.10.2.zip"), "Modernity f1")
        self.assertEqual(packs.suggest_pack_name("ModernityAdjunct-f1-1.6.zip"),
                         "ModernityAdjunct f1")

    def test_custom_pack_checks_updates(self):
        """登记后即可查询上游并判断更新（用真实 Modrinth 的版本形态）。"""
        ids = self._register()
        packs.reconcile_installed(self.cfg, self.db, kinds=[W.KIND_RESOURCE])
        entry = self.db.get(ids[1])
        res = packs.PackSourceResult(
            source="modrinth", status="ok", url=entry["urls"]["modrinth"],
            assets=[packs.PackAsset(url="https://cdn/ModernityAdjunct-f3-1.7.1.zip",
                                    file_name="ModernityAdjunct-f3-1.7.1.zip",
                                    version="f3-1.7.1", tag="f3-1.7.1",
                                    published_at="2026-08-25", source="modrinth")])
        plans = packs.plan_install(self.cfg, self.db, [entry],
                                   results={ids[1]: res})
        self.assertEqual(plans[0].action, "update")
        self.assertIn("1.6", plans[0].detail)


class TestFolderHint(unittest.TestCase):
    def test_hint_with_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp) / "data")
            cfg.set_pack_dir(W.KIND_SHADER, Path(tmp) / "shaderpacks")
            self.assertIn("shaderpacks", packs.folder_hint(cfg, W.KIND_SHADER))

    def test_hint_without_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(Path(tmp) / "data")
            self.assertIn("未设置", packs.folder_hint(cfg, W.KIND_RESOURCE))


if __name__ == "__main__":
    unittest.main()
