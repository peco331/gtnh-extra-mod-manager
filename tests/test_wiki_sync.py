import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gtnhmod import wiki
from gtnhmod.db import ModsDB
from gtnhmod.net import HttpError


class TestWikiSync(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(
            proxy=None,
            wiki_url="https://gtnh.huijiwiki.com/api.php",
            wiki_page="可添加MOD",
            data_dir=Path(tempfile.mkdtemp(prefix="gtnh_wiki_sync_")),
        )
        self.mods = [{"id": "a", "name_en": "A"}]
        self.text = ("== 星门规则模组 ==\n=== 功能增强 ===\n"
                     "{{可添加MOD表格行|模组英文名=A}}")

    def test_online_fetch_result_carries_network_response_evidence(self):
        with mock.patch.object(
                wiki, "_fetch_impersonate_wikitext", return_value=self.text
        ), mock.patch.object(wiki, "_write_wiki_cache"):
            result = wiki.fetch_wiki(self.cfg)

        self.assertEqual(result.source, "online")
        self.assertEqual(result.channel, "curl_cffi")
        self.assertEqual(result.request_count, 1)
        self.assertEqual(result.response_bytes, len(self.text.encode("utf-8")))
        self.assertEqual(len(result.text_hash), 64)

    def test_cloudflare_challenge_uses_the_visible_browser_capture(self):
        challenge = HttpError(-3, "需要人机验证")
        with mock.patch.multiple(
                wiki,
                _fetch_impersonate_wikitext=mock.Mock(side_effect=challenge),
                _fetch_api_wikitext=mock.Mock(side_effect=challenge),
                _fetch_curl_wikitext=mock.Mock(side_effect=challenge),
                _fetch_raw_wikitext=mock.Mock(side_effect=challenge),
        ), mock.patch.object(
                wiki, "fetch_wikitext_via_browser"
        ) as browser, mock.patch.object(wiki, "_write_wiki_cache"):
            def capture(*_args, request_counter=None, **_kwargs):
                request_counter()
                return self.text
            browser.side_effect = capture
            result = wiki.fetch_wiki(self.cfg, interactive=True)

        browser.assert_called_once()
        self.assertEqual(result.source, "online")
        self.assertEqual(result.channel, "browser")
        self.assertGreaterEqual(result.request_count, 2)

    def test_import_updates_its_own_timestamp_for_every_import(self):
        with tempfile.TemporaryDirectory(prefix="gtnh_wiki_import_") as tmp:
            db = ModsDB(Path(tmp) / "mods_db.json")
            entry = {
                "id": "a", "name_en": "A", "name_cn": "", "group": "星门规则",
                "category": "功能增强", "side": "both", "side_uncertain": False,
                "source_type": "manual", "source": {}, "desc": "", "detail": "",
                "urls": {}, "aliases": [], "wiki_removed": False,
            }
            with mock.patch("gtnhmod.utils.now_str", side_effect=["first", "second"]):
                db.merge_wiki([entry], update_fetched_at=False, preserve_missing=False)
                db.merge_wiki([{**entry}], update_fetched_at=False, preserve_missing=False)

            self.assertEqual(db.meta["wiki_imported_at"], "second")

    def test_suspicious_shrink_updates_present_entries_but_keeps_missing_entries(self):
        with tempfile.TemporaryDirectory(prefix="gtnh_wiki_shrink_") as tmp:
            db = ModsDB(Path(tmp) / "mods_db.json")
            old = [
                {"id": name, "name_en": name.upper(), "group": "星门规则",
                 "category": "功能增强", "side": "both", "side_uncertain": False,
                 "source_type": "manual", "source": {}, "desc": "old", "detail": "",
                 "urls": {}, "aliases": [], "wiki_removed": False}
                for name in ("a", "b", "c", "d")
            ]
            db.mods = old
            result = wiki.WikiSyncResult(
                mods=[{**old[0], "desc": "new"}], warnings=[], source="online",
                attempted_at="now", success_at="now", text_hash="x")

            applied = wiki.apply_wiki_result(self.cfg, db, result)

            self.assertTrue(applied.applied)
            self.assertEqual(len(db.wiki_mods()), 4)
            self.assertEqual(db.get("a")["desc"], "new")

    def test_cache_result_never_merges_or_advances_online_timestamp(self):
        db = SimpleNamespace(merge_wiki=mock.Mock())
        fetched = wiki.WikiFetchResult(
            text=self.text,
            cache_note="使用最近一次成功抓取的数据",
            source="cache",
            channel="cache",
            response_bytes=len(self.text.encode("utf-8")),
            request_count=4,
            text_hash="a" * 64,
        )
        with mock.patch.object(
                wiki, "fetch_wiki", return_value=fetched):
            result = wiki.sync_wiki(self.cfg, db)

        self.assertEqual(result.source, "cache")
        self.assertFalse(result.applied)
        db.merge_wiki.assert_not_called()

    def test_online_result_merges_only_when_explicitly_applied(self):
        db = SimpleNamespace(merge_wiki=mock.Mock(return_value=["changed"]))
        fetched = wiki.WikiFetchResult(
            text=self.text,
            cache_note=None,
            source="online",
            channel="curl_cffi",
            response_bytes=len(self.text.encode("utf-8")),
            request_count=1,
            text_hash="b" * 64,
        )
        with mock.patch.object(
                wiki, "fetch_wiki", return_value=fetched):
            result = wiki.sync_wiki(self.cfg, db, apply_merge=False)

        self.assertEqual(result.source, "online")
        self.assertFalse(result.applied)
        db.merge_wiki.assert_not_called()
        applied = wiki.apply_wiki_result(self.cfg, db, result)
        self.assertTrue(applied.applied)
        self.assertEqual(applied.changes, ["changed"])
        db.merge_wiki.assert_called_once_with(result.mods, update_fetched_at=True)

    def test_import_does_not_claim_an_online_fetch(self):
        db = SimpleNamespace(merge_wiki=mock.Mock(return_value=[]))
        result = wiki.wiki_result_from_text(
            self.text, source="import")

        applied = wiki.apply_wiki_result(self.cfg, db, result)

        self.assertEqual(applied.source, "import")
        self.assertIsNone(applied.success_at)
        db.merge_wiki.assert_called_once_with(result.mods, update_fetched_at=False)

    def test_invalid_import_preserves_existing_cache(self):
        db = SimpleNamespace(merge_wiki=mock.Mock())
        with mock.patch.object(wiki, "_write_wiki_cache") as write_cache:
            with self.assertRaises(HttpError):
                wiki.import_wiki_text(self.cfg, db, "<html>challenge</html>")

        db.merge_wiki.assert_not_called()
        write_cache.assert_not_called()

if __name__ == "__main__":
    unittest.main()
