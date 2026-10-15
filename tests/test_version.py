"""版本号只有一个来源；发布构建与本地构建显示可区分的版本。"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import gtnhmod

ROOT = Path(__file__).resolve().parent.parent


class TestVersionSource(unittest.TestCase):
    def test_pyproject_reads_version_from_package(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        self.assertIsNone(re.search(r'^version\s*=', project, re.M), "pyproject 不应再写死版本号")
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn('version = {attr = "gtnhmod.__version__"}', text)

    def test_version_is_semver(self):
        self.assertRegex(gtnhmod.__version__, r"^\d+\.\d+\.\d+$")


class TestDisplayVersion(unittest.TestCase):
    def test_source_run_is_marked_dev(self):
        with mock.patch.object(gtnhmod, "BUILD", None):
            self.assertEqual(gtnhmod.display_version(), f"{gtnhmod.__version__}-dev")

    def test_release_build_shows_plain_version(self):
        with mock.patch.object(gtnhmod, "BUILD", {"commit": "abc1234", "dirty": False, "release": True}):
            self.assertEqual(gtnhmod.display_version(), gtnhmod.__version__)

    def test_local_build_shows_commit(self):
        with mock.patch.object(gtnhmod, "BUILD", {"commit": "abc1234", "dirty": False, "release": False}):
            self.assertEqual(gtnhmod.display_version(), f"{gtnhmod.__version__}-dev+abc1234")
        with mock.patch.object(gtnhmod, "BUILD", {"commit": "abc1234", "dirty": True, "release": False}):
            self.assertEqual(gtnhmod.display_version(), f"{gtnhmod.__version__}-dev+abc1234.dirty")


class TestReleaseNotes(unittest.TestCase):
    def test_current_version_has_changelog_section(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import release_notes
        finally:
            sys.path.pop(0)
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        notes = release_notes.section(text, gtnhmod.__version__)
        self.assertIn("### ", notes)
        self.assertNotIn("## 未发布", notes)


class TestReleaseGuard(unittest.TestCase):
    def test_release_build_refuses_mismatched_tag(self):
        target = ROOT / "gtnhmod" / "_build_info.py"
        existed = target.exists()
        env = dict(os.environ, GITHUB_REF_TYPE="tag", GITHUB_REF_NAME="v0.0.0")
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "write_build_info.py"),
                                 "--require-release"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.exists(), existed, "失败时不能留下构建信息")


if __name__ == "__main__":
    unittest.main()
