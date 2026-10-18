import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from gtnhmod.config import Config
from gtnhmod.db import ModsDB
from gtnhmod.installed import InstalledDB
from gtnhmod import updater
from gtnhmod.sources import DownloadCandidate, Source, UpdateInfo, VersionOption


def _make_dummy_jar(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("test.txt", "dummy content")
    path.write_bytes(buf.getvalue())


class TestUpdatePlan(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gtnh_plan_"))
        self.data = self.tmp / "data"
        self.client_mods = self.tmp / "client" / "mods"
        for d in (self.data, self.client_mods):
            d.mkdir(parents=True)
        self.cfg = Config(self.data)
        self.cfg.set_mods_dir("client", self.client_mods)
        self.db = ModsDB(self.data / "mods_db.json")
        self.installed = InstalledDB(self.data / "installed.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_build_update_plan(self):
        _make_dummy_jar(self.client_mods / "PlanMod-1.7.10-1.0.jar")
        mid = self.db.add_custom({
            "name_en": "PlanMod", "name_cn": "计划Mod", "side": "client",
            "source_type": "manual"
        })
        cand = DownloadCandidate("http://example.com/PlanMod-1.7.10-1.2.jar", "PlanMod-1.7.10-1.2.jar")
        opt = VersionOption("1.2", "v1.2", "fix bugs", "2026-09-22", [cand], prerelease=True, target_status="eligible")

        with patch.object(updater, "list_install_options", return_value=([opt.__dict__], None)):
            plan = updater.build_update_plan(self.cfg, self.db, self.installed, target_mod_ids=[mid])
            self.assertEqual(len(plan), 1)
            item = plan[0]
            self.assertEqual(item["mod_id"], mid)
            self.assertEqual(item["current_version"], "1.0")
            self.assertEqual(item["target_version"], "1.2")
            self.assertTrue(item["prerelease"])
            self.assertEqual(item["action"], "update")
            self.assertIn("prefetched", item)

    def _plan_for(self, file_name, options):
        _make_dummy_jar(self.client_mods / file_name)
        mid = self.db.add_custom({"name_en": file_name.split("-")[0], "side": "client",
                                  "source_type": "manual"})
        with patch.object(updater, "list_install_options",
                          return_value=([o.__dict__ for o in options], None)):
            return updater.build_update_plan(self.cfg, self.db, self.installed, target_mod_ids=[mid])

    def test_local_file_that_is_a_release_asset_takes_that_release_version(self):
        """programmablehatches-0.2.0p30.jar 就是 v0.2.0p30-beta 的资产，不是「本地版本较新」。"""
        def opt(tag, name):
            return VersionOption(tag, tag, "", "2026-10-16", [DownloadCandidate("http://x/" + name, name)],
                                 target_status="eligible")
        plan = self._plan_for("programmablehatches-0.2.0p30.jar",
                              [opt("v0.2.0p30-beta", "programmablehatches-0.2.0p30.jar")])
        self.assertEqual(plan[0]["action"], "uptodate")

    def test_older_release_asset_still_updates(self):
        def opt(tag, name):
            return VersionOption(tag, tag, "", "2026-10-16", [DownloadCandidate("http://x/" + name, name)],
                                 target_status="eligible")
        plan = self._plan_for("programmablehatches-0.2.0p29.jar",
                              [opt("v0.2.0p30-beta", "programmablehatches-0.2.0p30.jar"),
                               opt("v0.2.0p29-beta", "programmablehatches-0.2.0p29.jar")])
        self.assertEqual(plan[0]["action"], "update")

    def test_up_to_date_mod_is_not_manual_when_newest_release_has_no_jar(self):
        """Mineshot：最新发布没有附带 jar，但本地已是该版本，应显示已最新而非需手动处理。"""
        plan = self._plan_for("mineshot-v1.85-Multi.jar", [
            VersionOption("v1.85-Multiplayer", "v1.85-Multiplayer", "", "2026-02-19", None,
                          target_status="eligible")])
        self.assertEqual(plan[0]["action"], "uptodate")

    def test_single_check_status_uses_release_asset_identity(self):
        """右键单独检查与 CLI 报告也不能把 0.2.0p30（v0.2.0p30-beta 的资产）说成「本地高于上游」。"""
        info = UpdateInfo("v0.2.0p30-beta", [DownloadCandidate("http://x/a", "programmablehatches-0.2.0p30.jar")],
                          None, "now")
        state = {"file_name": "programmablehatches-0.2.0p30.jar", "version": "0.2.0p30"}
        self.assertEqual(updater.installed_status(state, info), "uptodate")
        self.assertEqual(updater.installed_status({"file_name": "x-0.2.0p29.jar", "version": "0.2.0p29"}, info),
                         "update")

    def test_one_query_exception_keeps_other_plan_items(self):
        first = self.db.add_custom({"name_en": "Broken", "side": "client",
                                    "source_type": "manual"})
        second = self.db.add_custom({"name_en": "Working", "side": "client",
                                     "source_type": "manual"})
        registry = {"client": {
            first: {"enabled": True, "locked": False, "name_en": "Broken", "version": "1.0"},
            second: {"enabled": True, "locked": False, "name_en": "Working", "version": "1.0"},
        }, "server": {}}
        def lookup(entry, *_args, **_kwargs):
            if entry["id"] == first:
                raise RuntimeError("source timeout")
            return [], None

        with patch.object(updater, "list_install_options", side_effect=lookup):
            plan = updater.build_update_plan(self.cfg, self.db, self.installed,
                                             registry=registry)

        self.assertEqual([(item["mod_id"], item["action"]) for item in plan],
                         [(first, "error"), (second, "manual")])
        self.assertIn("source timeout", plan[0]["note"])


if __name__ == "__main__":
    unittest.main()
