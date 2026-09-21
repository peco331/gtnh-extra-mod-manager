import unittest
from types import SimpleNamespace
from unittest import mock

from gtnhmod import cli


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
        )
        stub = SimpleNamespace(ui=ui, cfg=object(), db=db)

        with mock.patch.object(cli.wikimod, "fetch_and_parse", return_value=([], [])) as fetch:
            cli.CliApp.do_refresh_wiki(stub)

        self.assertTrue(fetch.call_args.kwargs["interactive"])
        self.assertIsNotNone(fetch.call_args.kwargs["progress_cb"])


if __name__ == "__main__":
    unittest.main()
