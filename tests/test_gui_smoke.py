"""GUI 冒烟测试（Windows 有桌面会话时运行；CI 的 windows-latest 满足）。

覆盖"对话框打开即抛异常"一类回归——如 v1.5.0 版本选择器的前向引用
NameError（确认按钮根本没绑定，只能靠人工发现）。
"""
import gc
import re
import shutil
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gtnhmod import gui  # noqa: E402
from gtnhmod import packs  # noqa: E402


def setUpModule():
    # 同一进程内反复销毁并重建 Tk 在 Windows Tcl 8.6 上会偶发初始化失败。
    # 模块共用一个解释器；每个测试类仍有独立 GuiApp、数据目录和控件。
    global _test_root
    _test_root = tk.Tk()


def tearDownModule():
    _test_root.destroy()
    gc.collect()


def _new_app(data_dir):
    with mock.patch.object(gui.tk, "Tk", return_value=_test_root):
        return gui.GuiApp(data_dir)


def _clear_app(app):
    for callback in app.root.tk.call("after", "info"):
        app.root.after_cancel(callback)
    for child in app.root.winfo_children():
        child.destroy()
    app.root.update_idletasks()


def _walk(widget):
    """递归枚举对话框控件，避免测试依赖具体的 Frame 嵌套层级。"""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _pump(root, predicate, timeout=2.0):
    """驱动 Tk 事件循环直到 predicate() 为真。

    ``show_pack_detail`` 一类弹窗不走 mainloop/wait_window，只 root.after 是
    不会执行的：测试必须自己 update() 才能让排队的回调跑起来。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        _time.sleep(0.01)
    return predicate()


def _opts():
    """三个模拟版本（推荐/普通/不适配），覆盖标记与过滤分支。"""
    def o(ver, **kw):
        base = {"version": ver, "tag": ver, "body": None, "candidates": [],
                "compat": "unknown", "recommended": False, "latest": False,
                "prerelease": False, "published_at": "2026-08-01T00:00:00Z"}
        base.update(kw)
        return base
    return [
        o("1.7.52", recommended=True, latest=True),
        o("1.7.50"),
        o("0.9.9", compat="incompatible"),
    ]


class TestGuiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_gui_"))
        cls.app = _new_app(cls.tmp)
        cls.app.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        _clear_app(cls.app)
        cls.app = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        gc.collect()

    def test_app_builds(self):
        self.assertTrue(self.app.inst_tree["columns"])

    def test_source_confirmation_requires_explicit_yes(self):
        entry = {'id': 'demo', 'name_en': 'Demo', 'source_type': 'github',
                 'source': {'owner': 'owner', 'repo': 'demo'}}
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=False), \
                mock.patch.object(gui.updater, 'confirm_gtnh_source') as confirm:
            self.app._confirm_gtnh_source(entry)
            confirm.assert_not_called()
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True), \
                mock.patch.object(gui.updater, 'confirm_gtnh_source',
                                  return_value={'action': 'confirmed'}) as confirm:
            self.app._confirm_gtnh_source(entry)
            confirm.assert_called_once_with(self.app.db, 'demo', True)

    def test_target_menu_only_prompts_for_unproven_sources(self):
        entries = [
            ({'id': 'wiki', 'name_en': 'Wiki', 'group': '星门规则',
              'source_type': 'github',
              'source': {'owner': 'owner', 'repo': 'Demo'}}, False),
            ({'id': 'official', 'name_en': 'Official', 'group': '自定义',
              'source_type': 'github',
              'source': {'owner': 'GTNewHorizons', 'repo': 'Demo'}}, False),
            ({'id': 'marked', 'name_en': 'Marked', 'group': '自定义',
              'source_type': 'github',
              'source': {'owner': 'owner', 'repo': 'Demo-GTNH'}}, False),
            ({'id': 'unknown', 'name_en': 'Unknown', 'group': '自定义',
              'source_type': 'github',
              'source': {'owner': 'owner', 'repo': 'Demo'}}, True),
        ]
        for entry, should_prompt in entries:
            with self.subTest(entry=entry['id']):
                menu = tk.Menu(self.app.root, tearoff=0)
                self.app._add_target_menu(menu, entry)
                end = menu.index('end')
                labels = [menu.entrycget(i, 'label')
                          for i in range(end + 1)] if end is not None else []
                self.assertEqual(
                    any('确认此源用于 GTNH' in label for label in labels),
                    should_prompt)

    def test_bulk_confirmation_handles_all_unproven_sources_once(self):
        entries = [
            {'id': 'wiki', 'name_en': 'Wiki', 'group': '星门规则',
             'source_type': 'github',
             'source': {'owner': 'owner', 'repo': 'Demo'}},
            {'id': 'one', 'name_en': 'One', 'group': '自定义',
             'source_type': 'github',
             'source': {'owner': 'owner', 'repo': 'One'}},
            {'id': 'two', 'name_en': 'Two', 'group': '自定义',
             'source_type': 'github',
             'source': {'owner': 'owner', 'repo': 'Two'}},
            {'id': 'confirmed', 'name_en': 'Confirmed', 'group': '自定义',
             'source_type': 'github',
             'source': {'owner': 'owner', 'repo': 'Confirmed',
                        'target_profile': 'gtnh'}},
        ]
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True) as ask, \
                mock.patch.object(gui.updater, 'confirm_gtnh_source',
                                  return_value={'action': 'confirmed'}) as confirm, \
                mock.patch.object(self.app, 'refresh_custom') as refresh:
            try:
                self.app._bulk_confirm_gtnh_sources(entries)
            except AttributeError as exc:
                self.fail(f"bulk GTNH source confirmation is missing: {exc}")
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(
            [call.args[1] for call in confirm.call_args_list],
            ['one', 'two'])
        refresh.assert_called_once_with()

    def test_version_picker_opens_and_confirms(self):
        """更新选择器预选最新版，且确认/双击/回车绑定完整。"""
        bindings = {}
        # 打开后自动确认（等待窗口内事件循环处理 preselect/重绘）
        def auto_confirm():
            for w in self.app.root.winfo_children():
                if not isinstance(w, tk.Toplevel):
                    continue
                controls = list(_walk(w))
                lb = next(c for c in controls if isinstance(c, tk.Listbox))
                confirm = next(c for c in controls
                               if isinstance(c, ttk.Button)
                               and c.cget("text") == "使用选中版本")
                bindings["double_click"] = bool(lb.bind("<Double-Button-1>"))
                bindings["return"] = bool(w.bind("<Return>"))
                bindings["selected"] = lb.curselection()
                confirm.invoke()
                return
        self.app.root.after(150, auto_confirm)
        ver = self.app._version_picker(_opts(), current="1.7.50", title="测试",
                                       prefer_latest=True)
        self.assertEqual(ver, "1.7.52")
        self.assertEqual(bindings.get("selected"), (0,))
        self.assertTrue(bindings.get("double_click"))
        self.assertTrue(bindings.get("return"))

    def test_update_picker_passes_prefetched_pair(self):
        """批量手选版本后仍向更新器传递 (options, error) 二元组。"""
        result = (_opts(), None)
        mod = {"mod_id": "demo", "name_en": "Demo",
               "sides": {"client": {"version": "1.7.50"}}}
        with mock.patch.object(self.app, "_version_picker", return_value="1.7.52") as picker:
            with mock.patch.object(self.app, "_run_update_one") as run_update:
                self.app._on_versions_for_update(result, mod)
        self.assertTrue(picker.call_args.kwargs["prefer_latest"])
        self.assertEqual(picker.call_args.kwargs["current"], "1.7.50")
        self.assertIs(run_update.call_args.kwargs["prefetched"], result)

    def test_update_plan_dialog_is_non_modal_and_cancel_recovers_busy(self):
        plan = [{
            "mod_id": "demo", "name": "Demo", "sides": ["client"],
            "current_version": "1.0", "target_version": "2.0",
            "prerelease": False, "action": "update", "note": "可更新",
            "prefetched": ([], None),
        }]
        confirmed = []
        self.app._show_update_plan_dialog(plan, lambda p, **kw: confirmed.append(p))
        top = next(
            w for w in self.app.root.winfo_children()
            if isinstance(w, tk.Toplevel) and w.title() == "确认更新计划"
        )
        try:
            self.assertIsNot(self.app.root.grab_current(), top)
            self.assertTrue(self.app.busy)
        finally:
            top.destroy()
        self.assertFalse(self.app.busy)
        self.assertEqual(confirmed, [])

    def test_install_picker_passes_prefetched_pair(self):
        """手选安装版本也保留版本列表的二元组契约。"""
        result = (_opts(), None)
        entry = {"id": "demo", "name_en": "Demo"}
        with mock.patch.object(self.app, "_version_picker", return_value="1.7.52"):
            with mock.patch.object(self.app, "_run_install") as run_install:
                self.app._on_versions_for_install(result, entry, ["client"], "")
        self.assertIs(run_install.call_args.kwargs["prefetched"], result)

    def test_version_picker_filter_and_compat(self):
        """「仅显示适配」过滤掉不适配版本后，仍能正常打开/关闭。"""
        observed = {}

        def apply_filter_and_close():
            for w in self.app.root.winfo_children():
                if not isinstance(w, tk.Toplevel):
                    continue
                controls = list(_walk(w))
                only_compat = next(c for c in controls
                                   if isinstance(c, ttk.Checkbutton)
                                   and c.cget("text") == "仅显示适配")
                lb = next(c for c in controls if isinstance(c, tk.Listbox))
                only_compat.invoke()
                observed["rows"] = lb.size()
                w.destroy()
                return

        self.app.root.after(120, apply_filter_and_close)
        ver = self.app._version_picker(_opts(), current=None, title="过滤测试")
        self.assertIsNone(ver)  # 自动关闭 → None（不抛异常即通过）
        self.assertEqual(observed.get("rows"), 2)


class TestPacksRefreshGuard(unittest.TestCase):
    def test_busy_refresh_does_not_touch_installation_database(self):
        app = mock.Mock(busy=True)
        with mock.patch.object(gui.packmod, "reconcile_installed") as reconcile:
            gui.GuiApp.refresh_packs(app)
        reconcile.assert_not_called()


class TestPacksTabSmoke(unittest.TestCase):
    """「资源包与光影」页签冒烟：对话框构建/菜单绑定/列表刷新不抛异常。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_gui_pk_"))
        cls.app = _new_app(cls.tmp)
        cls.app.root.update_idletasks()
        # 真实存在的安装目录：reconcile 会用磁盘扫描结果校正安装记录
        cls.rp_dir = Path(cls.tmp) / "resourcepacks"
        cls.rp_dir.mkdir(exist_ok=True)
        cls.app.cfg.set_pack_dir("resourcepack", cls.rp_dir)

    @classmethod
    def tearDownClass(cls):
        _clear_app(cls.app)
        cls.app = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        gc.collect()

    def _make_zip(self, name):
        import zipfile
        with zipfile.ZipFile(self.rp_dir / name, "w") as zf:
            zf.writestr("pack.mcmeta", "{}")

    def test_check_does_not_report_latest_on_failed_or_unknown_results(self):
        self._make_zip("Demo.Pack.v1.0.zip")
        self.app.refresh_packs()
        for action, detail in (("error", "查询失败"), ("manual", "需手动选择"),
                               ("skip", "无法比较版本")):
            plan = packs.InstallPlanItem("pack-demo", "Demo Pack", "resourcepack",
                                         action, detail=detail)
            with self.subTest(action=action), \
                 mock.patch.object(gui.packmod, "plan_install", return_value=[plan]), \
                 mock.patch.object(self.app, "_run_async", side_effect=lambda work, done: done(work())), \
                 mock.patch.object(gui.messagebox, "showinfo") as info:
                self.app.check_pack_updates()
            message = info.call_args.args[1]
            self.assertNotIn("均为最新", message)
            self.assertIn("1", message)

    def setUp(self):
        for w in self.app.root.winfo_children():
            if isinstance(w, tk.Toplevel):
                w.destroy()
        # 页签筛选状态是共享 Tk 变量：逐用例复位，避免相互影响
        self.app.pack_kind.set("resourcepack")
        self.app.pack_cat.set("")
        self.app.pack_search.set("")
        self.app.pack_only_installed.set(False)
        self.app._pack_updates = {}
        for f in self.rp_dir.iterdir():
            if f.is_file():
                f.unlink()
        from gtnhmod import packs_wiki as packwiki
        self.app.packs.merge_wiki([
            {"id": "pack-demo", "kind": packwiki.KIND_RESOURCE, "name_en": "Demo Pack",
             "name_cn": "", "category": "深色模式", "author": "Tester", "scope": "范围说明",
             "loader": "", "desc": "说明文字", "detail": "范围说明\n说明文字",
             "version_requirements": [{"version": "2.8.0", "relation": "below"}],
             "icon": None,
             "urls": {"links": [{"url": "https://github.com/a/b/releases", "label": "下载"}],
                      "github": "https://github.com/a/b/releases", "modrinth": None,
                      "curseforge": None,
                      "primary": "https://github.com/a/b/releases",
                      "direct": ["https://github.com/a/b/releases"]},
             "aliases": [], "local": False, "wiki_removed": False},
            {"id": "pack-manual", "kind": packwiki.KIND_RESOURCE, "name_en": "Manual Pack",
             "name_cn": "", "category": "升级与重制", "author": "", "scope": "",
             "loader": "", "desc": "", "detail": "", "version_requirements": [],
             "icon": None,
             "urls": {"links": [{"url": "https://www.curseforge.com/x", "label": "cf"}],
                      "github": None, "modrinth": None,
                      "curseforge": "https://www.curseforge.com/x",
                      "primary": "https://www.curseforge.com/x", "direct": []},
             "aliases": [], "local": False, "wiki_removed": False},
            {"id": "shader-demo", "kind": packwiki.KIND_SHADER, "name_en": "Demo Shader",
             "name_cn": "", "category": "推荐列表", "author": "", "scope": "",
             "loader": "Angelica", "desc": "", "detail": "", "version_requirements": [],
             "icon": None,
             "urls": {"links": [{"url": "https://modrinth.com/shader/demo", "label": "m"}],
                      "github": None, "modrinth": "https://modrinth.com/shader/demo",
                      "curseforge": None, "primary": "https://modrinth.com/shader/demo",
                      "direct": ["https://modrinth.com/shader/demo"]},
             "aliases": [], "local": False, "wiki_removed": False},
        ])

    def test_tab_exists_and_refreshes(self):
        titles = [self.app.nb.tab(i, "text") for i in range(len(self.app.nb.tabs()))]
        self.assertIn("资源包与光影", titles)
        self.app.refresh_packs()
        self.assertEqual(len(self.app.pack_tree.get_children()), 2)   # 默认只看资源包
        self.app.pack_kind.set("shader")
        self.app.refresh_packs()
        self.assertEqual(len(self.app.pack_tree.get_children()), 1)
        self.app.pack_kind.set("resourcepack")

    def test_category_filter_and_search(self):
        self.app.refresh_packs()
        self.app.pack_cat.set("深色模式")
        self.app.refresh_packs()
        self.assertEqual([e["id"] for e in self.app.pack_rows], ["pack-demo"])
        self.app.pack_cat.set("")
        self.app.pack_search.set("manual")
        self.app.refresh_packs()
        self.assertEqual([e["id"] for e in self.app.pack_rows], ["pack-manual"])
        self.app.pack_search.set("")

    def test_status_column_marks_updates(self):
        """磁盘上有包 + 检查更新记录了目标版本 → 状态列显示「可更新 → x」。"""
        self._make_zip("Demo.Pack.v1.0.zip")
        self.app.packs.set_installed("resourcepack", "pack-demo",
                                     {"file_name": "Demo.Pack.v1.0.zip", "version": "v1.0"})
        self.app._pack_updates = {("resourcepack", "pack-demo"): "v9.9"}
        self.app.refresh_packs()
        row = self.app.pack_tree.item("pack-demo", "values")
        self.assertIn("可更新", row[6])
        self.assertIn("v9.9", row[6])

    def test_removed_file_clears_installed_mark(self):
        """用户手动删掉包后，列表不再显示已安装（与 mod 的校正语义一致）。"""
        self._make_zip("Demo.Pack.v1.0.zip")
        self.app.packs.set_installed("resourcepack", "pack-demo",
                                     {"file_name": "Demo.Pack.v1.0.zip", "version": "v1.0"})
        self.app.refresh_packs()
        self.assertEqual(self.app.pack_tree.item("pack-demo", "values")[4], "是")
        (self.rp_dir / "Demo.Pack.v1.0.zip").unlink()
        self.app.refresh_packs()
        self.assertEqual(self.app.pack_tree.item("pack-demo", "values")[4], "否")

    def _non_modal(self):
        """把 _dialog 变成非模态：模态 grab 会阻塞事件泵，测试无法检查其内容。"""
        original = gui.GuiApp._dialog

        def wrapper(app, title, size="600x400", **kw):
            return original(app, title, size, modal=False)
        return mock.patch.object(gui.GuiApp, "_dialog", wrapper)

    def _first_toplevel(self):
        return next((w for w in self.app.root.winfo_children()
                     if isinstance(w, tk.Toplevel)), None)

    def test_detail_dialog_opens_for_every_entry_kind(self):
        """详情弹窗对「可自动下载」「只能手动下载」两种条目都要能构建。"""
        self.app.refresh_packs()
        for pack_id in ("pack-demo", "pack-manual"):
            with self.subTest(pack_id=pack_id), self._non_modal():
                self.app.pack_tree.selection_set(pack_id)
                self.app.show_pack_detail()
                self.assertTrue(_pump(self.app.root, lambda: self._first_toplevel()))
                top = self._first_toplevel()
                try:
                    self.assertIn("详情", top.title())
                    self.assertTrue(any(isinstance(c, tk.Text) for c in _walk(top)))
                finally:
                    top.destroy()

    def test_detail_dialog_installed_record_shown(self):
        self._make_zip("Demo.Pack.v1.0.zip")
        self.app.packs.set_installed("resourcepack", "pack-demo",
                                     {"file_name": "Demo.Pack.v1.0.zip", "version": "v1.0",
                                      "installed_at": "2026-01-01 00:00:00", "note": "提示"})
        self.app.refresh_packs()
        self.app.pack_tree.selection_set("pack-demo")
        with self._non_modal():
            self.app.show_pack_detail()
            self.assertTrue(_pump(self.app.root, lambda: self._first_toplevel()))
            top = self._first_toplevel()
            try:
                text = next(c for c in _walk(top) if isinstance(c, tk.Text))
                body = text.get("1.0", "end")
            finally:
                top.destroy()
        self.assertIn("Demo.Pack.v1.0.zip", body)
        self.assertIn("本地已装", body)

    def test_asset_picker_dialog_binds_without_nameerror(self):
        """资产选择弹窗必须能真正完成绑定。

        真实回归：gui.py 里用了 ``re.escape`` 却从未 ``import re``，
        点「绑定此文件」直接抛 ``NameError: name 're' is not defined``——
        只有走到真正的绑定分支才暴露，单纯打开弹窗不会。
        """
        from gtnhmod import net
        assets = [packs.PackAsset(url="https://x/Shadow.UI.v5.45.zip",
                                  file_name="Shadow.UI.v5.45.zip", version="v5.45",
                                  tag="v5.45", published_at="2026-09-24", source="github"),
                  packs.PackAsset(url="https://x/Shadow.UI.v5.45-Modernity.version.zip",
                                  file_name="Shadow.UI.v5.45-Modernity.version.zip",
                                  version="v5.45", tag="v5.45", published_at="2026-09-24",
                                  source="github")]
        result = packs.PackSourceResult(source="github", status="ok", assets=assets,
                                        url="https://github.com/Ranzuu/Shadow-UI",
                                        note="GitHub Ranzuu/Shadow-UI")
        entry = self.app.packs.get("pack-demo")
        with self._non_modal(), \
                mock.patch.object(gui.packmod, "list_assets", return_value=result), \
                mock.patch.object(self.app, "_run_async",
                                  side_effect=lambda job, done: done(job())):
            self.app._pack_asset_dialog(entry)
            self.assertTrue(_pump(self.app.root, lambda: self._first_toplevel()))
            top = self._first_toplevel()
            try:
                listbox = next(c for c in _walk(top) if isinstance(c, tk.Listbox))
                listbox.selection_clear(0, "end")
                listbox.selection_set(0)
                bind = next(c for c in _walk(top) if isinstance(c, ttk.Button)
                            and c.cget("text") == "绑定此文件")
                bind.invoke()
            finally:
                if top.winfo_exists():
                    top.destroy()
        saved = self.app.packs.get("pack-demo")
        self.assertEqual(saved.get("bound_source"), "https://github.com/Ranzuu/Shadow-UI")
        self.assertTrue(saved.get("asset_regex"))
        self.assertTrue(re.search(saved["asset_regex"], "Shadow.UI.v5.45.zip"))

    def test_source_dialog_uses_gui_re_module(self):
        """设置下载源弹窗内部的 re 依赖（同 re 未导入类回归）。"""
        entry = self.app.packs.get("pack-demo")
        with self._non_modal():
            self.app._pack_source_dialog(entry)
            self.assertTrue(_pump(self.app.root, lambda: self._first_toplevel()))
            top = self._first_toplevel()
            try:
                self.assertTrue(any(isinstance(c, ttk.Entry) for c in _walk(top)))
            finally:
                top.destroy()

    def test_backup_dialog_opens(self):
        with self._non_modal():
            self.app.pack_backup_dialog()
            self.assertTrue(_pump(self.app.root, lambda: self._first_toplevel()))
            top = self._first_toplevel()
            try:
                self.assertEqual(top.title(), "备份与恢复")
                # 备份列表控件存在
                self.assertTrue(any(isinstance(c, ttk.Treeview) for c in _walk(top)))
            finally:
                top.destroy()

    def test_context_menu_builds_without_error(self):
        menu = tk.Menu(self.app.root, tearoff=0)
        self.app._pack_menu(menu, "pack-demo")
        labels = [menu.entrycget(i, "label")
                  for i in range(menu.index("end") + 1)
                  if menu.type(i) != "separator"]
        for expected in ("详情", "安装/更新", "选择下载资产...", "打开下载页面", "检查更新"):
            self.assertIn(expected, labels)

    def test_refresh_without_wiki_data_is_safe(self):
        from gtnhmod import packs as packmod
        empty = packmod.PacksDB(Path(self.tmp) / "empty_packs.json")
        original = self.app.packs
        try:
            self.app.packs = empty
            self.app.refresh_packs()          # 不应抛异常
            self.assertEqual(self.app.pack_tree.get_children(), ())
        finally:
            self.app.packs = original

    def test_folder_label_uses_hint(self):
        self.app.refresh_packs()
        txt = self.app.pack_dir_label.cget("text")
        self.assertTrue(txt.startswith("安装目录："), txt)


if __name__ == "__main__":
    unittest.main()
