import shutil
import tempfile
import unittest
from pathlib import Path

from gtnhmod.config import Config, detect_instance_paths


class TestSettingsDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gtnh_set_"))
        self.cfg = Config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_detect_prism_multimc_instance(self):
        inst_dir = self.tmp / "GTNH-2.7.0"
        mc_dir = inst_dir / ".minecraft"
        mods_dir = mc_dir / "mods"
        mods_dir.mkdir(parents=True)
        (inst_dir / "instance.cfg").write_text(
            "InstanceType=OneSix\nname=GT_New_Horizons_2.7.0\n", encoding="utf-8"
        )
        res = detect_instance_paths(inst_dir)
        self.assertEqual(res["client_mods"], mods_dir)
        self.assertEqual(res["gtnh_version"], "2.7.0")

    def test_detect_direct_mods_folder(self):
        mods_dir = self.tmp / "mods"
        mods_dir.mkdir()
        res = detect_instance_paths(mods_dir)
        self.assertEqual(res["client_mods"], mods_dir)


if __name__ == "__main__":
    unittest.main()
