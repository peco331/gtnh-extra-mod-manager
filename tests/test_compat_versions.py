"""GTNH 兼容表解析 + 版本匹配 + 指定版本安装 测试。"""
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod import updater  # noqa: E402
from gtnhmod.downloader import atomic_replace  # noqa: E402
from gtnhmod.sources import GitHubSource, VersionOption  # noqa: E402
from gtnhmod.wiki import parse_compat, parse_wikitext  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wiki_sample.txt"


class TestParseCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
        cls.by_id = {m["id"]: m for m in mods}

    def test_row_table_range(self):
        # Programmable Hatches: {{row|v0.2.0pXX|GTNH 2.9.0 beta 1 | GTNH 2.9.0 beta 1}}
        e = self.by_id.get("programmable-hatch-mod")
        if not e:
            self.skipTest("fixture中无此条目")
        ranges = [r for r in e["compat"] if r["kind"] == "range"]
        self.assertTrue(ranges)
        r0 = ranges[0]
        self.assertEqual(r0["mod_ver"], "0.2.0pXX")
        self.assertEqual(r0["min"], "2.9.0 beta 1")
        self.assertEqual(r0["max"], "2.9.0 beta 1")

    def test_gtnh_min_rule(self):
        # Twist Space: "GTNH 2.9.0-beta1 版本：模组 0.8.0 及以上版本"
        e = self.by_id.get("twist-space-technology-mod")
        if not e:
            self.skipTest("fixture中无此条目")
        mins = [r for r in e["compat"] if r["kind"] == "gtnh_min"]
        self.assertTrue(any(r.get("mod_min") == "0.8.0" for r in mins))
        # "0.7.16是最后一个支持GTNH 2.8.0的版本" → gtnh_max
        maxs = [r for r in e["compat"] if r["kind"] == "gtnh_max"]
        self.assertTrue(any(r.get("mod_max") == "0.7.16" for r in maxs))

    def test_install_rule(self):
        # Box Plus Plus: "≥GTNH2.7.X需要安装Box 1.9及以上"
        e = self.by_id.get("box-plus-plus")
        if not e:
            self.skipTest("fixture中无此条目")
        mins = [r for r in e["compat"] if r["kind"] == "gtnh_min"]
        self.assertTrue(any(r.get("mod_min") == "1.9" for r in mins))

    def test_compat_count(self):
        # 有版本表的条目解析出了兼容规则（当前 wiki 内容格式下为 3 个）
        n = sum(1 for m in self.by_id.values() if m.get("compat"))
        self.assertGreaterEqual(n, 3)


class TestMatchCompat(unittest.TestCase):
    RULES_RANGE = [{"kind": "range", "mod_ver": "0.2.0pXX",
                    "min": "2.9.0 beta 1", "max": "2.9.0 beta 1", "raw": ""}]

    def test_range_ok(self):
        self.assertEqual(updater.match_compat(self.RULES_RANGE, "2.9.0-beta1", "0.2.0p44"),
                         "compatible")

    def test_range_wrong_gtnh(self):
        # 该mod版本要求GTNH 2.9.0 beta1，而用户是2.8.0 → 不适配
        self.assertEqual(updater.match_compat(self.RULES_RANGE, "2.8.0", "0.2.0p44"),
                         "incompatible")

    def test_range_pattern_miss(self):
        # 0.1.x 不在规则覆盖内 → unknown
        self.assertEqual(updater.match_compat(self.RULES_RANGE, "2.9.0-beta1", "0.1.3"),
                         "unknown")

    def test_min_rule(self):
        rules = [{"kind": "gtnh_min", "gtnh": "2.9.0", "mod_min": "0.8.0", "raw": ""}]
        self.assertEqual(updater.match_compat(rules, "2.9.0", "0.8.0"), "compatible")
        self.assertEqual(updater.match_compat(rules, "2.9.0", "0.7.16"), "incompatible")
        # 低于要求的GTNH版本时规则不生效 → unknown
        self.assertEqual(updater.match_compat(rules, "2.8.0", "0.5.0"), "unknown")
        # "2.9.0 beta 1" 与 "2.9.0-beta1" 等价
        rules2 = [{"kind": "gtnh_min", "gtnh": "2.9.0 beta 1", "mod_min": "0.8.0", "raw": ""}]
        self.assertEqual(updater.match_compat(rules2, "2.9.0-beta1", "0.8.0"), "compatible")

    def test_max_rule(self):
        rules = [{"kind": "gtnh_max", "gtnh": "2.8.0", "mod_max": "0.7.16", "raw": ""}]
        self.assertEqual(updater.match_compat(rules, "2.8.0", "0.7.16"), "compatible")
        self.assertEqual(updater.match_compat(rules, "2.8.0", "0.8.0"), "incompatible")

    def test_no_rules(self):
        self.assertEqual(updater.match_compat([], "2.9.0", "1.0.0"), "unknown")
        self.assertEqual(updater.match_compat(None, "2.9.0", "1.0.0"), "unknown")

    def test_pattern_match(self):
        self.assertTrue(updater._mod_pattern_match("v0.2.0pXX", "0.2.0p44"))
        self.assertTrue(updater._mod_pattern_match("0.3.7", "0.3.7"))
        self.assertTrue(updater._mod_pattern_match("1.9", "1.9.1"))
        self.assertFalse(updater._mod_pattern_match("0.3.7", "0.3.8"))


class TestVersionInstall(unittest.TestCase):
    """local_folder 源：指定版本安装。"""

    def test_install_specific_version(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_ver_"))
        try:
            data = tmp / "data"
            mods = tmp / "mods"
            src = tmp / "src"
            for d in (data, mods, src):
                d.mkdir(parents=True)
            from gtnhmod.config import Config
            from gtnhmod.db import ModsDB
            from gtnhmod.installed import InstalledDB
            cfg = Config(data)
            cfg.set_mods_dir("client", mods)
            cfg.data["gtnh_version"] = "2.9.0"
            cfg.save()
            db = ModsDB(data / "mods_db.json")
            inst = InstalledDB(data / "installed.json")
            for v in ("0.8.0", "0.9.0", "1.0.0"):
                (src / f"VerMod-1.7.10-{v}.jar").write_bytes(b"PK\x03\x04" + v.encode())
            mid = db.add_custom({"name_en": "VerMod", "source_type": "local_folder",
                                 "source": {"path": str(src), "name_regex": "^VerMod"}})
            entry = db.get(mid)
            # 版本列表：降序
            options = updater.get_available_versions(entry, cfg, force=True)
            self.assertEqual([o["version"] for o in options], ["1.0.0", "0.9.0", "0.8.0"])
            self.assertTrue(options[0]["recommended"])  # 无兼容规则 → 推荐最新
            # 指定安装旧版本
            r = updater.install_mod(cfg, db, inst, mid, "client", version="0.8.0")
            self.assertEqual(r["action"], "installed", r)
            self.assertEqual(r["version"], "0.8.0")
            jars = list(mods.glob("*.jar"))
            self.assertEqual([p.name for p in jars], ["VerMod-1.7.10-0.8.0.jar"])
            # 指定版本更新到 0.9.0
            r = updater.update_mod(cfg, db, inst, mid, "client", version="0.9.0")
            self.assertEqual(r["action"], "updated", r)
            self.assertEqual([p.name for p in mods.glob("*.jar")], ["VerMod-1.7.10-0.9.0.jar"])
            # 更新到同一版本 → uptodate
            r = updater.update_mod(cfg, db, inst, mid, "client", version="0.9.0")
            self.assertEqual(r["action"], "uptodate", r)
            # 不存在的版本 → error
            r = updater.install_mod(cfg, db, inst, mid, "client", version="9.9.9")
            self.assertEqual(r["action"], "error")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestNameCnAndAutoSides(unittest.TestCase):
    def test_set_name_cn_preserved_on_merge(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_nc_"))
        try:
            from gtnhmod.db import ModsDB
            mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(mods)
            # ezminer 之类中文名为空的条目
            target = next((m for m in mods if not m["name_cn"]), None)
            if not target:
                self.skipTest("fixture无空中文名条目")
            r = updater.set_name_cn(db, target["id"], "我的中文名")
            self.assertEqual(r["action"], "saved")
            self.assertEqual(db.get(target["id"])["name_cn"], "我的中文名")
            # 刷新wiki后保留
            db.merge_wiki(mods)
            self.assertEqual(db.get(target["id"])["name_cn"], "我的中文名")
            # 清空 → 恢复wiki原文
            updater.set_name_cn(db, target["id"], "")
            db.merge_wiki(mods)
            self.assertEqual(db.get(target["id"])["name_cn"], target["name_cn"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_auto_install_sides(self):
        from gtnhmod.config import Config
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_as_"))
        try:
            cfg = Config(tmp)
            both = {"side": "both"}
            client = {"side": "client"}
            server = {"side": "server"}
            # 均未设置 → both 返回空+提示；单端返回空+提示
            sides, note = updater.auto_install_sides(both, cfg)
            self.assertEqual(sides, [])
            self.assertIn("未设置", note)
            sides, note = updater.auto_install_sides(client, cfg)
            self.assertEqual(sides, [])
            # 只设置客户端
            mods = tmp / "mods"
            mods.mkdir()
            cfg.set_mods_dir("client", mods)
            sides, note = updater.auto_install_sides(both, cfg)
            self.assertEqual(sides, ["client"])
            self.assertIn("服务端", note)  # 服务端未设置提示
            sides, note = updater.auto_install_sides(client, cfg)
            self.assertEqual(sides, ["client"])
            self.assertEqual(note, "")
            sides, note = updater.auto_install_sides(server, cfg)
            self.assertEqual(sides, [])
            # 两端都设置
            cfg.set_mods_dir("server", tmp / "mods2")
            (tmp / "mods2").mkdir()
            sides, note = updater.auto_install_sides(both, cfg)
            self.assertEqual(sides, ["client", "server"])
            self.assertEqual(note, "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_body_compat_rules(self):
        # GitHub release body 里的兼容说明参与推荐标记
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_body_"))
        try:
            from gtnhmod.config import Config
            from gtnhmod.sources import VersionOption

            class StubSource:
                def list_versions(self, *, force=False):
                    return [
                        VersionOption("0.8.0", body=""),
                        VersionOption("0.7.16", body=""),
                    ]

            cfg = Config(tmp)
            cfg.data["gtnh_version"] = "2.8.0"
            entry = {"id": "x", "name_en": "X", "source_type": "manual",
                     "source": {}, "compat": [
                         {"kind": "gtnh_min", "gtnh": "2.9.0", "mod_min": "0.8.0", "raw": ""}]}
            # wiki条目级规则：GTNH2.8.0不适用 → unknown → 推荐最新0.8.0
            import gtnhmod.updater as U
            orig = U.Source.from_entry
            U.Source.from_entry = staticmethod(lambda e, c: StubSource())
            try:
                opts = U.get_available_versions(entry, cfg)
            finally:
                U.Source.from_entry = orig
            self.assertTrue(opts[0]["recommended"])
            # 带body的版本：body说"GTNH 2.8.0 版本：模组 0.7.16 及以上版本" → 0.7.16兼容
            class StubSource2(StubSource):
                def list_versions(self, *, force=False):
                    return [
                        VersionOption("0.8.0", body=""),
                        VersionOption("0.7.16",
                                      body="* GTNH 2.8.0 版本：模组 0.7.16 及以上版本"),
                    ]
            U.Source.from_entry = staticmethod(lambda e, c: StubSource2())
            try:
                opts = U.get_available_versions(entry, cfg)
            finally:
                U.Source.from_entry = orig
            by_ver = {o["version"]: o for o in opts}
            self.assertEqual(by_ver["0.7.16"]["compat"], "compatible")
            self.assertTrue(by_ver["0.7.16"]["recommended"])
            self.assertFalse(by_ver["0.8.0"]["recommended"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMergeCustomCollision(unittest.TestCase):
    """自定义源与wiki条目同名时的合并行为 + db备份恢复。"""

    def _fresh(self):
        mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
        return mods

    def test_custom_merged_with_notice(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_mc_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            # 场景：wiki 尚无 WorldEdit 时用户添加了同名自定义源，之后 wiki 收录
            fresh_without = [m for m in self._fresh() if m["id"] != "worldedit"]
            db.merge_wiki(fresh_without)
            db.add_custom({"name_en": "WorldEdit", "name_cn": "我的创世神",
                           "side": "both", "source_type": "github",
                           "source": {"owner": "enginehub", "repo": "WorldEdit",
                                      "asset_regex": "", "exclude_regex": ""}})
            self.assertIsNotNone(db.get("worldedit"))
            changes = db.merge_wiki(self._fresh())
            # 下载页发布时间不是wiki字段，刷新wiki后仍保留
            e = db.get("worldedit")
            e["release_date"] = "2026-08-17T12:00:00Z"
            db.save()
            db.merge_wiki(self._fresh())
            self.assertEqual(db.get("worldedit")["release_date"], "2026-08-17T12:00:00Z")
            # 无重复id；自定义被合并进wiki条目；用户字段（中文名/源）保留
            ids = [m["id"] for m in db.mods]
            self.assertEqual(len(ids), len(set(ids)))
            e = db.get("worldedit")
            self.assertEqual(e.get("group"), "星门规则")  # 现在是wiki条目
            self.assertEqual(e["name_cn"], "我的创世神")   # 用户中文名保留
            self.assertEqual(e["source"]["owner"], "enginehub")  # 用户绑定的源保留
            self.assertTrue(any("已被 wiki 收录" in c for c in changes), changes)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_db_backup_and_recovery(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_bk_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(self._fresh())
            db.add_custom({"name_en": "MyCustom", "source_type": "manual"})
            db.add_custom({"name_en": "SecondOne", "source_type": "manual"})
            bak = tmp / "mods_db.json.bak"
            self.assertTrue(bak.exists(), "结构变更应留.bak")
            # 损坏主文件 → load 自动从 .bak 恢复（恢复到上次结构变更前的状态）
            (tmp / "mods_db.json").write_text("{broken json", encoding="utf-8")
            db2 = ModsDB(tmp / "mods_db.json")
            self.assertTrue(db2.get("mycustom"), "应从.bak恢复出自定义源")
            # 主文件缺失 → 同样恢复
            (tmp / "mods_db.json").unlink()
            db3 = ModsDB(tmp / "mods_db.json")
            self.assertTrue(db3.get("mycustom"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEmptyFreshGuard(unittest.TestCase):
    """wiki 解析结果为空时必须中止合并（Cloudflare 验证页事故防护）。"""

    def _fresh(self):
        mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
        return mods

    def test_empty_fresh_aborts_merge(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_ef_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(self._fresh())
            before = (tmp / "mods_db.json").read_text(encoding="utf-8")
            with self.assertRaises(ValueError):
                db.merge_wiki([])
            # 合并被取消：磁盘文件与内存条目都不能有任何改动
            self.assertEqual((tmp / "mods_db.json").read_text(encoding="utf-8"), before)
            self.assertFalse(any(m.get("wiki_removed") for m in db.mods))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestRefreshReleaseDates(unittest.TestCase):
    def test_refreshes_github_dates_without_downloading(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_release_date_"))
        try:
            from types import SimpleNamespace
            from unittest.mock import patch
            from gtnhmod.config import Config
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.add_custom({"name_en": "GithubMod", "source_type": "github",
                           "source": {"owner": "o", "repo": "r"}})
            db.add_custom({"name_en": "ManualMod", "source_type": "manual"})
            fake = SimpleNamespace(published_at="2026-08-17T12:00:00Z")
            with patch("gtnhmod.updater.Source.from_entry") as factory:
                factory.return_value.check.return_value = fake
                result = updater.refresh_release_dates(Config(tmp / "data"), db)
            self.assertEqual(result, {"updated": 1, "failed": 0})
            self.assertEqual(db.get("githubmod")["release_date"], fake.published_at)
            self.assertNotIn("release_date", db.get("manualmod"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestBindSource(unittest.TestCase):
    """多下载链接mod绑定下载源。"""

    @classmethod
    def setUpClass(cls):
        mods, _ = parse_wikitext(FIXTURE.read_text(encoding="utf-8"))
        cls.wiki_mods = mods

    def test_add_custom_curseforge(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_cf_custom_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            mid = db.add_custom({
                "name_en": "CustomCF", "source_type": "curseforge",
                "curseforge_url": "https://www.curseforge.com/minecraft/mc-mods/custom-cf",
            })
            e = db.get(mid)
            self.assertEqual(e["source_type"], "curseforge")
            self.assertEqual(e["urls"]["curseforge"],
                             "https://www.curseforge.com/minecraft/mc-mods/custom-cf")
            self.assertEqual(e["urls"]["links"][0]["label"], "curseforge")
            db.update_custom(mid, {"name_en": "EditedCF", "side": "client",
                                   "source_type": "manual", "source": {},
                                   "urls": {"curseforge": None, "links": []}})
            edited = db.get(mid)
            self.assertEqual(edited["name_en"], "EditedCF")
            self.assertEqual(edited["side"], "client")
            self.assertEqual(edited["source_type"], "manual")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bind_github(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_bind_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(self.wiki_mods)
            # WorldEdit 有多个github链接：默认绑定第一个(GTNH特供版)，改绑第二个(enginehub)
            e = db.get("worldedit")
            self.assertEqual(e["source"]["repo"], "worldedit-gtnh")
            links = e["urls"]["links"]
            enginehub = next(l["url"] for l in links if "enginehub" in l["url"])
            r = updater.bind_source(db, "worldedit", enginehub)
            self.assertEqual(r["action"], "bound", r)
            e = db.get("worldedit")
            self.assertEqual(e["source"]["owner"], "enginehub")
            self.assertEqual(e["source"]["repo"], "WorldEdit")
            self.assertTrue(e["source_override"])
            self.assertEqual(updater.current_source_url(e), enginehub)
            # 刷新wiki后绑定保留
            changes = db.merge_wiki(self.wiki_mods)
            e = db.get("worldedit")
            self.assertEqual(e["source"]["repo"], "WorldEdit")
            self.assertTrue(e["source_override"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bind_curseforge(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_bind2_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(self.wiki_mods)
            # Inputfix 只有curseforge链接
            e = db.get("inputfix")
            r = updater.bind_source(db, "inputfix", e["urls"]["curseforge"])
            self.assertEqual(r["action"], "bound")
            e = db.get("inputfix")
            self.assertEqual(e["source_type"], "curseforge")
            self.assertTrue(e["source_override"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bind_invalid(self):
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_bind3_"))
        try:
            from gtnhmod.db import ModsDB
            db = ModsDB(tmp / "mods_db.json")
            db.merge_wiki(self.wiki_mods)
            r = updater.bind_source(db, "worldedit", "https://example.com/x")
            self.assertEqual(r["action"], "error")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAtomicReplace(unittest.TestCase):
    def test_cross_drive_fallback(self):
        # 模拟跨盘：os.replace 抛 WinError 17 → 走复制+同盘替换
        tmp = Path(tempfile.mkdtemp(prefix="gtnh_xd_"))
        try:
            src = tmp / "a.jar"
            src.write_bytes(b"PK\x03\x04new")
            dst = tmp / "b.jar"
            import os as _os
            import gtnhmod.downloader as dl
            orig = _os.replace

            def fake_replace(a, b):
                if str(a).startswith(str(tmp)) and a.name.startswith(".dl_"):
                    raise OSError(17, "系统无法将文件移到不同的磁盘驱动器")
                return orig(a, b)
            _os.replace = fake_replace
            try:
                dl.os.replace = fake_replace  # downloader 模块内的 os
                dl.atomic_replace(src, dst)
            finally:
                _os.replace = orig
                dl.os.replace = orig
            self.assertTrue(dst.exists())
            self.assertEqual(dst.read_bytes(), b"PK\x03\x04new")
            self.assertFalse(src.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
