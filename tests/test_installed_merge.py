"""installed.json 保存时与磁盘合并：本进程刚检查的结果不能被旧记录覆盖。"""
import tempfile
import unittest
from pathlib import Path

from gtnhmod import utils
from gtnhmod.installed import InstalledDB


class TestInstalledMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "installed.json"
        seed = InstalledDB(self.path)
        seed.set("client", "demo", file_name="demo-1.0.jar")

    def test_fresh_check_survives_disk_record_with_later_timestamp(self):
        """时区/系统时间不同时，磁盘上的旧检查时间字符串可能「更晚」，不能因此丢掉新结果。"""
        mine = InstalledDB(self.path)
        other = InstalledDB(self.path)
        other.set("client", "demo", last_checked="2999-01-01 00:00:00", last_remote_version="1.0")
        mine.touch_checked("client", "demo", "2.0")
        self.assertEqual(InstalledDB(self.path).get("client", "demo")["last_remote_version"], "2.0")

    def test_untouched_record_still_adopts_newer_check_from_another_process(self):
        mine = InstalledDB(self.path)
        other = InstalledDB(self.path)
        other.touch_checked("client", "demo", "3.0")
        mine.set("client", "other", file_name="other-1.0.jar")  # 本进程保存别的记录
        self.assertEqual(InstalledDB(self.path).get("client", "demo")["last_remote_version"], "3.0")


if __name__ == "__main__":
    unittest.main()
