"""退出保护与事件轮询：只读查询不能拖住退出，失败不能杀死完成通知。"""
import queue
import threading
import unittest
from unittest import mock

from gtnhmod import gui


class TestGuiLifecycle(unittest.TestCase):
    def app(self):
        app = object.__new__(gui.GuiApp)
        app.root, app.cfg, app.mod_icons = mock.Mock(), mock.Mock(), mock.Mock()
        app.cfg.data = {}
        app.root.grab_current.return_value = None
        app.queue = queue.Queue()
        app.busy, app._closing, app._active_jobs = False, False, 0
        app._release_dates_cancel = threading.Event()
        app._wiki_view = app._pack_view = None
        app._log, app._set_status = mock.Mock(), mock.Mock()
        app._set_busy, app._report_cb_exc = mock.Mock(), mock.Mock()
        app._persist_view = mock.Mock()
        for name in ('inst_tree', 'add_tree', 'um_tree', 'cust_tree', 'pack_tree',
                     'pack_inst_tree', 'pack_um_tree', 'pack_custom_tree'):
            setattr(app, name, mock.Mock())
        return app

    def test_readonly_query_does_not_prevent_close(self):
        app = self.app()
        release = threading.Event()
        try:
            app._run_async(lambda: release.wait(2), blocking=False)
            self.assertEqual(app._active_jobs, 0)
            with mock.patch.object(gui.messagebox, 'showinfo') as info:
                app._on_close()
            app.root.destroy.assert_called_once()
            info.assert_not_called()
            self.assertTrue(app._release_dates_cancel.is_set())
            self.assertTrue(app._closing)
        finally:
            release.set()

    def test_file_operation_still_prevents_close(self):
        app = self.app()
        app._active_jobs = 1
        with mock.patch.object(gui.messagebox, 'showinfo') as info:
            app._on_close()
        info.assert_called_once()
        app.root.destroy.assert_not_called()
        self.assertFalse(app._closing)

    def test_background_result_cannot_release_another_job(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                app = self.app()
                app._active_jobs, app.busy = 2, True
                done = mock.Mock()
                def work():
                    if raises:
                        raise ValueError('query failed')
                    return 'date'
                app._run_async(work, done, blocking=False)
                app.queue.put(app.queue.get(timeout=2))
                app._poll_queue()
                self.assertEqual(app._active_jobs, 2)
                self.assertTrue(app.busy)
                app._set_busy.assert_not_called()
                done.assert_called_once_with(None if raises else 'date')

    def test_browser_sync_exception_cannot_stop_queue_rescheduling(self):
        app = self.app()
        app._wiki_view = mock.Mock()
        app._wiki_view.enable.side_effect = RuntimeError('browser state failed')
        app._poll_queue()
        app._report_cb_exc.assert_called_once()
        app.root.after.assert_called_once_with(100, app._poll_queue)
        done = mock.Mock()
        app._active_jobs = 1
        app.queue.put(('_done', done, 'finished'))
        app._poll_queue()
        done.assert_called_once_with('finished')
        self.assertEqual(app._active_jobs, 0)
        self.assertEqual(app.root.after.call_count, 2)

    def test_close_discards_queued_late_results(self):
        app = self.app()
        done = mock.Mock()
        app.queue.put(('_background_done', done, 'late'))
        app._on_close()
        app.root.after.reset_mock()
        app._poll_queue()
        done.assert_not_called()
        app.root.after.assert_not_called()
