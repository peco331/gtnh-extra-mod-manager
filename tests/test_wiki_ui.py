import unittest
from types import SimpleNamespace
from unittest import mock

from gtnhmod import gui


class TestWikiRefreshUiContract(unittest.TestCase):
    def test_wiki_done_finishes_after_merge_without_waiting_for_release_dates(self):
        """Wiki 目录合并完成后立即释放界面，不串行等待 GitHub 发布时间。"""
        entry = {"id": "demo", "name_en": "Demo"}
        db = SimpleNamespace(
            mods=[],
            merge_wiki=mock.Mock(return_value=[]),
            wiki_mods=mock.Mock(return_value=[entry]),
            custom_mods=mock.Mock(return_value=[]),
        )
        stub = SimpleNamespace(
            db=db,
            _new_ids=set(),
            logs=[],
            busy_states=[],
            _log=lambda msg: stub.logs.append(msg),
            _set_busy=lambda value: stub.busy_states.append(value),
            refresh_addable=mock.Mock(),
            _run_async=mock.Mock(side_effect=AssertionError("Wiki 刷新不应等待发布时间任务")),
        )

        gui.GuiApp._on_wiki_done(stub, ([entry], []))

        self.assertEqual(stub.busy_states[-1], False)
        stub.refresh_addable.assert_called_once()
        self.assertTrue(any("Wiki 在线完成" in msg for msg in stub.logs))


if __name__ == "__main__":
    unittest.main()
