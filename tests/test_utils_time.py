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


if __name__ == "__main__":
    unittest.main()
