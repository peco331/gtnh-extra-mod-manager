"""versions.py 单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod.versions import (  # noqa: E402
    VersionParseError, compare, max_version, parse_version, split_mc_mod_version,
)


class TestParse(unittest.TestCase):
    def test_equal_with_extra_zero(self):
        self.assertEqual(compare("1.0.0", "1.0.0.0"), 0)

    def test_numeric_segment(self):
        self.assertLess(compare("0.9", "0.10"), 0)

    def test_v_prefix(self):
        self.assertEqual(compare("v1.2.3", "1.2.3"), 0)

    def test_leading_zero_segments(self):
        self.assertEqual(compare("5.09.44.03", "5.9.44.3"), 0)

    def test_prerelease_order(self):
        self.assertLess(compare("1.0.0-alpha", "1.0.0"), 0)
        self.assertLess(compare("1.0.0-alpha", "1.0.0-beta"), 0)
        self.assertLess(compare("1.0.0-beta", "1.0.0-rc1"), 0)
        self.assertLess(compare("1.0.0-rc1", "1.0.0"), 0)

    def test_short_prerelease_letters(self):
        self.assertLess(compare("1.0.0a", "1.0.0b"), 0)
        self.assertLess(compare("1.0.0b", "1.0.0"), 0)

    def test_dev_earliest(self):
        self.assertLess(compare("1.0-dev", "1.0.0-alpha"), 0)

    def test_p_patch(self):
        self.assertLess(compare("0.2.0p05", "0.2.0p10"), 0)
        self.assertGreater(compare("0.2.0p05", "0.2.0"), 0)  # p 补丁比基础版新

    def test_unknown_suffix_ignored(self):
        self.assertEqual(compare("2.0.0-GTNH", "2.0.0"), 0)

    def test_plain_order(self):
        self.assertLess(compare("0.8.0", "0.9.1"), 0)
        self.assertLess(compare("0.9.1", "1.0.0"), 0)

    def test_rc_sequence(self):
        self.assertLess(compare("1.0.0-rc1", "1.0.0-rc2"), 0)
        self.assertLess(compare("1.0.0-rc2", "1.0.0"), 0)

    def test_prerelease_with_number(self):
        self.assertLess(compare("1.0.0-alpha2", "1.0.0-alpha10"), 0)
        self.assertLess(compare("1.0.0-alpha2", "1.0.0-beta"), 0)

    def test_mc_tag_prefix_stripped(self):
        # GitHub tag 带 MC 段：1.7.10-0.8.0 与 0.8.0 相等
        self.assertEqual(compare("1.7.10-0.8.0", "0.8.0"), 0)
        self.assertEqual(compare("MC1.7.10-0.8.0", "0.8.0"), 0)

    def test_version_like_prefixed_not_stripped(self):
        # 1.2.3-rc1 不应被当成 MC 前缀剥掉；数字段=(1,2,3)，预发布 rc 序号 (1,)
        v = parse_version("1.2.3-rc1")
        self.assertEqual(v.parts, (1, 2, 3))
        self.assertEqual(v.pre_kind, 3)
        self.assertEqual(v.pre_nums, (1,))

    def test_errors(self):
        for bad in ("", "  ", "abc", "dev-build", None):
            with self.assertRaises(VersionParseError):
                parse_version(bad)


class TestMaxVersion(unittest.TestCase):
    def test_max_version(self):
        self.assertEqual(max_version(["0.8.0", "0.9.1", "1.0.0"]), "1.0.0")

    def test_max_version_with_bad(self):
        self.assertEqual(max_version(["dev-build", "0.9.1", "0.2.0p05"]), "0.9.1")

    def test_all_bad(self):
        self.assertIsNone(max_version(["dev-build", "abc"]))


class TestSplit(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(split_mc_mod_version("GardenOfGlass-1.7.10-1.9.5"),
                         ("GardenOfGlass", "1.7.10", "1.9.5"))

    def test_name_with_dash(self):
        self.assertEqual(split_mc_mod_version("NeverEnoughCharacters-Rework-1.7.10-2.0.0"),
                         ("NeverEnoughCharacters-Rework", "1.7.10", "2.0.0"))

    def test_no_mc_segment(self):
        self.assertEqual(split_mc_mod_version("SomeMod-1.2.3"),
                         ("SomeMod", None, "1.2.3"))

    def test_mc_prefix_name(self):
        # MC1.7.10-FoamFix-0.10.2：MC段在最前，名字取下一段
        self.assertEqual(split_mc_mod_version("MC1.7.10-FoamFix-0.10.2"),
                         ("FoamFix", "MC1.7.10", "0.10.2"))

    def test_unknown_all(self):
        self.assertEqual(split_mc_mod_version("GTNH-Core-Mod"),
                         ("GTNH-Core-Mod", None, None))

    def test_empty(self):
        self.assertEqual(split_mc_mod_version(""), (None, None, None))

    def test_version_with_build_suffix(self):
        # GTNHModify_CutCorners-v1.3.17+2.9.0-beta-1 → 版本是完整后缀而非最后一段"1"
        self.assertEqual(
            split_mc_mod_version("GTNHModify_CutCorners-v1.3.17+2.9.0-beta-1"),
            ("GTNHModify_CutCorners", None, "v1.3.17+2.9.0-beta-1"))

    def test_name_without_version(self):
        self.assertEqual(split_mc_mod_version("GTNH-Core-Mod"),
                         ("GTNH-Core-Mod", None, None))

    def test_version_before_mc_anchor(self):
        # name-modver-mcver 命名（如 advanced_memory_card-1.0.1-1.7.10-GTNH）：
        # MC 锚点前一段像版本 → 版本整体保留（含 MC 段与后缀）
        self.assertEqual(split_mc_mod_version("advanced_memory_card-1.0.1-1.7.10-GTNH"),
                         ("advanced_memory_card", "1.7.10", "1.0.1-1.7.10-GTNH"))

    def test_version_before_mc_anchor_no_suffix(self):
        # 无后缀时 MC 段在末尾，走无MC段分支，版本同样整体保留
        self.assertEqual(split_mc_mod_version("modernmarkings-0.3.13-1.7.10"),
                         ("modernmarkings", None, "0.3.13-1.7.10"))


if __name__ == "__main__":
    unittest.main()
