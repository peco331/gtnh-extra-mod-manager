import tempfile
import unittest
from pathlib import Path

from gtnhmod.db import ModsDB


def _entry(desc="old"):
    return {
        "id": "demo",
        "name_en": "Demo",
        "name_cn": "",
        "group": "星门规则",
        "category": "功能增强",
        "side": "both",
        "side_uncertain": False,
        "source_type": "manual",
        "source": {},
        "desc": desc,
        "detail": "",
        "urls": {},
        "aliases": [],
        "wiki_removed": False,
    }


class TestWikiRefreshStatus(unittest.TestCase):
    def test_wiki_mods_excludes_custom_and_removed_entries(self):
        with tempfile.TemporaryDirectory(prefix="gtnh_wiki_status_") as tmp:
            db = ModsDB(Path(tmp) / "mods_db.json")
            db.mods = [
                _entry(),
                {**_entry(), "id": "custom", "group": "自定义"},
                {**_entry(), "id": "removed", "wiki_removed": True},
            ]

            self.assertEqual([m["id"] for m in db.wiki_mods()], ["demo"])

    def test_merge_reports_wiki_description_change(self):
        with tempfile.TemporaryDirectory(prefix="gtnh_wiki_status_") as tmp:
            db = ModsDB(Path(tmp) / "mods_db.json")
            db.mods = [_entry("old")]

            changes = db.merge_wiki([_entry("new")])

            self.assertTrue(any("Wiki资料已更新" in change for change in changes), changes)


if __name__ == "__main__":
    unittest.main()
