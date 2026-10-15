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
        app = gui.GuiApp(data_dir)
    # 默认冒烟测试不启动真实浏览器或访问上游；网页桥接单独隔离验证。
    app.add_view_mode.set('offline')
    app._show_addable_view()
    app.pack_nb.select(1)
    app.pack_view_mode.set('offline')
    app._show_pack_view()
    return app


def _clear_app(app):
    app._close_pack_view()
    app.mod_icons.close()
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

    def test_jar_icon_loads_in_background_and_is_reused_after_restart(self):
        import zipfile
        from tests.test_mod_icons import picture
        from gtnhmod.mod_icons import ModIconCache
        folder = self.tmp / 'icon-fixture'
        folder.mkdir(exist_ok=True)
        jar = folder / 'IconDemo-1.0.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('logo.png', picture('blue'))
        key, initial = self.app.mod_icons.request({'id': 'icon-demo'}, [jar])
        self.assertIs(initial, self.app.mod_icons.fallback)
        self.assertTrue(_pump(self.app.root, lambda: key in self.app.mod_icons.images))
        self.assertEqual(self.app.mod_icons.images[key].width(), 16)
        cache = self.app.cfg.data_dir / 'cache/mod_icons' / (key + '.png')
        self.assertTrue(cache.is_file())
        other = ModIconCache(self.app.root, self.app.cfg, mock.Mock())
        try:
            with mock.patch('gtnhmod.mod_icons.jar_icon') as read_jar, \
                 mock.patch('gtnhmod.mod_icons.download_icon') as fetch:
                other._load(key, '', (str(jar),), ('icondemo',), self.app.cfg.wiki_url, None)
            read_jar.assert_not_called()
            fetch.assert_not_called()
            self.assertTrue(other.emit.call_args.args[1])
        finally:
            other.close()
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('logo.png', picture('red', size=(64, 64)))
        updated, _ = self.app.mod_icons.request({'id': 'icon-demo'}, [jar])
        self.assertNotEqual(updated, key)
        self.assertTrue(_pump(self.app.root, lambda: updated in self.app.mod_icons.images))

    def test_mod_asset_picker_requires_and_returns_explicit_choice(self):
        from gtnhmod.sources import DownloadCandidate
        candidates = [DownloadCandidate('https://example.invalid/a.jar', 'a.jar'),
                      DownloadCandidate('https://example.invalid/b.jar', 'b.jar')]
        def choose_second():
            top = next(w for w in self.app.root.winfo_children()
                       if isinstance(w, tk.Toplevel) and w.title() == '选择下载资产')
            controls = list(_walk(top))
            lb = next(w for w in controls if isinstance(w, tk.Listbox))
            self.assertFalse(lb.curselection())
            lb.selection_set(1)
            next(w for w in controls if isinstance(w, ttk.Button)
                 and w.cget('text') == '使用选中文件').invoke()
        self.app.root.after(100, choose_second)
        self.assertIs(self.app._mod_asset_picker(candidates), candidates[1])

    def test_nested_dialog_restores_parent_modal_grab(self):
        parent = self.app._dialog('父窗口')
        child = self.app._dialog('子窗口')
        self.assertIs(self.app.root.grab_current(), child)
        child.destroy()
        self.assertIs(self.app.root.grab_current(), parent)
        parent.destroy()

    def test_settings_scrolls_at_minimum_window_size_and_save_stays_visible(self):
        old_geometry = self.app.root.geometry()
        old_tab = self.app.nb.index('current')
        try:
            self.app.root.deiconify()
            self.app.root.geometry('980x640')
            self.app.nb.select(5)
            self.app.root.update()
            canvas = self.app.settings_canvas
            region = tuple(float(v) for v in canvas.cget('scrollregion').split())
            self.assertGreater(region[3], canvas.winfo_height())
            self.assertTrue(self.app.settings_save_button.winfo_ismapped())
            canvas.yview_moveto(1)
            self.app.root.update()
            self.assertGreater(canvas.yview()[0], 0)
        finally:
            self.app.nb.select(old_tab)
            self.app.root.geometry(old_geometry)
            self.app.root.withdraw()

    def test_custom_source_regex_row_does_not_overlap_save_button(self):
        mid = self.app.db.add_custom({'name_en': 'ReviewDemo', 'source_type': 'github',
                  'source': {'owner': 'owner', 'repo': 'ReviewDemo', 'target_profile': 'gtnh',
                             'asset_regex': 'runtime', 'tag_regex': 'GTNH'}})
        try:
            self.app._custom_source_dialog(self.app.db.get(mid))
            top = next(w for w in self.app.root.winfo_children()
                       if isinstance(w, tk.Toplevel) and w.title() == '编辑自定义源')
            controls = list(_walk(top))
            regex = next(w for w in controls if isinstance(w, ttk.Entry)
                         and w.grid_info().get('row') == 5)
            save = next(w for w in controls if isinstance(w, ttk.Button) and w.cget('text') == '保存')
            self.assertEqual(regex.get(), 'runtime')
            self.assertNotEqual(regex.grid_info()['row'], save.grid_info()['row'])
            save.invoke()
            updated = self.app.db.get(mid)
            self.assertEqual(updated['source']['target_profile'], 'gtnh')
            self.assertEqual(updated['source']['tag_regex'], 'GTNH')
            self.assertEqual(updated['source']['asset_regex'], 'runtime')
        finally:
            self.app.db.remove_custom(mid)

    def test_manual_install_results_use_one_summary_without_browser_prompts(self):
        entry = {'id': 'demo', 'urls': {'github': 'https://github.com/owner/Demo'}}
        rows = [{'action': 'manual', 'side': s, 'name': 'Demo', 'note': '需手动', 'entry': entry}
                for s in ('client', 'server')]
        with mock.patch.object(self.app, '_install_summary_dialog') as summary, \
                mock.patch.object(gui.messagebox, 'askyesno') as ask, \
                mock.patch.object(self.app, 'refresh_all'):
            self.app._on_install_batch_done(rows)
        summary.assert_called_once_with(rows)
        ask.assert_not_called()

    def test_gui_logs_are_persisted(self):
        marker = 'review: simulated upstream request failed'
        self.app._log(marker)
        content = gui.utils.log_file_path(self.app.cfg.data_dir).read_text(encoding='utf-8')
        self.assertIn(marker, content)

    def test_install_summary_download_button_deduplicates_two_sides(self):
        entry = {'id': 'demo', 'name_en': 'Demo', 'source_type': 'github',
                 'source': {'owner': 'owner', 'repo': 'Demo'},
                 'urls': {'github': 'https://github.com/owner/Demo'}}
        rows = [{'action': 'manual', 'name': 'Demo', 'side': side, 'entry': entry}
                for side in ('client', 'server')]
        self.app._install_summary_dialog(rows)
        top = next(w for w in self.app.root.winfo_children()
                   if isinstance(w, tk.Toplevel) and w.title() == '安装结果')
        try:
            button = next(w for w in _walk(top) if isinstance(w, ttk.Button)
                          and w.cget('text') == '打开手动处理下载页')
            with mock.patch.object(gui.webbrowser, 'open') as open_page:
                button.invoke()
            open_page.assert_called_once_with('https://github.com/owner/Demo')
        finally:
            top.destroy()

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
        cls.sp_dir = Path(cls.tmp) / 'shaderpacks'
        cls.sp_dir.mkdir(exist_ok=True)
        cls.app.cfg.set_pack_dir('shader', cls.sp_dir)

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
        self.app.pack_only_uninstalled.set(False)
        self.app.pack_nb.select(1)
        self.app._invalidate_pack_checks()
        self.app.packs.packs = []
        self.app.packs.meta = {}
        self.app.cfg.data['ignored_pack_files'] = {}
        self.app._pack_updates = {}
        for f in self.rp_dir.iterdir():
            if f.is_file():
                f.unlink()
        for f in self.sp_dir.iterdir():
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

    def test_four_pages_split_installed_catalog_unmanaged_and_custom(self):
        import zipfile
        self._make_zip('Demo.Pack.v1.0.zip')
        with zipfile.ZipFile(self.sp_dir / 'Demo.Shader.v1.0.zip', 'w') as archive:
            archive.writestr('shaders/main.fsh', 'fixture')
        self.app.pack_kind.set('all')
        self.app.refresh_packs()
        self.assertEqual([self.app.pack_nb.tab(i, 'text') for i in range(4)],
                         ['已安装', '可添加', '未受管', '自定义源'])
        self.assertEqual(set(self.app.pack_inst_tree.get_children()), {'pack-demo', 'shader-demo'})
        self.app.pack_only_uninstalled.set(True)
        self.app.refresh_packs()
        self.assertEqual(self.app.pack_tree.get_children(), ('pack-manual',))

    def test_disable_lock_and_uninstall_keep_the_selected_pack_and_backup(self):
        self._make_zip('Demo.Pack.v1.0.zip')
        self.app.pack_nb.select(0)
        self.app.refresh_packs()
        self.app.pack_inst_tree.selection_set('pack-demo')
        self.app._change_pack_enabled(False)
        self.assertTrue((self.rp_dir / 'Demo.Pack.v1.0.zip.disabled').exists())
        self.assertEqual(self.app.pack_inst_tree.selection(), ('pack-demo',))
        self.app._toggle_pack_lock()
        self.assertTrue(self.app.packs.installed('resourcepack')['pack-demo']['locked'])
        self.app._change_pack_enabled(True)
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True), \
             mock.patch.object(gui.messagebox, 'showwarning') as warning:
            self.app.uninstall_selected_packs()
        warning.assert_not_called()
        self.assertFalse((self.rp_dir / 'Demo.Pack.v1.0.zip').exists())
        self.assertFalse(self.app.pack_inst_tree.exists('pack-demo'))
        self.assertTrue(packs.list_backups(self.app.cfg, 'resourcepack'))

    def _checked_snapshot(self):
        self._make_zip('Demo.Pack.v1.0.zip')
        self.app.refresh_packs()
        asset = packs.PackAsset('https://example.invalid/Demo.Pack.v2.0.zip',
                                 'Demo.Pack.v2.0.zip', version='2.0', source='github')
        result = packs.PackSourceResult('github', 'ok', [asset])
        with mock.patch.object(packs, 'list_assets', return_value=result):
            snapshot = packs.check_update_plan(self.app.cfg, self.app.packs)
        self.app._pack_checked_updates = snapshot
        self.app.pack_nb.select(0)
        self.app.refresh_packs()
        self.app.pack_inst_tree.selection_set('pack-demo')
        return snapshot

    def test_update_selected_consumes_checked_asset_without_a_second_query(self):
        snapshot = self._checked_snapshot()
        with mock.patch.object(gui.messagebox, 'askyesno', return_value=True), \
             mock.patch.object(packs, 'list_assets', side_effect=AssertionError('unexpected query')), \
             mock.patch.object(self.app, '_run_pack_installs') as execute:
            self.app.update_selected_packs()
        plans = execute.call_args.args[0]
        self.assertEqual([p.pack_id for p in plans], ['pack-demo'])
        self.assertEqual(plans[0].asset.url, snapshot['plan'][0].asset.url)
        self.assertIs(execute.call_args.kwargs['snapshot'], snapshot)
        self.assertIsNone(self.app._pack_checked_updates)

    def test_update_rechecks_local_state_after_confirmation(self):
        self._checked_snapshot()
        def changed(*_args):
            self.app.packs.update_entry('pack-demo', {'bound_source': 'https://github.com/changed/source'})
            return True
        with mock.patch.object(gui.messagebox, 'askyesno', side_effect=changed), \
             mock.patch.object(gui.messagebox, 'showinfo') as info, \
             mock.patch.object(self.app, '_run_pack_installs') as execute:
            self.app.update_selected_packs()
        execute.assert_not_called()
        self.assertIn('重新检查', info.call_args.args[1])

    def test_update_all_requires_a_current_check(self):
        with mock.patch.object(gui.messagebox, 'showinfo') as info, \
             mock.patch.object(packs, 'list_assets') as query, \
             mock.patch.object(self.app, '_run_pack_installs') as execute:
            self.app.update_all_packs()
        query.assert_not_called()
        execute.assert_not_called()
        self.assertIn('检查更新', info.call_args.args[1])

    def test_unmanaged_ignore_and_restore_never_delete_the_file(self):
        self._make_zip('Unknown.Pack.v1.0.zip')
        self.app.pack_nb.select(2)
        self.app.refresh_packs()
        iid = self.app.pack_um_tree.get_children()[0]
        self.app.pack_um_tree.selection_set(iid)
        self.app.ignore_selected_packs()
        self.assertEqual(self.app.pack_um_tree.get_children(), ())
        self.assertTrue((self.rp_dir / 'Unknown.Pack.v1.0.zip').exists())
        self.app.restore_ignored_packs()
        self.assertEqual(len(self.app.pack_um_tree.get_children()), 1)

    def test_association_never_claims_an_unrelated_same_version_file(self):
        self._make_zip('Short.v1.0.zip')
        self._make_zip('Other.v1.0.zip')
        self.app.pack_nb.select(2)
        self.app.refresh_packs()
        self.app.pack_um_tree.selection_set('resourcepack:Short.v1.0.zip')
        with self._non_modal():
            self.app.associate_selected_pack()
            top = self._first_toplevel()
            combo = next(w for w in _walk(top) if isinstance(w, ttk.Combobox))
            combo.current(0)
            next(w for w in _walk(top) if isinstance(w, ttk.Button) and w.cget('text') == '关联').invoke()
        self.assertEqual(self.app.packs.installed('resourcepack')['pack-demo']['file_name'], 'Short.v1.0.zip')
        self.assertEqual(self.app.pack_um_tree.get_children(), ('resourcepack:Other.v1.0.zip',))

    def test_checked_update_downloads_over_http_once_and_keeps_old_backup(self):
        import io
        import threading
        import zipfile
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        body = io.BytesIO()
        with zipfile.ZipFile(body, 'w') as archive:
            archive.writestr('pack.mcmeta', '{}')
            archive.writestr('assets/version.txt', '2.0')
        data, requested = body.getvalue(), []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self._make_zip('Demo.Pack.v1.0.zip')
        self._make_zip('Unrelated.zip')
        old = (self.rp_dir / 'Demo.Pack.v1.0.zip').read_bytes()
        asset = packs.PackAsset(f'http://127.0.0.1:{server.server_port}/Demo.Pack.v2.0.zip',
            'Demo.Pack.v2.0.zip', version='2.0', size=len(data), source='github')
        result = packs.PackSourceResult('github', 'ok', [asset])
        proxy = self.app.cfg.data['proxy']
        self.app.cfg.data['proxy'] = {'host': ''}
        try:
            with mock.patch.object(packs, 'list_assets', return_value=result):
                self.app._pack_checked_updates = packs.check_update_plan(self.app.cfg, self.app.packs)
            self.app.pack_nb.select(0)
            self.app.refresh_packs()
            self.app.pack_inst_tree.selection_set('pack-demo')
            with mock.patch.object(gui.messagebox, 'askyesno', return_value=True), \
                 mock.patch.object(gui.messagebox, 'showwarning') as warning, \
                 mock.patch.object(packs, 'list_assets', side_effect=AssertionError('must reuse checked asset')), \
                 mock.patch.object(self.app, '_run_async', side_effect=lambda job, done: done(job())):
                self.app.update_selected_packs()
            warning.assert_not_called()
        finally:
            self.app.cfg.data['proxy'] = proxy
        self.assertEqual(requested, ['/Demo.Pack.v2.0.zip'])
        self.assertFalse((self.rp_dir / 'Demo.Pack.v1.0.zip').exists())
        self.assertTrue((self.rp_dir / 'Unrelated.zip').exists())
        self.assertEqual((self.rp_dir / 'Demo.Pack.v2.0.zip').read_bytes(), data)
        self.assertTrue(any(b['path'].read_bytes() == old for b in packs.list_backups(self.app.cfg, 'resourcepack') if b['path'].is_file()))

    def test_custom_entries_have_their_own_page_and_survive_wiki_refresh(self):
        pack_id = self.app.packs.add_custom({'kind': 'resourcepack', 'name_en': 'Local Demo',
                                            'source_url': 'https://github.com/custom/pack'})
        self.app.refresh_packs()
        self.assertTrue(self.app.pack_custom_tree.exists(pack_id))
        self.assertFalse(self.app.pack_tree.exists(pack_id))
        fresh = [dict(e) for e in self.app.packs.by_kind() if not e.get('custom')]
        self.app.packs.merge_wiki(fresh)
        self.app.refresh_packs()
        self.assertEqual(self.app.packs.get(pack_id)['bound_source'], 'https://github.com/custom/pack')
        self.assertTrue(self.app.pack_custom_tree.exists(pack_id))

    def test_wiki_auto_sync_resumes_if_page_loaded_during_another_task(self):
        view = mock.Mock(ready=True)
        previous = self.app._pack_view
        self.app._pack_view = view
        self.app._pack_auto_sync = False
        try:
            with mock.patch.object(self.app.pack_wiki_frame, 'winfo_ismapped', return_value=True), \
                 mock.patch.object(self.app, 'refresh_packs_wiki') as sync:
                self.app.busy = True
                self.app._sync_pack_wiki_view()
                sync.assert_not_called()
                self.app.busy = False
                self.app._sync_pack_wiki_view()
                self.assertTrue(_pump(self.app.root, lambda: sync.called))
                sync.assert_called_once()
        finally:
            self.app.busy = False
            self.app._pack_view = previous
            self.app._pack_auto_sync = False
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
        for expected in ("详情", "安装", "选择下载资产...", "打开下载页面", "检查更新"):
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


class TestCatalogInstallationMarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        from gtnhmod.config import Config
        from tests.test_catalog_identity import duplicate_pair, entry
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_gui_identity_"))
        cfg = Config(cls.tmp / "data")
        rows, cls.expected = [], {}
        for side in ("client", "server"):
            folder = cls.tmp / side
            folder.mkdir()
            cfg.data["mods_folders"][side] = str(folder)
        for name, alias, prefix in (
                ("GT-Simple-Wireless-Network", "gtswn", "GTSWN"),
                ("GT-Interesting-Thing", "gtit", "GTIT"),
                ("GT-Steam-Reborn", "gtsr", "GTSR"),
                ("Twist Space Technology Mod", "TwistSpaceTechnology", "TST"),
                ("Programmable Hatch Mod", "programmablehatches", "PH")):
            old, current = duplicate_pair(name, alias, prefix)
            current["aliases"] = [alias]
            rows.extend([current, old])
            cls.expected[old["id"]] = f"{alias}-1.0.jar"
            for side in ("client", "server"):
                (cfg.mods_dir(side) / cls.expected[old["id"]]).write_bytes(b"fixture")
        # 单端安装和未安装条目，验证端别与“仅未安装”过滤。
        single = entry("ClientOnly", aliases=["ClientOnly"])
        rows.extend([single, entry("NotInstalled")])
        (cfg.mods_dir("client") / "ClientOnly-1.0.jar.disabled").write_bytes(b"fixture")
        cfg.save()
        (cls.tmp / "data" / "mods_db.json").write_text(
            json.dumps({"mods": rows}), encoding="utf-8")
        cls.app = _new_app(cls.tmp / "data")
        cls.app.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        _clear_app(cls.app)
        cls.app = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        gc.collect()

    def setUp(self):
        self.app.only_uninstalled.set(False)
        self.app.add_search.set("")
        self.app.refresh_installed()
        self.app.refresh_addable()
        self.app.refresh_unmanaged()

    def test_duplicate_wiki_entries_show_installed_on_both_pages(self):
        for mid in self.expected:
            with self.subTest(mid=mid):
                self.assertTrue(self.app.inst_tree.exists(mid))
                self.assertEqual(self.app.add_tree.set(mid, "installed"), "双端")
                self.assertTrue(self.app.add_tree.set(mid, "name").startswith("✓ "))
                self.assertNotIn("[[", self.app.add_tree.set(mid, "name"))
                self.assertIn("inst", self.app.add_tree.item(mid, "tags"))

    def test_all_mod_tables_use_small_icons_without_changing_data_columns(self):
        for tree in (self.app.inst_tree, self.app.add_tree, self.app.um_tree, self.app.cust_tree):
            self.assertIn('tree', tuple(str(x) for x in tree['show']))
            self.assertEqual(tree.column('#0', 'width'), 28)
            for iid in tree.get_children(''):
                self.assertTrue(tree.item(iid, 'image'))
        self.assertEqual(self.app.inst_tree['columns'],
                         ('name', 'side', 'version', 'inst_time', 'latest', 'status'))

    def test_async_icon_completion_keeps_selection_values_tags_and_busy_state(self):
        from tests.test_mod_icons import picture
        tree = self.app.inst_tree
        iid = next(iter(self.expected))
        tree.selection_set(iid)
        before = tree.item(iid)
        self.app.busy = True
        try:
            self.app._on_mod_icon(self.app._icon_targets[tree, iid], picture())
            self.assertTrue(self.app.busy)
            self.assertEqual(tree.selection(), (iid,))
            self.assertEqual(tree.item(iid, 'values'), tuple(before['values']))
            self.assertEqual(tuple(tree.item(iid, 'tags')), tuple(before['tags']))
        finally:
            self.app.busy = False

    def test_freshly_installed_aliases_do_not_appear_as_unmanaged(self):
        unmanaged = {f.file_name for files in gui.updater.unmatched_files(
            self.app.cfg, self.app.db).values() for f in files}
        self.assertFalse(set(self.expected.values()).intersection(unmanaged))
        self.assertEqual(self.app.um_tree.get_children(), ())

    def test_uninstalled_filter_and_disabled_single_side_mark(self):
        self.assertEqual(self.app.add_tree.set("clientonly", "installed"), "仅客户端")
        self.app.only_uninstalled.set(True)
        self.app.refresh_addable()
        self.assertEqual(self.app.add_tree.get_children(), ("notinstalled",))

    def test_wiki_refresh_keeps_marks_and_unique_ids(self):
        from tests.test_catalog_identity import entry
        fresh = [entry(e["name_en"], aliases=[]) for e in self.app.db.wiki_mods()]
        self.app.db.merge_wiki(fresh)
        self.app.refresh_all()
        for mid in self.expected:
            self.assertEqual(self.app.add_tree.set(mid, "installed"), "双端")
        self.assertEqual(len(self.app.db.all()), 7)

    def test_f5_rescans_installed_marks_after_external_file_change(self):
        path = self.app.cfg.mods_dir("client") / "NotInstalled-1.0.jar"
        self.app.nb.select(1)
        self.app.root.update()
        self.assertEqual(self.app.add_tree.set("notinstalled", "installed"), "—")
        try:
            path.write_bytes(b"fixture")
            self.app._refresh_current_tab()
            self.assertEqual(self.app.add_tree.set("notinstalled", "installed"), "仅客户端")
        finally:
            path.unlink(missing_ok=True)
            self.app._merged_cache = None


class TestBackupOverview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gtnh_backup_gui_"))
        cls.app = _new_app(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        _clear_app(cls.app)
        cls.app = None
        shutil.rmtree(cls.tmp, ignore_errors=True)
        gc.collect()

    def setUp(self):
        self.folder = self.app.cfg.backup_dir / "client" / self._testMethodName
        self.folder.mkdir(parents=True)
        self.files = [self.folder / f"20261015_02000{i}_Demo-{i}.jar" for i in range(3)]
        for path in self.files:
            path.write_bytes(b"backup")
        self.app._backup_overview_dialog()
        self.top = next(w for w in self.app.root.winfo_children()
                        if isinstance(w, tk.Toplevel) and w.title() == "备份管理（全部）")
        self.tree = next(w for w in _walk(self.top) if isinstance(w, ttk.Treeview))

    def tearDown(self):
        self.top.destroy()
        for path in self.files:
            path.unlink(missing_ok=True)
        self.folder.rmdir()

    def button(self, text):
        return next(w for w in _walk(self.top) if isinstance(w, ttk.Button) and w.cget("text") == text)

    def test_no_selection_explains_how_to_locate(self):
        with mock.patch.object(gui.messagebox, "showinfo") as info, \
                mock.patch.object(self.app, "_reveal") as reveal:
            self.button("定位选中备份").invoke()
        info.assert_called_once()
        reveal.assert_not_called()

    def test_selected_backup_is_revealed_and_double_click_is_bound(self):
        iid = self.tree.get_children()[0]
        self.tree.selection_set(iid)
        with mock.patch.object(self.app, "_reveal") as reveal:
            self.button("定位选中备份").invoke()
        self.assertEqual(reveal.call_args.args[0], Path(iid.split("|", 2)[2]))
        self.assertTrue(self.tree.bind("<Double-Button-1>"))
        self.assertTrue(self.tree.bind("<Return>"))

    def test_open_backup_directory_does_not_require_a_selection(self):
        with mock.patch.object(self.app, "_reveal") as reveal:
            self.button("打开备份目录").invoke()
        reveal.assert_called_once_with(self.app.cfg.backup_dir)

    def test_delete_only_removes_selected_file_without_pruning_others(self):
        self.app.cfg.data["backup_keep"] = 1
        iid = self.tree.get_children()[0]
        selected = Path(iid.split("|", 2)[2])
        self.tree.selection_set(iid)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            self.button("删除选中").invoke()
        self.assertFalse(selected.exists())
        self.assertTrue(all(p.exists() for p in self.files if p != selected))
        self.assertEqual(len(self.tree.get_children()), 2)
        self.button("刷新").invoke()
        self.assertEqual(len(self.tree.get_children()), 2)

    def test_missing_file_has_visible_feedback_and_does_not_launch_explorer(self):
        path = self.files[0]
        path.unlink()
        with mock.patch.object(gui.messagebox, "showinfo") as info, \
                mock.patch.object(gui.subprocess, "Popen") as launch:
            self.app._reveal(path)
        info.assert_called_once()
        launch.assert_not_called()

    def test_reveal_handles_file_paths_with_spaces(self):
        folder = self.folder / "path with spaces"
        folder.mkdir()
        path = folder / "backup file.jar"
        path.write_bytes(b"backup")
        try:
            with mock.patch.object(gui.subprocess, "Popen") as launch:
                self.app._reveal(path)
            self.assertEqual(launch.call_args.args[0], ["explorer.exe", f"/select,{path.resolve()}"])
        finally:
            path.unlink()
            folder.rmdir()


if __name__ == "__main__":
    unittest.main()
