import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gtnhmod import gui
from gtnhmod.wiki import WikiSyncResult
from gtnhmod.db import ModsDB


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
            _run_async=mock.Mock(),
            _on_release_dates_done=mock.Mock(),
        )
        return entry, stub

    def test_wiki_done_finishes_after_merge_without_waiting_for_release_dates(self):
        """Wiki 目录合并完成后立即释放界面，不串行等待 GitHub 发布时间。"""
        entry = {"id": "demo", "name_en": "Demo"}
        db = SimpleNamespace(
            mods=[],
            all=mock.Mock(return_value=[entry]),
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
        stub._run_async.assert_called_once()
        job = stub._run_async.call_args.args[0]
        with mock.patch.object(gui.updater, "fetch_release_dates",
                               return_value={"dates": {}, "errors": {}, "failed": 0,
                                             "checked": 0}) as fetch:
            job()
        fetch.assert_called_once_with(stub.cfg, [entry])

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
        stub._run_async.assert_not_called()

    def test_release_dates_done_applies_results_without_releasing_another_task(self):
        _, stub = self._stub(object())
        result = {"dates": {"demo": "2026-10-04T07:00:00Z"},
                  "errors": {"other": "HTTP 403"}, "checked": 2, "failed": 1}
        with mock.patch.object(gui.updater, "apply_release_dates", return_value=1) as apply:
            gui.GuiApp._on_release_dates_done(stub, result)
        apply.assert_called_once_with(stub.db, result["dates"])
        self.assertEqual(stub.busy_states, [])
        stub.refresh_addable.assert_called_once()
        self.assertTrue(any("1 个" in msg and "1 个失败" in msg for msg in stub.logs))
        self.assertTrue(any("other" in msg and "HTTP 403" in msg for msg in stub.logs))

    def test_release_dates_task_failure_is_not_reported_as_success(self):
        _, stub = self._stub(object())
        gui.GuiApp._on_release_dates_done(stub, None)
        self.assertTrue(any("失败" in msg for msg in stub.logs))
        self.assertFalse(any("已更新" in msg for msg in stub.logs))
        self.assertEqual(stub.busy_states, [])

    def test_release_dates_callback_persists_and_redraws_the_date(self):
        with tempfile.TemporaryDirectory() as folder:
            db = ModsDB(Path(folder) / "mods_db.json")
            db.mods = [{"id": "demo", "source_type": "github",
                        "source": {"owner": "o", "repo": "r"}}]
            _, stub = self._stub(db)
            result = {"dates": {"demo": "2026-10-04T07:00:00Z"},
                      "checked": 1, "failed": 0, "errors": {}}
            gui.GuiApp._on_release_dates_done(stub, result)
            self.assertEqual(ModsDB(db.path).get("demo")["release_date"],
                             "2026-10-04T07:00:00Z")
            stub.refresh_addable.assert_called_once()

    def test_release_dates_callback_rejects_dates_from_a_rebound_source(self):
        old = {"id": "demo", "source_type": "github",
               "source": {"owner": "o", "repo": "old"}}
        current = {**old, "source": {"owner": "o", "repo": "new"}}
        db = SimpleNamespace(get=lambda _id: current)
        _, stub = self._stub(db)
        result = {"dates": {"demo": "2026-10-04T07:00:00Z"},
                  "checked": 1, "failed": 0, "errors": {}}
        with mock.patch.object(gui.updater, "apply_release_dates", return_value=0) as apply:
            gui.GuiApp._on_release_dates_done(stub, result, entries=[old])
        apply.assert_called_once_with(db, {})


if __name__ == "__main__":
    unittest.main()
