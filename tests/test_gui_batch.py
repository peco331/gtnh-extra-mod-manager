"""GUI 批量安装和更新计划的无界面回归测试。"""
from types import SimpleNamespace
import unittest
from unittest import mock

from gtnhmod import gui


def _sync_run_async(fn, on_done=None):
    """测试用：在调用线程完成后台工作，便于检查回收结果。"""
    result = fn()
    if on_done:
        on_done(result)


class TestGuiBatch(unittest.TestCase):
  def test_expired_update_plan_rechecks_before_execution(self):
    plan = [{"mod_id": "one", "name": "One", "sides": ["client"],
             "action": "update", "target_version": "2.0", "prefetched": ([], None)}]
    app = SimpleNamespace(
        cfg=object(), db=object(), installed=object(),
        _set_busy=lambda value: None, _log=lambda message: None,
        _run_async=_sync_run_async, _show_update_plan_dialog=mock.Mock(),
        _execute_update_plan=gui.GuiApp._execute_update_plan,
    )
    fresh_plan = [{**plan[0], "target_version": "3.0"}]
    with mock.patch.object(gui.time, "monotonic", return_value=1000), \
            mock.patch.object(gui.updater, "build_registry", return_value={}), \
            mock.patch.object(gui.updater, "build_update_plan", return_value=fresh_plan), \
            mock.patch.object(gui.updater, "update_mod") as update:
        gui.GuiApp._execute_update_plan(app, plan, checked_at=0)

    update.assert_not_called()
    app._show_update_plan_dialog.assert_called_once_with(
        fresh_plan, gui.GuiApp._execute_update_plan)

  def test_install_addable_forwards_every_selected_entry_to_batch(self):
    """主按钮多选时不能悄悄只安装列表第一项。"""
    selected = [
        {"id": "one", "name_en": "One"},
        {"id": "two", "name_en": "Two"},
    ]
    app = SimpleNamespace(busy=False, _selected_addable=lambda: selected)
    app._batch_install = mock.Mock()

    with mock.patch.object(gui.messagebox, "showinfo"):
        gui.GuiApp.install_addable(app)

    app._batch_install.assert_called_once_with(selected)


  def test_batch_install_records_every_item_and_keeps_going_after_failure(self):
    """一个 mod 或端别失败后，其余选项仍应执行并在结果中可见。"""
    entries = [
        {"id": "one", "name_en": "One", "side": "client"},
        {"id": "two", "name_en": "Two", "side": "client"},
    ]
    logs, results = [], []
    app = SimpleNamespace(
        busy=False,
        cfg=object(), db=object(), installed=object(),
        _selected_addable=lambda: [],
        _set_busy=lambda value: setattr(app, "busy", value),
        _log=logs.append,
        _push_progress=lambda *args: None,
        refresh_all=lambda: None,
        _offer_open_download_page=lambda entry: None,
        _run_async=_sync_run_async,
    )
    original_done = gui.GuiApp._on_install_batch_done
    app._on_install_batch_done = lambda rs: (results.extend(rs), original_done(app, rs))

    def install(cfg, db, installed, mod_id, side, **kwargs):
        if mod_id == "one":
            raise RuntimeError("download failed")
        return {"action": "installed", "version": "2.0"}

    with mock.patch.object(gui.messagebox, "askyesno", return_value=True), \
            mock.patch.object(gui.updater, "auto_install_sides", return_value=(["client"], "")), \
            mock.patch.object(gui.updater, "install_mod", side_effect=install):
        gui.GuiApp._batch_install(app, entries)

    self.assertEqual([r["name"] for r in results], ["One", "Two"])
    self.assertEqual(results[0]["action"], "error")
    self.assertEqual(results[1]["action"], "installed")
    self.assertFalse(app.busy)


  def test_update_plan_returns_all_plan_outcomes_and_recovers_busy_state(self):
    """确认后的计划要汇总已最新/手动/查询失败及实际更新结果。"""
    plan = [
        {"mod_id": "update", "name": "Update", "sides": ["client"],
         "action": "update", "target_version": "2.0", "prefetched": ([], None)},
        {"mod_id": "manual", "name": "Manual", "sides": ["server"],
         "action": "manual", "note": "需要手动下载"},
        {"mod_id": "failed", "name": "Failed", "sides": ["client"],
         "action": "error", "note": "查询失败: timeout"},
        {"mod_id": "current", "name": "Current", "sides": ["server"],
         "action": "uptodate", "note": "已最新"},
    ]
    captured = []
    app = SimpleNamespace(
        busy=False, cfg=object(), db=object(), installed=object(),
        _set_busy=lambda value: setattr(app, "busy", value),
        _log=lambda message: None,
        _push_progress=lambda *args: None,
        _download_progress_cb=lambda label: None,
        _run_async=_sync_run_async,
        _on_update_done=lambda rs: (captured.extend(rs), setattr(app, "busy", False)),
    )

    with mock.patch.object(gui.updater, "update_mod", return_value={"action": "updated", "from": "1.0", "to": "2.0"}):
        gui.GuiApp._execute_update_plan(app, plan)

    self.assertEqual([(r["name"], r["action"]) for r in captured], [
        ("Update", "updated"), ("Manual", "manual"),
        ("Failed", "error"), ("Current", "uptodate"),
    ])
    self.assertFalse(app.busy)
