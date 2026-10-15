"""时间显示：zip 打包的占位时间不能显示成真实的本地更新时间。"""
import time
import unittest

from gtnhmod import utils


class TestFmtTs(unittest.TestCase):
    def test_zip_placeholder_time_is_unknown(self):
        # GTNH 自带资源包解压后的 mtime 是 DOS 纪元 1980-01-01
        self.assertEqual(utils.fmt_ts(315532800), "")
        self.assertEqual(utils.fmt_ts(0), "")

    def test_real_time_is_formatted(self):
        stamp = time.mktime((2026, 10, 15, 21, 30, 0, 0, 0, -1))
        self.assertEqual(utils.fmt_ts(stamp), "2026-10-15 21:30")


class TestMarkNewest(unittest.TestCase):
    def test_new_backup_is_strictly_newest_even_with_identical_clock(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"b{i}.zip" for i in range(3)]
            # 旧备份的时间不早于「现在」：等价于快速磁盘上同一时钟刻度内连续写入
            later = time.time_ns() + 3_600_000_000_000
            for p in paths:
                p.write_bytes(b"x")
                os.utime(p, ns=(later, later))
            utils.mark_newest(paths[-1])
            newest = max(paths, key=lambda p: p.stat().st_mtime_ns)
            self.assertEqual(newest, paths[-1])
            self.assertGreater(paths[-1].stat().st_mtime_ns, later)


if __name__ == "__main__":
    unittest.main()
