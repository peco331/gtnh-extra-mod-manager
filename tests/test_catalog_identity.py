"""Wiki 名称改成链接后，目录身份、安装记录与磁盘识别必须保持一致。"""
import json
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from gtnhmod import updater, wiki
from gtnhmod.config import Config
from gtnhmod.db import ModsDB
from gtnhmod.installed import InstalledDB
from gtnhmod.sources import DownloadCandidate


def entry(name="GT-Simple-Wireless-Network", mid=None, **fields):
    result = dict(id=mid or wiki.make_id(name, ""), name_en=name, name_cn="",
                  group="非星门规则", category="非星门规则", side="both",
                  side_uncertain=False, source_type="github",
                  source={"owner": "example", "repo": "Demo", "asset_regex": ""},
                  urls={"github": "https://github.com/example/Demo"},
                  aliases=[], desc="", detail="", compat=[], wiki_removed=False)
    result.update(fields)
    return result


def duplicate_pair(name="GT-Simple-Wireless-Network", alias="gtswn", prefix="GTSWN"):
    old = entry(name, aliases=[alias], wiki_removed=True)
    raw = f"[[{prefix}:{name}|{name}]]"
    raw_id = "-".join("".join(c.lower() if c.isascii() and c.isalnum() else " "
                               for c in raw).split())
    current = entry(raw, mid=raw_id)
    return old, current


class CatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gtnh_identity_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(self.root / "data")
        self.cfg.data_dir.mkdir(parents=True)
        self.path = self.cfg.data_dir / "mods_db.json"
        self.inst = InstalledDB(self.cfg.data_dir / "installed.json")
        for side in ("client", "server"):
            folder = self.root / side
            folder.mkdir()
            self.cfg.data["mods_folders"][side] = str(folder)

    def load(self, entries):
        self.path.write_text(json.dumps({"meta": {"wiki_fetched_at": "original"},
                                         "mods": entries}), encoding="utf-8")
        return ModsDB(self.path)

    def test_link_target_and_formatting_do_not_change_generated_id(self):
        name = "GT-Simple-Wireless-Network"
        for text in (name, f"[[{name}]]", f"[[GTSWN:{name}|{name}]]",
                     f"<b>{name}</b><ref>reference</ref>"):
            with self.subTest(text=text):
                self.assertEqual(wiki.make_id(text, ""), "gt-simple-wireless-network")

    def test_cached_duplicates_merge_without_writing_until_saved(self):
        old, current = duplicate_pair()
        old.update(source_override=True, name_cn="用户中文名", name_cn_override=True)
        old["source"]["target_profile"] = "gtnh"
        current["aliases"] = ["new-alias"]
        db = self.load([current, old])
        original = self.path.read_bytes()
        self.assertEqual(len(db.wiki_mods()), 1)
        merged = db.get(old["id"])
        self.assertIs(db.get(current["id"]), merged)
        self.assertEqual(merged["name_en"], old["name_en"])
        self.assertEqual(merged["name_cn"], "用户中文名")
        self.assertEqual(merged["aliases"], ["gtswn", "new-alias"])
        self.assertEqual(merged["source"]["target_profile"], "gtnh")
        self.assertEqual(db.meta["wiki_fetched_at"], "original")
        self.assertEqual(self.path.read_bytes(), original)
        db.save(backup=True)
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), original)
        reloaded = ModsDB(self.path)
        self.assertFalse(reloaded.catalog_dirty)
        self.assertEqual(reloaded.id_redirects(), {current["id"]: old["id"]})

    def test_refresh_preserves_identity_bindings_and_aliases(self):
        old, current = duplicate_pair()
        old["wiki_removed"] = False
        old["source_override"] = True
        old["source"]["tag_regex"] = "GTNH"
        db = self.load([old])
        fresh = deepcopy(current)
        before = deepcopy(fresh)
        db.merge_wiki([fresh])
        self.assertEqual(fresh, before, "合并不得修改调用方的解析快照")
        self.assertEqual([m["id"] for m in db.all()], [old["id"]])
        self.assertEqual(db.get(old["id"])["source"]["tag_regex"], "GTNH")
        db.merge_wiki([entry(old["name_en"])])
        self.assertEqual(len(db.wiki_mods()), 1)
        self.assertEqual(db.get(current["id"])["aliases"], ["gtswn"])

    def test_single_legacy_link_id_keeps_installation_identity(self):
        _, current = duplicate_pair()
        db = self.load([current])
        plain = entry()
        db.merge_wiki([plain])
        self.assertEqual([e["id"] for e in db.all()], [current["id"]])
        self.assertEqual(db.get(plain["id"])["id"], current["id"])

    def test_manual_link_entry_without_project_address_stays_stable(self):
        old, current = duplicate_pair()
        current.update(source_type="manual", source={}, urls={})
        db = self.load([current])
        fresh = entry(source_type="manual", source={}, urls={})
        db.merge_wiki([fresh])
        self.assertEqual(len(db.all()), 1)
        self.assertEqual(db.get(old["id"])["id"], current["id"])

    def test_distinct_names_sharing_repository_stay_separate(self):
        db = self.load([entry("Base", wiki_removed=True), entry("BaseAddon")])
        self.assertEqual(len(db.all()), 2)
        self.assertFalse(db.id_redirects())

    def test_same_name_different_projects_stay_separate(self):
        old, current = duplicate_pair()
        old["source"]["repo"] = "Other"
        old["urls"]["github"] = "https://github.com/example/Other"
        db = self.load([old, current])
        self.assertEqual(len(db.all()), 2)
        self.assertNotIn(current["id"], db.id_redirects())

    def test_distinct_asset_bindings_and_multiple_active_entries_stay_separate(self):
        old, current = duplicate_pair()
        old["source"]["asset_regex"] = "^one"
        current["source"]["asset_regex"] = "^two"
        db = self.load([old, current])
        self.assertEqual(len(db.all()), 2)
        old["wiki_removed"] = False
        current["source"]["asset_regex"] = "^one"
        db = self.load([old, current])
        self.assertEqual(len(db.all()), 2)

    def test_conflicting_legacy_id_cannot_merge_unrelated_install_state(self):
        first = entry("First", legacy_ids=["second"])
        db = self.load([first, entry("Second")])
        self.assertEqual(db.entry_ids("first"), ["first"])

    def test_startup_migrates_both_sides_and_preserves_lock_and_latest_cache(self):
        old, current = duplicate_pair("Twist Space Technology Mod", "TwistSpaceTechnology", "TST")
        current["aliases"] = ["TwistSpaceTechnology"]
        db = self.load([old, current])
        for side in ("client", "server"):
            folder = self.cfg.mods_dir(side)
            (folder / "TwistSpaceTechnology-0.8.0-RC2.5.jar").write_bytes(b"jar")
            self.inst.set(side, old["id"], save=False, locked=True,
                          last_checked="2026-10-01", last_remote_version="0.7.0")
            self.inst.set(side, current["id"], save=False, locked=False,
                          last_checked="2026-10-15", last_remote_version="0.8.0-RC2.5")
        self.inst.save()
        updater.startup_maintenance(self.cfg, db, self.inst)
        rows = updater.build_merged_registry(self.cfg, db, self.inst)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["install_side"], "both")
        self.assertTrue(rows[0]["locked"])
        for side in ("client", "server"):
            self.assertIsNone(self.inst.get(side, current["id"]))
            rec = self.inst.get(side, old["id"])
            self.assertEqual(rec["last_remote_version"], "0.8.0-RC2.5")
            self.assertEqual(rec["parsed_version"], "0.8.0-RC2.5")
        self.assertEqual(updater.unmatched_files(self.cfg, db), {"client": [], "server": []})
        self.assertTrue(InstalledDB(self.inst.path).get("server", old["id"])["locked"])

    def test_unavailable_directory_does_not_lose_migrated_record(self):
        old, current = duplicate_pair()
        db = self.load([old, current])
        self.inst.set("client", current["id"], locked=True, file_name="gtswn-1.0.jar")
        self.cfg.data["mods_folders"] = {"client": str(self.root / "missing"), "server": ""}
        updater.startup_maintenance(self.cfg, db, self.inst)
        self.assertTrue(self.inst.get("client", old["id"])["locked"])

    def test_install_then_refresh_and_restart_remains_managed(self):
        for name, alias, prefix in (("Twist Space Technology Mod", "TwistSpaceTechnology", "TST"),
                                    ("Programmable Hatch Mod", "programmablehatches", "PH")):
            with self.subTest(name=name):
                old, current = duplicate_pair(name, alias, prefix)
                old["aliases"] = [alias]
                db = self.load([old, current])
                source = self.root / f"{alias}-1.0.jar"
                with zipfile.ZipFile(source, "w") as jar:
                    jar.writestr("fixture.txt", "identity regression")
                options = [{"version": "1.0", "candidates": [DownloadCandidate(str(source), source.name)],
                            "target_status": "eligible", "body": "", "published_at": "2026-10-15"}]
                for side in ("client", "server"):
                    result = updater.install_mod(self.cfg, db, self.inst, current["id"], side,
                                                 prefetched=(options, None))
                    self.assertEqual(result["action"], "installed", result)
                db.merge_wiki([entry(name)])
                reloaded = ModsDB(self.path)
                inst = InstalledDB(self.inst.path)
                updater.startup_maintenance(self.cfg, reloaded, inst)
                for side, files in updater.scan_sides(self.cfg, reloaded).items():
                    match = next(f for f in files if f.file_name == source.name)
                    self.assertEqual(match.mod_id, old["id"])
                    self.assertEqual(match.match_quality, "exact")

    def test_rebound_source_does_not_inherit_old_confirmation_or_tag_filter(self):
        old = entry()
        old["source"].update(target_profile="gtnh", tag_regex="old-tag")
        db = self.load([old])
        fresh = entry(source={"owner": "other", "repo": "Different"},
                      urls={"github": "https://github.com/other/Different"})
        db.merge_wiki([fresh])
        self.assertNotIn("target_profile", db.get(old["id"])["source"])
        self.assertNotIn("tag_regex", db.get(old["id"])["source"])

    def test_backup_under_legacy_id_restores_canonical_install_record(self):
        old, current = duplicate_pair()
        db = self.load([old, current])
        backup = self.cfg.backup_dir / "client" / current["id"] / "20261015_010000_gtswn-1.0.jar"
        backup.parent.mkdir(parents=True)
        with zipfile.ZipFile(backup, "w") as jar:
            jar.writestr("test.txt", "old version")
        installed_file = self.cfg.mods_dir("client") / "gtswn-2.0.jar"
        with zipfile.ZipFile(installed_file, "w") as jar:
            jar.writestr("test.txt", "current version")
        self.inst.set("client", old["id"], file_name=installed_file.name, locked=True)
        results = updater.rollback_mod(self.cfg, db, self.inst, old["id"], sides=("client",))
        self.assertEqual(results[0]["action"], "restored", results)
        self.assertTrue(backup.exists())
        self.assertTrue(self.inst.get("client", old["id"])["locked"])
        self.assertIsNone(self.inst.get("client", current["id"]))
        self.assertEqual(self.inst.get("client", old["id"])["parsed_version"], "1.0")


if __name__ == "__main__":
    unittest.main()
