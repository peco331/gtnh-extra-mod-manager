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
