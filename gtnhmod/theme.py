"""浅色 / 暗色 / 跟随系统 外观切换。

浅色沿用 Windows 原生 vista 主题；vista 的按钮、输入框由系统绘制无法改色，
暗色因此改用可完全配色的 clam 主题。经典 Tk 控件（Text、Listbox、Canvas、
对话框底色）不受 ttk 主题控制，通过选项数据库影响之后新建的控件，
切换时再遍历已存在的控件补色。所有颜色都从调色板取，界面代码只引用语义名。
"""
import ctypes
import os
import tkinter as tk
from tkinter import ttk

MODES = ("system", "light", "dark")
MODE_LABELS = {"system": "跟随系统", "light": "浅色", "dark": "暗色"}

PALETTES = {
    "light": {
        "bg": "#f0f0f0", "surface": "#ffffff", "fg": "#000000", "muted": "#666666",
        "border": "#adadad", "select_bg": "#0078d7", "select_fg": "#ffffff",
        "odd": "#f4f5f7", "heading": "#ffffff", "button": "#e1e1e1", "button_active": "#e5f1fb",
        "ok": "#1a7f37", "ok_bg": "#c9ecd8", "ok_fg": "#0a6b2d",
        "warn": "#b36b00", "warn_bg": "#fdf3d1", "new_bg": "#fff3bf",
        "error": "#b13d3d", "disabled": "#888888",
        "locked_bg": "#e4e8fb", "locked_fg": "#3d4bb8", "link": "#0a58ca",
        "installed": "#237241",
    },
    "dark": {
        "bg": "#202124", "surface": "#2b2d31", "fg": "#e6e6e6", "muted": "#a0a4aa",
        "border": "#4a4d52", "select_bg": "#2f5f8f", "select_fg": "#ffffff",
        "odd": "#26282c", "heading": "#33363b", "button": "#3a3d42", "button_active": "#474b51",
        "ok": "#6fcf8a", "ok_bg": "#1f3d2a", "ok_fg": "#8fe0a6",
        "warn": "#f0b45a", "warn_bg": "#3d3420", "new_bg": "#3f3a1c",
        "error": "#ff8a80", "disabled": "#8a8e94",
        "locked_bg": "#2a2f4d", "locked_fg": "#aab4ff", "link": "#7ab4ff",
        "installed": "#6fcf8a",
    },
}


def system_prefers_dark() -> bool:
    """Windows「默认应用模式」为深色时返回 True；读取失败按浅色处理。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve(mode: str) -> str:
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return "dark" if system_prefers_dark() else "light"


def set_title_bar(window, dark: bool) -> None:
    """让 Windows 标题栏跟随暗色（Win10 1809+ / Win11），不支持时静默忽略。"""
    if os.name != "nt":
        return
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新旧编号）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (AttributeError, OSError, tk.TclError):
        pass


class ThemeManager:
    """持有当前调色板；树表标签、经典控件与新建窗口都从这里取色。"""

    def __init__(self, root, font, mode="system"):
        self.root, self.font = root, font
        self.mode = mode if mode in MODES else "system"
        self.name = resolve(self.mode)
        self._trees = []          # (tree, {tag: {option: palette_key}})
        self._listeners = []
        self._poll_job = None
        # 窗口映射后才有可用的 HWND，在此时设置标题栏颜色
        for cls in ("Toplevel", "Tk"):
            root.bind_class(cls, "<Map>", self._on_toplevel_map)  # 一个解释器只有一个管理器

    @property
    def palette(self):
        return PALETTES[self.name]

    def color(self, key):
        return self.palette[key]

    @property
    def dark(self):
        return self.name == "dark"

    def on_change(self, callback):
        self._listeners.append(callback)

    def register_tree(self, tree, spec):
        """spec: {tag: {"foreground"/"background": 调色板键}}；切换主题时自动重设。"""
        self._trees.append((tree, spec))
        self._apply_tree(tree, spec)

    def set_mode(self, mode):
        self.mode = mode if mode in MODES else "system"
        self.apply()

    def apply(self, force=False):
        name = resolve(self.mode)
        changed = name != self.name
        self.name = name
        self._configure_styles()
        self._configure_option_db()
        self._recolor(self.root)
        self._trees = [(t, s) for t, s in self._trees if _alive(t)]
        for tree, spec in self._trees:
            self._apply_tree(tree, spec)
        set_title_bar(self.root, self.dark)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                set_title_bar(child, self.dark)
        if changed or force:
            for callback in list(self._listeners):
                callback(self.name)
        self._schedule_poll()

    # ---------- 内部 ----------
    def _schedule_poll(self):
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
            self._poll_job = None
        if self.mode == "system":
            self._poll_job = self.root.after(3000, self._poll_system)

    def _poll_system(self):
        self._poll_job = None
        if self.mode == "system" and resolve("system") != self.name:
            self.apply()
        else:
            self._schedule_poll()

    def _on_toplevel_map(self, event):
        if isinstance(event.widget, (tk.Toplevel, tk.Tk)):
            set_title_bar(event.widget, self.dark)

    def _apply_tree(self, tree, spec):
        if not _alive(tree):
            return
        p = self.palette
        for tag, options in spec.items():
            tree.tag_configure(tag, **{opt: p[key] for opt, key in options.items()})

    def _configure_option_db(self):
        p = self.palette
        for pattern, value in (
                ("*Background", p["bg"]), ("*Foreground", p["fg"]),
                ("*Text.Background", p["surface"]), ("*Listbox.Background", p["surface"]),
                ("*Text.insertBackground", p["fg"]), ("*highlightBackground", p["border"]),
                ("*selectBackground", p["select_bg"]), ("*selectForeground", p["select_fg"]),
                ("*TCombobox*Listbox.background", p["surface"]),
                ("*TCombobox*Listbox.foreground", p["fg"]),
                ("*TCombobox*Listbox.selectBackground", p["select_bg"]),
                ("*TCombobox*Listbox.selectForeground", p["select_fg"])):
            self.root.option_add(pattern, value, "interactive")

    def _recolor(self, widget):
        p = self.palette
        cls = widget.winfo_class()
        options = {
            "Tk": {"background": p["bg"]},
            "Toplevel": {"background": p["bg"]},
            "Canvas": {"background": p["bg"]},
            "Panedwindow": {"background": p["bg"]},
            "Text": {"background": p["surface"], "foreground": p["fg"],
                     "insertbackground": p["fg"], "selectbackground": p["select_bg"],
                     "highlightbackground": p["border"], "highlightcolor": p["select_bg"],
                     "selectforeground": p["select_fg"]},
            "Listbox": {"background": p["surface"], "foreground": p["fg"],
                        "highlightbackground": p["border"], "highlightcolor": p["select_bg"],
                        "selectbackground": p["select_bg"], "selectforeground": p["select_fg"]},
            "Radiobutton": {"background": p["bg"], "foreground": p["fg"],
                            "activebackground": p["bg"], "activeforeground": p["fg"],
                            "selectcolor": p["surface"]},
            "Menu": {"background": p["surface"], "foreground": p["fg"],
                     "activebackground": p["select_bg"], "activeforeground": p["select_fg"]},
        }.get(cls)
        if options:
            try:
                widget.configure(**options)
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._recolor(child)

    def _configure_styles(self):
        style = ttk.Style(self.root)
        p = self.palette
        try:
            style.theme_use("clam" if self.dark else "vista")
        except tk.TclError:
            style.theme_use("clam")
        # 布局和字体按主题分别保存，切换后需重设
        style.configure("Treeview", rowheight=24, font=self.font)
        style.configure("Treeview.Heading", font=self.font)
        style.configure("ModIcon.Treeview", rowheight=24, indent=0)
        style.layout("ModIcon.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])
        style.configure("Muted.TLabel", foreground=p["muted"])
        style.configure("Warn.TLabel", foreground=p["warn"])
        if not self.dark:
            return
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["surface"],
                        bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                        troughcolor=p["surface"], selectbackground=p["select_bg"],
                        selectforeground=p["select_fg"], insertcolor=p["fg"],
                        focuscolor=p["select_bg"], arrowcolor=p["fg"])
        style.map(".", foreground=[("disabled", p["disabled"])],
                  fieldbackground=[("disabled", p["bg"])])
        style.configure("TButton", background=p["button"], bordercolor=p["border"],
                        lightcolor=p["button"], darkcolor=p["button"], padding=(8, 2))
        style.map("TButton", background=[("disabled", p["bg"]), ("pressed", p["select_bg"]),
                                         ("active", p["button_active"])])
        style.configure("TEntry", fieldbackground=p["surface"], foreground=p["fg"])
        style.configure("TCombobox", fieldbackground=p["surface"], foreground=p["fg"],
                        background=p["button"])
        style.map("TCombobox", fieldbackground=[("readonly", p["surface"]), ("disabled", p["bg"])],
                  foreground=[("readonly", p["fg"]), ("disabled", p["disabled"])],
                  selectbackground=[("readonly", p["surface"])],
                  selectforeground=[("readonly", p["fg"])])
        for name in ("TCheckbutton", "TRadiobutton"):
            style.configure(name, background=p["bg"], foreground=p["fg"],
                            indicatorbackground=p["surface"], indicatorforeground=p["fg"])
            style.map(name, background=[("active", p["bg"])],
                      indicatorbackground=[("pressed", p["button_active"]), ("selected", p["surface"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["button"], foreground=p["fg"],
                        lightcolor=p["button"], bordercolor=p["border"], padding=(10, 3))
        style.map("TNotebook.Tab", background=[("selected", p["bg"]), ("active", p["button_active"])],
                  lightcolor=[("selected", p["bg"])])
        style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                        foreground=p["fg"], bordercolor=p["border"])
        style.map("Treeview", background=[("selected", p["select_bg"])],
                  foreground=[("selected", p["select_fg"])])
        style.configure("Treeview.Heading", background=p["heading"], foreground=p["fg"],
                        lightcolor=p["heading"], bordercolor=p["border"])
        style.map("Treeview.Heading", background=[("active", p["button_active"])])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        style.configure("TScrollbar", background=p["button"], troughcolor=p["bg"],
                        bordercolor=p["bg"], lightcolor=p["button"], darkcolor=p["button"],
                        arrowcolor=p["muted"], gripcount=0)
        style.map("TScrollbar", background=[("active", p["button_active"])])
        style.configure("TProgressbar", background=p["select_bg"], troughcolor=p["surface"])
        style.configure("TMenubutton", background=p["button"], foreground=p["fg"])


def _alive(widget):
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
