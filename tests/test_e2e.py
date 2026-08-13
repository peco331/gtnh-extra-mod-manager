"""端到端测试：假目录内完成 安装→检查→更新→备份→禁用→重扫 全链路。

使用 local_folder 源（无网络）。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod.config import Config  # noqa: E402
from gtnhmod.db import ModsDB  # noqa: E402
from gtnhmod.installed import InstalledDB  # noqa: E402
from gtnhmod import updater  # noqa: E402
from gtnhmod.scanner import scan_folder  # noqa: E402

JAR = b"PK\x03\x04" + b"\x00" * 64


def make_jar(path: Path, content: bytes = JAR) -> Path:
    path.write_bytes(content)
    return path


class TestE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_e2e_"))
        cls.data = cls.tmp / "data"
        cls.client_mods = cls.tmp / "client" / "mods"
        cls.server_mods = cls.tmp / "server" / "mods"
        cls.src_folder = cls.tmp / "my_source"   # local_folder 源目录
        for d in (cls.data, cls.client_mods, cls.server_mods, cls.src_folder):
            d.mkdir(parents=True)
        make_jar(cls.src_folder / "TestMod-1.7.10-1.0.0.jar")

        cls.cfg = Config(cls.data)
        cls.cfg.set_mods_dir("client", cls.client_mods)
        cls.cfg.set_mods_dir("server", cls.server_mods)
        cls.db = ModsDB(cls.data / "mods_db.json")
        cls.installed = InstalledDB(cls.data / "installed.json")
        cls.mod_id = cls.db.add_custom({
            "name_en": "TestMod", "name_cn": "测试mod", "side": "both",
            "source_type": "local_folder",
            "source": {"path": str(cls.src_folder), "name_regex": r"^TestMod"},
            "category": "自定义",
        })

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step_install(self):
        r = updater.install_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
        self.assertEqual(r["action"], "installed", r)
        self.assertEqual(r["version"], "1.0.0")
        files = list(self.client_mods.glob("*.jar"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "TestMod-1.7.10-1.0.0.jar")

    def step_check(self, expect):
        results = updater.check_updates(self.cfg, self.db, self.installed,
                                        sides=("client",), force=True)
        found = [r for r in results if r[0] == "client" and r[1] == self.mod_id]
        self.assertEqual(len(found), 1)
        info = found[0][2]
        self.assertEqual(info.latest_version, expect)

    def step_update(self):
        n_before = len(list((self.cfg.backup_dir / "client" / self.mod_id).glob("*.jar")))
        r = updater.update_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
        self.assertEqual(r["action"], "updated", r)
        self.assertEqual(r["to"], "1.1.0")
        # 旧文件已移除、新文件就位
        names = [p.name for p in self.client_mods.glob("*.jar*")]
        self.assertEqual(names, ["TestMod-1.7.10-1.1.0.jar"])
        # 备份存在（按端别/目录存放；比更新前多一份）
        backups = list((self.cfg.backup_dir / "client" / self.mod_id).glob("*.jar"))
        self.assertEqual(len(backups), n_before + 1)
        self.assertTrue(any("TestMod-1.7.10-1.0.0.jar" in b.name for b in backups))

    def test_full_chain(self):
        # 1. 安装 1.0.0
        self.step_install()
        # 2. 检查 → 1.0.0 已最新
        self.step_check("1.0.0")
        reg = updater.build_registry(self.cfg, self.db, self.installed)
        st = reg["client"][self.mod_id]
        self.assertEqual(st["status"], "installed")
        self.assertTrue(st["enabled"])
        # 3. 源目录出现 1.1.0 → 检查发现可更新 → 更新
        make_jar(self.src_folder / "TestMod-1.7.10-1.1.0.jar")
        self.step_check("1.1.0")
        self.assertEqual(updater.version_status("1.0.0", "1.1.0"), "update")
        self.step_update()
        # 4. 更新后再检查 → 已最新
        self.step_check("1.1.0")
        r = updater.update_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
        self.assertEqual(r["action"], "uptodate")
        # 5. 禁用（.jar.disabled）
        r = updater.set_enabled(self.cfg, self.db, self.installed, self.mod_id, "client", False)
        self.assertEqual(r["action"], "disabled")
        self.assertTrue((self.client_mods / "TestMod-1.7.10-1.1.0.jar.disabled").exists())
        # 服务端不受影响（独立开关）
        self.assertFalse(list(self.server_mods.glob("*.jar*")))
        # 6. 重新启用
        r = updater.set_enabled(self.cfg, self.db, self.installed, self.mod_id, "client", True)
        self.assertEqual(r["action"], "enabled")
        self.assertTrue((self.client_mods / "TestMod-1.7.10-1.1.0.jar").exists())
        # 7. 锁定后检查跳过
        updater.toggle_lock(self.installed, self.mod_id, "client")
        results = updater.check_updates(self.cfg, self.db, self.installed,
                                        sides=("client",), force=True)
        found = [r for r in results if r[1] == self.mod_id]
        self.assertEqual(found, [])
        updater.toggle_lock(self.installed, self.mod_id, "client")  # 解锁

    def test_install_to_server_and_side_warning(self):
        # 双端mod装到服务端无警告
        r = updater.install_mod(self.cfg, self.db, self.installed, self.mod_id, "server")
        self.assertEqual(r["action"], "installed")
        self.assertEqual(r.get("warning", ""), "")
        # client-only mod 装到服务端有警告
        cid = self.db.add_custom({
            "name_en": "ClientOnly", "name_cn": "纯客户端", "side": "client",
            "source_type": "manual",
        })
        try:
            r = updater.install_mod(self.cfg, self.db, self.installed, cid, "server")
            self.assertEqual(r["action"], "manual")  # manual 源无自动下载
            self.assertIn("专用", r.get("warning", ""))
        finally:
            self.db.remove_custom(cid)

    def test_merged_registry_three_side(self):
        # 两端都装 → 一行，install_side=both；仅客户端 → client
        r = updater.install_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
        self.assertEqual(r["action"], "installed", r)
        merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
        by_id = {m["mod_id"]: m for m in merged}
        m = by_id[self.mod_id]
        self.assertEqual(m["install_side"], "both")  # e2e 前面测试已装到服务端
        self.assertEqual(set(m["sides"]), {"client", "server"})
        self.assertNotIn("testmod2", by_id)
        # 仅客户端的mod
        cid = self.db.add_custom({"name_en": "OnlyClientMod", "source_type": "manual",
                                  "side": "client"})
        try:
            make_jar(self.client_mods / "OnlyClientMod-1.7.10-1.0.0.jar")
            merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
            by_id = {x["mod_id"]: x for x in merged}
            self.assertEqual(by_id[cid]["install_side"], "client")
        finally:
            self.db.remove_custom(cid)
            (self.client_mods / "OnlyClientMod-1.7.10-1.0.0.jar").unlink()

    def test_check_only_filter(self):
        # 只检查指定mod（only 过滤）
        make_jar(self.client_mods / "TestMod-1.7.10-1.0.0.jar")
        bid = self.db.add_custom({"name_en": "OnlyCheckB", "source_type": "local_folder",
                                  "source": {"path": str(self.src_folder), "name_regex": "^OnlyCheckB"}})
        make_jar(self.src_folder / "OnlyCheckB-1.7.10-1.0.0.jar")
        make_jar(self.client_mods / "OnlyCheckB-1.7.10-1.0.0.jar")
        try:
            results = updater.check_updates(self.cfg, self.db, self.installed,
                                            sides=("client",), force=True,
                                            only={self.mod_id})
            ids = {r[1] for r in results}
            self.assertEqual(ids, {self.mod_id})
        finally:
            self.db.remove_custom(bid)
            (self.client_mods / "OnlyCheckB-1.7.10-1.0.0.jar").unlink(missing_ok=True)
            (self.src_folder / "OnlyCheckB-1.7.10-1.0.0.jar").unlink(missing_ok=True)

    def test_delete_mod(self):
        # 删除：jar 移入备份目录加 .deleted 后缀，mods 目录清空，installed 记录移除
        jar = self.client_mods / "TestMod-1.7.10-1.0.0.jar"
        if not jar.exists():
            r = updater.install_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
            self.assertEqual(r["action"], "installed", r)
        self.assertTrue(jar.exists())
        r = updater.delete_mod(self.cfg, self.db, self.installed, self.mod_id,
                               sides=("client",))
        self.assertEqual(r["action"], "deleted", r)
        self.assertFalse(jar.exists())
        self.assertFalse(list(self.client_mods.glob("TestMod-*.jar*")))
        deleted = list((self.cfg.backup_dir / "client" / self.mod_id).glob("*.deleted"))
        self.assertEqual(len(deleted), 1, deleted)
        self.assertIsNone(self.installed.get("client", self.mod_id))
        # 日志记录了删除
        from gtnhmod import utils
        self.assertIn("删除 TestMod", utils.log_file_path(self.data).read_text(encoding="utf-8"))

    def test_duplicate_jars(self):
        # 同mod两个jar：注册表取版本最高者并标记重复；清理后只留一个
        for p in self.client_mods.glob("TestMod-*.jar*"):
            p.unlink()
        make_jar(self.client_mods / "TestMod-1.7.10-1.0.0.jar")
        make_jar(self.client_mods / "TestMod-1.7.10-1.0.5.jar")
        try:
            merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
            m = {x["mod_id"]: x for x in merged}[self.mod_id]
            self.assertEqual(m["sides"]["client"]["version"], "1.0.5")  # 显示最高版本
            self.assertIn("TestMod-1.7.10-1.0.0.jar",
                          m["duplicates"].get("client", []))
            # 清理重复：保留1.0.5，1.0.0进备份
            r = updater.cleanup_duplicates(self.cfg, self.db, self.installed, self.mod_id)
            self.assertEqual(r["action"], "cleaned", r)
            names = [p.name for p in self.client_mods.glob("TestMod-*.jar*")]
            self.assertEqual(names, ["TestMod-1.7.10-1.0.5.jar"])
            backups = list((self.cfg.backup_dir / "client" / self.mod_id).glob("*.jar"))
            self.assertTrue(any("1.0.0" in b.name for b in backups))
            # 更新时也会自动清理旧jar：放回一个旧jar，源出现新版后更新
            make_jar(self.client_mods / "TestMod-1.7.10-0.9.0.jar")
            make_jar(self.src_folder / "TestMod-1.7.10-1.0.9.jar")
            try:
                r = updater.update_mod(self.cfg, self.db, self.installed,
                                       self.mod_id, "client")
                self.assertEqual(r["action"], "updated", r)
                self.assertEqual(r["to"], "1.0.9")
                names = [p.name for p in self.client_mods.glob("TestMod-*.jar*")]
                self.assertEqual(names, ["TestMod-1.7.10-1.0.9.jar"], names)  # 0.9.0/1.0.5 被自动清理
            finally:
                (self.src_folder / "TestMod-1.7.10-1.0.9.jar").unlink(missing_ok=True)
        finally:
            for p in self.client_mods.glob("TestMod-*.jar*"):
                p.unlink()
            make_jar(self.client_mods / "TestMod-1.7.10-1.0.0.jar")  # 供后续测试

    def test_exclude_installed(self):
        # 剔除后从注册表消失；恢复忽略后重新出现（自给自足，不依赖其他测试顺序）
        if not list(self.client_mods.glob("*.jar*")):
            r = updater.install_mod(self.cfg, self.db, self.installed, self.mod_id, "client")
            self.assertEqual(r["action"], "installed", r)
        jar = (self.client_mods / "TestMod-1.7.10-1.0.0.jar")
        self.assertTrue(jar.exists())
        names = updater.exclude_installed(self.cfg, self.db, self.installed, self.mod_id)
        self.assertTrue(names)
        merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
        self.assertNotIn(self.mod_id, {m["mod_id"] for m in merged})
        # 文件仍在磁盘上，只是不显示
        self.assertTrue(jar.exists())
        for n in names:
            updater.unignore(self.cfg, n)
        merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
        self.assertIn(self.mod_id, {m["mod_id"] for m in merged})

    def test_uptodate_compare(self):
        self.assertEqual(updater.version_status("1.0.0", "1.0.0"), "uptodate")
        self.assertEqual(updater.version_status("1.0.0", "0.9.0"), "uptodate")
        self.assertEqual(updater.version_status("dev-build", "1.0.0"), "unknown")

    def test_operation_log(self):
        from gtnhmod import utils
        log = utils.log_file_path(self.data)
        self.assertTrue(log.exists())
        text = log.read_text(encoding="utf-8")
        # 前面的链路应该已记录：安装/更新/禁用/启用/剔除
        self.assertIn("安装 TestMod", text)
        self.assertIn("更新 TestMod", text)
        self.assertIn("禁用 TestMod", text)
        self.assertIn("启用 TestMod", text)
        self.assertIn("从列表剔除 TestMod", text)
        self.assertIn("添加自定义源", text)

    def test_update_all_only_outdated(self):
        # 源目录加 1.2.0 → 只更新需要更新的mod；uptodate的返回uptodate不下载
        make_jar(self.src_folder / "TestMod-1.7.10-1.2.0.jar")
        # 另加一个已最新的mod
        up2 = self.db.add_custom({"name_en": "UptodateMod", "source_type": "local_folder",
                                  "source": {"path": str(self.src_folder), "name_regex": "^Uptodate"}})
        make_jar(self.src_folder / "UptodateMod-1.7.10-1.2.0.jar")
        (self.client_mods / "UptodateMod-1.7.10-1.2.0.jar").write_bytes(b"PK\x03\x04")
        try:
            results = updater.update_all(self.cfg, self.db, self.installed,
                                         sides=("client",))
            by_mod = {}
            for r in results:
                by_mod.setdefault(r["mod_id"], []).append(r)
            tm = by_mod[self.mod_id]
            self.assertTrue(any(r["action"] == "updated" and r["to"] == "1.2.0" for r in tm))
            um = by_mod[up2]
            self.assertTrue(all(r["action"] == "uptodate" for r in um))
            # 未禁用、未锁定的mod才更新：锁定的被跳过
            updater.set_lock(self.installed, self.mod_id, "client", True)
            results = updater.update_all(self.cfg, self.db, self.installed, sides=("client",))
            self.assertFalse(any(r["mod_id"] == self.mod_id for r in results))
            updater.set_lock(self.installed, self.mod_id, "client", False)
        finally:
            self.db.remove_custom(up2)
            (self.client_mods / "UptodateMod-1.7.10-1.2.0.jar").unlink()

    def test_unmatched_and_register(self):
        # 放一个未知 jar → 未受管；注册后消失
        unknown = make_jar(self.client_mods / "Strange-Thing-1.7.10-0.1.0.jar")
        um = updater.unmatched_files(self.cfg, self.db)
        self.assertTrue(any(f.file_name == unknown.name for f in um["client"]))
        eid = updater.register_unmanaged(self.cfg, self.db, "client", unknown.name,
                                         side_override="both")
        um = updater.unmatched_files(self.cfg, self.db)
        self.assertFalse(any(f.file_name == unknown.name for f in um["client"]))
        self.db.remove_custom(eid)


if __name__ == "__main__":
    unittest.main()
