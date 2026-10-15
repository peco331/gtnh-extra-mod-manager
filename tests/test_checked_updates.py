"""检查与执行共享一次结果；执行入口不重新发现项目或查询上游。"""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from gtnhmod import gui, scanner, updater
from gtnhmod.config import Config
from gtnhmod.db import ModsDB
from gtnhmod.installed import InstalledDB
from gtnhmod.sources import DownloadCandidate


class TestCheckedUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gtnh_checked_updates_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = Config(self.root / "data")
        self.db = ModsDB(self.cfg.data_dir / "mods_db.json")
        self.inst = InstalledDB(self.cfg.data_dir / "installed.json")
        for side in ("client", "server"):
            folder = self.root / side
            folder.mkdir()
            self.cfg.data["mods_folders"][side] = str(folder)
        self.mid = self.db.add_custom({"name_en": "PlanMod", "side": "both",
            "source_type": "github", "source": {"owner": "example", "repo": "PlanMod"}})
        self.latest = self.db.add_custom({"name_en": "CurrentMod", "side": "client", "source_type": "manual"})
        self.jar(self.cfg.mods_dir("client") / "PlanMod-1.0.jar")
        self.jar(self.cfg.mods_dir("server") / "PlanMod-2.0.jar")
        self.jar(self.cfg.mods_dir("client") / "CurrentMod-2.0.jar")
        self.asset = self.root / "PlanMod-2.0.jar"
        self.jar(self.asset)

    @staticmethod
    def jar(path):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("test.txt", "fixture")

    def lookup(self, entry, *_args, **_kwargs):
        name = "PlanMod" if entry["id"] == self.mid else "CurrentMod"
        return [{"version": "2.0", "tag": "v2.0", "target_status": "eligible",
                 "body": "update", "published_at": "2026-10-15", "prerelease": True,
                 "candidates": [DownloadCandidate(str(self.asset), f"{name}-2.0.jar")]}], None

    def check(self):
        with patch.object(updater, "list_install_options", side_effect=self.lookup) as query:
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        self.assertEqual(query.call_count, 2, "双端同一 MOD 只查询一次")
        return snapshot

    def app(self, snapshot):
        app = gui.GuiApp.__new__(gui.GuiApp)
        app.cfg, app.db, app.installed = self.cfg, self.db, self.inst
        app._checked_updates, app.busy = snapshot, False
        app._merged_cache = None
        app.refresh_installed = lambda: setattr(app, "_merged_cache", None)
        app._log = Mock()
        app._sync_update_button = Mock()
        app._show_update_plan_dialog = Mock()
        app._set_busy = lambda value: setattr(app, "busy", value)
        app._run_async = lambda job, on_done=None: on_done(job()) if on_done else job()
        app._push_progress = Mock()
        app._download_progress_cb = Mock(return_value=None)
        app._on_update_done = Mock()
        return app

    def test_all_update_uses_only_checked_updates_without_scan_or_query(self):
        snapshot = self.check()
        app = self.app(snapshot)
        app._merged_registry()  # 真实窗口里列表已渲染，按钮数量复用这份结果
        with patch.object(scanner, "scan_folder", side_effect=AssertionError("unexpected rescan")), \
                patch.object(updater, "list_install_options", side_effect=AssertionError("unexpected query")):
            app.update_all()
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual([(p["mod_id"], p["sides"]) for p in plan], [(self.mid, ["client"])])
        self.assertIs(app._show_update_plan_dialog.call_args.kwargs["snapshot"], snapshot)

    def test_execution_installs_prefetched_asset_without_querying(self):
        snapshot = self.check()
        app = self.app(snapshot)
        plan = [p for p in snapshot["plan"] if p["action"] == "update"]
        with patch.object(updater, "list_install_options", side_effect=AssertionError("unexpected query")):
            app._execute_update_plan(plan, checked_at=snapshot["checked_at"], snapshot=snapshot)
        self.assertTrue((self.cfg.mods_dir("client") / "PlanMod-2.0.jar").exists())
        self.assertFalse((self.cfg.mods_dir("client") / "PlanMod-1.0.jar").exists())
        self.assertEqual(app._on_update_done.call_args.args[0][0]["action"], "updated")
        self.assertFalse([p for p in app._checked_updates["plan"] if p["action"] == "update"])

    def test_nothing_marked_updatable_does_not_start_work(self):
        app = self.app(None)
        with patch.object(gui.messagebox, "showinfo") as info, \
                patch.object(updater, "build_update_plan") as query:
            app.update_all()
        info.assert_called_once()
        query.assert_not_called()
        app._show_update_plan_dialog.assert_not_called()

    def test_update_all_works_without_a_check_this_session(self):
        """列表里有「可更新」（上次记录的远端版本）就能全部更新，只为这些 MOD 获取下载信息。"""
        self.inst.touch_checked("client", self.mid, "2.0")
        app = self.app(None)
        with patch.object(updater, "list_install_options", side_effect=self.lookup) as query:
            app.update_all()
        self.assertEqual({c.args[0]["id"] for c in query.call_args_list}, {self.mid})
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual([(p["mod_id"], p["sides"]) for p in plan], [(self.mid, ["client"])])

    def test_update_button_follows_updatable_marks(self):
        app = self.app(None)
        app.btn_update_all = Mock()
        app._sync_update_button = gui.GuiApp._sync_update_button.__get__(app)
        app._sync_update_button()
        self.assertEqual(app.btn_update_all.configure.call_args.kwargs["state"], "disabled")
        self.inst.touch_checked("client", self.mid, "2.0")
        app._merged_cache = None
        app._sync_update_button()
        self.assertEqual(app.btn_update_all.configure.call_args.kwargs,
                         {"text": "全部更新 (1)", "state": "normal"})

    def test_result_does_not_expire_with_time(self):
        snapshot = self.check()
        snapshot["checked_at"] -= 6 * 3600  # 检查后过了几个小时，本地状态未变
        app = self.app(snapshot)
        with patch.object(gui.messagebox, "showinfo") as info, \
                patch.object(updater, "check_update_plan") as query:
            app.update_all()
        query.assert_not_called()
        info.assert_not_called()
        app._show_update_plan_dialog.assert_called_once()

    def add_second_update(self):
        other = self.db.add_custom({"name_en": "OtherMod", "side": "client",
            "source_type": "github", "source": {"owner": "example", "repo": "OtherMod"}})
        self.jar(self.cfg.mods_dir("client") / "OtherMod-1.0.jar")
        self.other_asset = self.root / "OtherMod-2.0.jar"
        self.jar(self.other_asset)
        original = self.lookup

        def lookup(entry, *args, **kwargs):
            if entry["id"] != other:
                return original(entry, *args, **kwargs)
            return [{"version": "2.0", "tag": "v2.0", "target_status": "eligible", "body": "",
                     "published_at": "2026-10-15", "prerelease": False,
                     "candidates": [DownloadCandidate(str(self.other_asset), "OtherMod-2.0.jar")]}], None
        self.lookup = lookup
        return other

    def test_updating_selected_keeps_the_rest_for_update_all(self):
        other = self.add_second_update()
        with patch.object(updater, "list_install_options", side_effect=self.lookup):
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        app = self.app(snapshot)
        first = [p for p in snapshot["plan"] if p["action"] == "update" and p["mod_id"] == self.mid]
        with patch.object(updater, "list_install_options", side_effect=AssertionError("unexpected query")):
            app._execute_update_plan(first, snapshot=snapshot)
        self.assertNotIn(self.mid, {p["mod_id"] for p in app._checked_updates["plan"]})
        app.busy = False  # 上一批更新完成
        app.update_all()
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual([p["mod_id"] for p in plan], [other])

    def test_single_mod_recheck_keeps_other_results(self):
        other = self.add_second_update()
        with patch.object(updater, "list_install_options", side_effect=self.lookup):
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        app = self.app(snapshot)
        app._on_check_done = Mock()
        with patch.object(updater, "check_updates", return_value=[]):
            app._check_single({"mod_id": self.mid, "name_en": "PlanMod"})
        app.busy = False
        with patch.object(updater, "list_install_options", side_effect=self.lookup) as query:
            app.update_all()
        # 其余结果直接复用，只重新获取刚单独检查过的那个
        self.assertEqual({c.args[0]["id"] for c in query.call_args_list}, {self.mid})
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual({p["mod_id"] for p in plan}, {other, self.mid})

    def test_item_locked_after_check_is_left_out_quietly(self):
        other = self.add_second_update()
        with patch.object(updater, "list_install_options", side_effect=self.lookup):
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        self.inst.set("client", self.mid, locked=True)  # 锁定后不再算「可更新」
        app = self.app(snapshot)
        with patch.object(gui.messagebox, "showinfo") as info, \
                patch.object(updater, "list_install_options", side_effect=AssertionError("unexpected query")):
            app.update_all()
        info.assert_not_called()
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual([p["mod_id"] for p in plan], [other])

    def test_file_changed_after_check_is_fetched_again_not_dropped(self):
        other = self.add_second_update()
        with patch.object(updater, "list_install_options", side_effect=self.lookup):
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        self.jar(self.cfg.mods_dir("client") / "PlanMod-1.0.jar")  # 检查后文件被替换
        app = self.app(snapshot)
        with patch.object(updater, "list_install_options", side_effect=self.lookup) as query:
            app.update_all()
        self.assertEqual({c.args[0]["id"] for c in query.call_args_list}, {self.mid})
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertEqual({p["mod_id"] for p in plan}, {other, self.mid})

    def test_changed_file_source_lock_and_directory_require_recheck(self):
        for change in ("file", "source", "lock", "directory", "disable"):
            with self.subTest(change=change):
                snapshot = self.check()
                plan = [p for p in snapshot["plan"] if p["action"] == "update"]
                path = self.cfg.mods_dir("client") / "PlanMod-1.0.jar"
                if change == "file":
                    path.write_bytes(b"changed")
                elif change == "source":
                    self.db.get(self.mid)["source"]["repo"] = "Changed"
                elif change == "lock":
                    self.inst.set("client", self.mid, locked=True)
                elif change == "directory":
                    self.cfg.data["mods_folders"]["client"] = str(self.root / "missing")
                else:
                    path.rename(path.with_suffix(".jar.disabled"))
                self.assertIsNotNone(updater.validate_checked_updates(self.cfg, self.db, self.inst, snapshot, plan))
                self.cfg.data["mods_folders"]["client"] = str(self.root / "client")
                self.db.get(self.mid)["source"]["repo"] = "PlanMod"
                self.inst.set("client", self.mid, locked=False)
                disabled = path.with_suffix(".jar.disabled")
                if disabled.exists():
                    disabled.rename(path)
                self.jar(path)

    def test_newly_installed_mod_is_not_added_to_existing_update_plan(self):
        snapshot = self.check()
        new = self.db.add_custom({"name_en": "AddedLater", "source_type": "manual"})
        self.jar(self.cfg.mods_dir("client") / "AddedLater-1.0.jar")
        app = self.app(snapshot)
        app.update_all()
        plan = app._show_update_plan_dialog.call_args.args[0]
        self.assertNotIn(new, {p["mod_id"] for p in plan})

    def test_query_failure_and_ambiguous_asset_are_not_updated(self):
        def lookup(entry, *_args, **_kwargs):
            options, err = self.lookup(entry)
            if entry["id"] == self.mid:
                options[0]["asset_ambiguous"] = True
                return options, None
            return [], "rate limited"
        with patch.object(updater, "list_install_options", side_effect=lookup):
            snapshot = updater.check_update_plan(self.cfg, self.db, self.inst)
        self.assertEqual({p["action"] for p in snapshot["plan"]}, {"manual", "error"})
        app = self.app(snapshot)
        with patch.object(gui.messagebox, "showinfo"):
            app.update_all()
        app._show_update_plan_dialog.assert_not_called()
        mod = {"mod_id": self.latest, "status": "installed", "latest_version": "2.0"}
        self.assertEqual(app._checked_status(mod)["status"], "query_failed")

    def test_change_during_confirmation_prevents_execution(self):
        snapshot = self.check()
        plan = [p for p in snapshot["plan"] if p["action"] == "update"]
        app = self.app(snapshot)
        self.db.get(self.mid)["source"]["repo"] = "Other"
        with patch.object(gui.messagebox, "showinfo"), patch.object(updater, "update_mod") as update:
            app._execute_update_plan(plan, snapshot=snapshot)
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
