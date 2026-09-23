import unittest
from types import SimpleNamespace
from unittest import mock

from gtnhmod import cli
from gtnhmod.wiki import WikiSyncResult


class TestWikiRefreshCliContract(unittest.TestCase):
    def test_refresh_uses_interactive_browser_fallback(self):
        ui = SimpleNamespace(
            info=mock.Mock(),
            warn=mock.Mock(),
            ok=mock.Mock(),
            error=mock.Mock(),
        )
        db = SimpleNamespace(
            merge_wiki=mock.Mock(return_value=[]),
            wiki_mods=mock.Mock(return_value=[]),
            custom_mods=mock.Mock(return_value=[]),
        )
        stub = SimpleNamespace(ui=ui, cfg=object(), db=db)

        result = WikiSyncResult(
            mods=[], warnings=["使用缓存"], source="cache", attempted_at="now",
            success_at=None, text_hash="abc", channel="cache",
            response_bytes=321, request_count=4)
        with mock.patch.object(cli.wikimod, "sync_wiki", return_value=result) as sync:
            cli.CliApp.do_refresh_wiki(stub)

        self.assertTrue(sync.call_args.kwargs["interactive"])
        self.assertIsNotNone(sync.call_args.kwargs["progress_cb"])
        db.merge_wiki.assert_not_called()
        self.assertTrue(any("未在线同步" in call.args[0] for call in ui.info.call_args_list))


if __name__ == "__main__":
    unittest.main()
