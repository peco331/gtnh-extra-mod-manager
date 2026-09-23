"""Explicit update checks must not silently use stale release data."""
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from gtnhmod import net, utils
from gtnhmod.sources import GitHubSource


class TestNetworkFreshness(unittest.TestCase):
    def test_forced_check_rejects_stale_cache_after_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "releases.json"
            utils.atomic_write_json(cache_file, {
                "etag": '"old"', "fetched_at": time.time() - 86400,
                "data": [{"tag_name": "old"}],
            })
            error = urllib.error.HTTPError("https://example.invalid/releases", 429,
                                           "rate limited", None, None)
            with mock.patch.object(net, "_open_request", side_effect=error):
                with self.assertRaises(net.HttpError):
                    net.http_get_cached("https://example.invalid/releases",
                                        cache_file=cache_file, force=True)

    def test_forced_release_search_forces_tag_fallback(self):
        source = GitHubSource("owner", "repo", target_profile="gtnh")
        calls = []

        def api(path, _key, *, force=False):
            calls.append((path, force))
            if "/releases?" in path:
                return [], "fresh"
            if path.endswith("/tags"):
                return [{"name": "1.0"}], "fresh"
            return {"tag_name": "1.0", "published_at": "2026-09-23T00:00:00Z",
                    "assets": [{"name": "Mod-1.7.10-1.0.jar",
                                "browser_download_url": "https://example.invalid/mod.jar"}]}, "fresh"

        with mock.patch.object(source, "_api", side_effect=api):
            options = source.list_versions(force=True)

        self.assertEqual(options[0].version, "1.0")
        self.assertTrue(all(forced for _, forced in calls), calls)


if __name__ == "__main__":
    unittest.main()
