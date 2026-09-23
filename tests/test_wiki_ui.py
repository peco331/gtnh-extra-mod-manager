import unittest
from types import SimpleNamespace
from unittest import mock

from gtnhmod import gui
from gtnhmod.wiki import WikiSyncResult


class TestWikiRefreshUiContract(unittest.TestCase):
    def _stub(self, db):
        entry = {"id": "demo", "name_en": "Demo"}
        stub = SimpleNamespace(
            cfg=object(),
            db=db,
            _new_ids=set(),
            logs=[],
            busy_states=[],
            _log=lambda msg: stub.logs.append(msg),
            _set_busy=lambda value: stub.busy_states.append(value),
            refresh_addable=mock.Mock(),
            _run_async=mock.Mock(side_effect=AssertionError("Wiki 刷新不应等待发布时间任务")),
        )
        return entry, stub

    def test_wiki_done_finishes_after_merge_without_waiting_for_release_dates(self):
        """Wiki 目录合并完成后立即释放界面，不串行等待 GitHub 发布时间。"""
        entry = {"id": "demo", "name_en": "Demo"}
        db = SimpleNamespace(
            mods=[],
            merge_wiki=mock.Mock(return_value=[]),
            wiki_mods=mock.Mock(return_value=[entry]),
            custom_mods=mock.Mock(return_value=[]),
        )
        entry, stub = self._stub(db)
        result = WikiSyncResult(
            mods=[entry], warnings=[], source="online", attempted_at="now",
            success_at="now", text_hash="abc", channel="curl_cffi",
            response_bytes=123, request_count=1)

        gui.GuiApp._on_wiki_done(stub, result)

        self.assertEqual(stub.busy_states[-1], False)
        stub.refresh_addable.assert_called_once()
        self.assertTrue(any("Wiki 在线完成" in msg for msg in stub.logs))
        self.assertTrue(any("curl_cffi" in msg and "123" in msg for msg in stub.logs))

    def test_cache_result_is_never_merged_and_never_reported_as_online(self):
        db = SimpleNamespace(
            mods=[],
            merge_wiki=mock.Mock(),
            wiki_mods=mock.Mock(return_value=[]),
            custom_mods=mock.Mock(return_value=[]),
        )
        _, stub = self._stub(db)
        result = WikiSyncResult(
            mods=[], warnings=["使用缓存"], source="cache", attempted_at="now",
            success_at=None, text_hash="abc", channel="cache",
            response_bytes=321, request_count=4)

        gui.GuiApp._on_wiki_done(stub, result)

        db.merge_wiki.assert_not_called()
        self.assertEqual(stub.busy_states[-1], False)
        self.assertTrue(any("未在线同步" in msg for msg in stub.logs))
        self.assertFalse(any("Wiki 在线完成" in msg for msg in stub.logs))


if __name__ == "__main__":
    unittest.main()
