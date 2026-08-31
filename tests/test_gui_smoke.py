"""GUI 冒烟测试（Windows 有桌面会话时运行；CI 的 windows-latest 满足）。

覆盖"对话框打开即抛异常"一类回归——如 v1.5.0 版本选择器的前向引用
NameError（确认按钮根本没绑定，只能靠人工发现）。
"""
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


def _walk(widget):
    """递归枚举对话框控件，避免测试依赖具体的 Frame 嵌套层级。"""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


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
        cls.app = gui.GuiApp(cls.tmp)
        cls.app.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        cls.app.root.destroy()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_app_builds(self):
        self.assertTrue(self.app.inst_tree["columns"])

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


if __name__ == "__main__":
    unittest.main()
