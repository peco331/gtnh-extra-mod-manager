"""Tkinter GUI 壳。

线程模型：所有网络/文件操作在后台线程执行，经 queue.Queue 把日志与
"刷新"事件推回主线程（root.after 轮询），避免 UI 冻结。Tk 控件只在主线程操作。

已安装页：一个 mod 一行，安装端别分三类（双端/仅客户端/仅服务端）。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import SIDES, SIDE_LABELS, __version__, net, updater, utils, cookies
from . import wiki as wikimod
from .config import Config
from .db import ModsDB
from .installed import InstalledDB

STATUS_CN = {"installed": "已安装", "update_avail": "可更新",
             "update_incompat": "有新版（可能不兼容）",
             "disabled": "已禁用"}
STATUS_COLORS = {"update_avail": "#1a7f37", "disabled": "#999999", "installed": "#000000"}
INSTALL_SIDE_CN = {"both": "双端", "client": "仅客户端", "server": "仅服务端"}
FONT = ("Microsoft YaHei UI", 9)


def side_state_text(st) -> str:
    """某端别的安装状态简述：v1.0.0 ✓ / v1.0.0 ✗禁用 / v1.0.0 ↑可更新 / —。"""
    if st is None:
        return "—"
    if not st["enabled"]:
        return f"v{st['version']} ✗禁用"
    if st["status"] == "update_avail":
        return f"v{st['version']} ↑可更新"
    return f"v{st['version']} ✓"


class GuiApp:
    def __init__(self, data_dir: Path):
        self.cfg = Config(data_dir)
        self.db = ModsDB(data_dir / "mods_db.json")
        self.installed = InstalledDB(data_dir / "installed.json")
        # 启动维护：校正已安装记录、清理积压备份、清扫孤儿临时文件
        updater.startup_maintenance(self.cfg, self.db, self.installed)
        self.queue: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(f"GTNH 额外MOD管理工具 v{__version__}")
        self.root.geometry(self._clamp_geometry(
            self.cfg.data.get("window_geometry") or "1120x720"))
        self.root.minsize(980, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.report_callback_exception = self._report_cb_exc
        self._merged_cache = None      # build_merged_registry 结果缓存（一次刷新内复用）
        self._new_ids: set = set()     # 本次刷新wiki新增的mod（查看详情后清除高亮）
        self._build_ui()
        self.root.after(100, self._poll_queue)
        # 启动时刷新全部页签（否则自定义源/未受管页首次是空的）
        self.refresh_installed()
        self.refresh_addable()
        self.refresh_unmanaged()
        self.refresh_custom()
        # 切换页签时自动刷新对应页
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        # 全局快捷键：Ctrl+F 聚焦搜索、F5 刷新当前页
        self.root.bind_all("<Control-f>", lambda e: self._focus_search())
        self.root.bind_all("<Control-F>", lambda e: self._focus_search())
        self.root.bind_all("<F5>", lambda e: self._refresh_current_tab())
        # 树全选（Ctrl+A）
        for t in (self.inst_tree, self.add_tree, self.um_tree, self.cust_tree):
            t.bind("<Control-a>", lambda _e, tree=t: tree.selection_set(tree.get_children("")))
        # 键盘导航：回车=详情，Delete=删除（有确认框兜底）
        self.inst_tree.bind("<Return>", lambda _e: self._open_inst_detail_selection())
        self.inst_tree.bind("<KP_Enter>", lambda _e: self._open_inst_detail_selection())
        self.add_tree.bind("<Return>", lambda _e: self.show_addable_detail())
        self.add_tree.bind("<KP_Enter>", lambda _e: self.show_addable_detail())
        self.inst_tree.bind("<Delete>", lambda _e: self.delete_selected())

    def _clamp_geometry(self, geo):
        """恢复记忆的窗口位置时 clamp 到当前屏幕内（换小屏后窗口跑到屏外）。"""
        import re
        m = re.match(r"(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?", geo or "")
        if not m:
            return geo
        w, h = int(m.group(1)), int(m.group(2))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, sw), min(h, sh)
        x = y = None
        if m.group(3) is not None:
            x = min(max(int(m.group(3)), 0), max(0, sw - 200))
            y = min(max(int(m.group(4)), 0), max(0, sh - 200))
        return f"{w}x{h}" + (f"{x:+d}{y:+d}" if x is not None else "")

    def _on_close(self):
        """退出前取消挂起的防抖定时器（避免对已销毁控件的回调）并记忆窗口状态。"""
        for attr in ("_inst_search_after", "_add_search_after", "_um_search_after"):
            if getattr(self, attr, None):
                self.root.after_cancel(getattr(self, attr))
        try:
            for t in (self.inst_tree, self.add_tree, self.um_tree, self.cust_tree):
                self._persist_view(t)
            self.cfg.data["window_geometry"] = self.root.geometry()
            self.cfg.save()
        except OSError:
            pass
        self.root.destroy()

    def _report_cb_exc(self, exc, val, tb):
        """Tk 回调异常落盘（pyw 无控制台，否则无声丢失）+ 写日志区。"""
        import traceback
        try:
            err_file = self.cfg.data_dir / "logs" / "gui_error.log"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            with open(err_file, "a", encoding="utf-8") as f:
                f.write(f"[{utils.now_str()}] Tk回调异常\n{traceback.format_exc()}\n")
        except OSError:
            pass
        self._log(f"[错误] 界面回调异常: {val}")

    def _focus_search(self):
        """Ctrl+F：聚焦当前页的搜索框。"""
        for attr in ("inst_search_entry", "add_search_entry", "um_search_entry"):
            e = getattr(self, attr, None)
            if e is not None and e.winfo_ismapped():
                e.focus_set()
                return
        self.nb.select(0)
        self.inst_search_entry.focus_set()

    def _refresh_current_tab(self):
        if self.busy:
            return  # 后台任务进行中，避免并发扫描/重绘
        (self.refresh_installed, self.refresh_addable,
         self.refresh_unmanaged, self.refresh_custom, lambda: None)[self.nb.index("current")]()

    # ---------- UI 构建 ----------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24, font=FONT)
        style.configure("Treeview.Heading", font=FONT)

        # 上下可拖拽分栏：主界面 + 底部日志区（经典 PanedWindow，分割条明显可抓）
        main_pane = tk.PanedWindow(self.root, orient="vertical",
                                   sashwidth=8, showhandle=True)
        main_pane.pack(fill="both", expand=True)
        self.nb = ttk.Notebook(main_pane)
        main_pane.add(self.nb, stretch="always", minsize=300)

        tab1 = ttk.Frame(self.nb)
        self.nb.add(tab1, text="已安装MOD")
        self._build_installed_tab(tab1)
        tab2 = ttk.Frame(self.nb)
        self.nb.add(tab2, text="可添加MOD")
        self._build_addable_tab(tab2)
        tab3 = ttk.Frame(self.nb)
        self.nb.add(tab3, text="未受管MOD")
        self._build_unmanaged_tab(tab3)
        tab4 = ttk.Frame(self.nb)
        self.nb.add(tab4, text="自定义源")
        self._build_custom_tab(tab4)
        tab5 = ttk.Frame(self.nb)
        self.nb.add(tab5, text="设置")
        self._build_settings_tab(tab5)

        bottom = ttk.Frame(main_pane)
        main_pane.add(bottom, stretch="never", minsize=56)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", side="top", padx=6, pady=(4, 0))
        log_bar = ttk.Frame(bottom)
        log_bar.pack(fill="x", padx=6)
        ttk.Label(log_bar, text="操作日志:").pack(side="left")
        ttk.Button(log_bar, text="清空", command=self.clear_log).pack(side="left", padx=4)
        ttk.Button(log_bar, text="打开日志文件", command=self._open_log_file).pack(side="left", padx=2)
        # 日志区可折叠：浏览列表时收起省空间，状态存入配置
        self.log_expanded = bool(self.cfg.data.get("log_expanded", True))
        self.log_toggle_btn = ttk.Button(log_bar, text="收起日志 ▴",
                                         command=self._toggle_log)
        self.log_toggle_btn.pack(side="right")
        log_frame = ttk.Frame(bottom)
        self.log = tk.Text(log_frame, height=8, font=("Consolas", 9),
                           state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.log, log_frame)
        self.log_frame = log_frame
        self.status_var = tk.StringVar(value="就绪")
        self.status_label = ttk.Label(bottom, textvariable=self.status_var, font=FONT,
                                      anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(2, 4))  # 先占位
        self._apply_log_layout()  # log_frame 用 before=status_label 插到状态栏之前
        if not self.cfg.client_mods_dir and not self.cfg.server_mods_dir:
            self._log("就绪。首次使用请先到「设置」配置两端 mods 目录；"
                      "mod 数据请点「可添加MOD」页的刷新按钮获取。")
        else:
            self._log("就绪。")

    def _toggle_log(self):
        self.log_expanded = not self.log_expanded
        self.cfg.data["log_expanded"] = self.log_expanded
        self.cfg.save()
        self._apply_log_layout()

    def _apply_log_layout(self):
        # 展开时固定在状态栏之前（pack_forget 后重新 pack 会追加到队尾，
        # 不指定位置状态栏会被顶到日志上方）
        if self.log_expanded:
            self.log_frame.pack(fill="both", expand=True, padx=6,
                                before=self.status_label)
            self.log_toggle_btn.configure(text="收起日志 ▴")
        else:
            self.log_frame.pack_forget()
            self.log_toggle_btn.configure(text="展开日志 ▾")

    def _set_status(self, msg):
        """写状态栏；后台任务进行时让位给进度文案。"""
        if not self.busy:
            self.status_var.set(msg)

    def _attach_scrollbar(self, widget, parent):
        sb = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
        sb.pack(side="right", fill="y")
        widget.configure(yscrollcommand=sb.set)
        return sb

    # ---- 视图持久化（排序/列宽，存 config.tree_views） ----
    def _view_cfg(self):
        return self.cfg.data.setdefault("tree_views", {})

    def _persist_view(self, tree):
        key = getattr(tree, "_view_key", None)
        if not key:
            return
        try:
            widths = {c: tree.column(c, "width") for c in tree["columns"]}
        except tk.TclError:
            return
        self._view_cfg()[key] = {
            "sort_col": getattr(tree, "_sort_col", "") or "",
            "sort_rev": bool(getattr(tree, "_sort_rev", False)),
            "widths": widths,
        }

    def _restore_view(self, tree):
        key = getattr(tree, "_view_key", None)
        if not key:
            return
        vs = self._view_cfg().get(key) or {}
        if vs.get("sort_col"):
            tree._sort_col = vs["sort_col"]
            tree._sort_rev = bool(vs.get("sort_rev"))
        for c, w in (vs.get("widths") or {}).items():
            if c in tree["columns"]:
                try:
                    tree.column(c, width=int(w))
                except (tk.TclError, ValueError):
                    pass

    # ---- 表头点击排序（通用，所有列表） ----
    def _make_sortable(self, tree, desc_first_cols=()):
        # 排序选择持久化
        key = getattr(tree, "_view_key", None)
        if key:
            self._view_cfg().setdefault(key, {})
        """表头点击按该列排序；再次点击反向。desc_first_cols 里的列首次点击为降序。"""

        def handler(col):
            if getattr(tree, "_sort_col", None) == col:
                reverse = not getattr(tree, "_sort_rev", False)
            else:
                reverse = col in desc_first_cols
            tree._sort_col, tree._sort_rev = col, reverse
            self._sort_tree(tree, col, reverse)
            self._persist_view(tree)

        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col: handler(c))

    def _sort_tree(self, tree, col, reverse):
        """按列排序。无值（—/空）的行无论升降序都排在最后。"""
        def key(iid):
            v = tree.set(iid, col)
            try:
                return (0, float(v))
            except ValueError:
                if v in ("", "—", "?"):
                    return (2, "")
                return (1, v.lower())
        iids = list(tree.get_children(""))
        has = [i for i in iids if key(i)[0] != 2]
        empty = [i for i in iids if key(i)[0] == 2]
        has.sort(key=key, reverse=reverse)
        for i, iid in enumerate(has + empty):
            tree.move(iid, "", i)

    def _reapply_sort(self, tree):
        if hasattr(tree, "_sort_col"):
            self._sort_tree(tree, tree._sort_col, tree._sort_rev)

    def _popup(self, tree, build, event):
        """右键菜单：自动选中目标行后弹出；busy 期间不弹（防并发操作）。"""
        if self.busy:
            return
        iid = tree.identify_row(event.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        build(menu, iid)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _dialog(self, title, size="600x400"):
        """统一对话框：Esc 关闭、跟随主窗、模态。"""
        top = tk.Toplevel(self.root)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.title(title)
        top.geometry(size)
        top.transient(self.root)
        try:
            # 不用 wait_visibility：主窗最小化时它会无限等（表现为"卡住"）
            top.deiconify()
            top.lift()
            top.focus_force()
            top.grab_set()
        except tk.TclError:
            pass
        return top

    def _reveal(self, path):
        """在资源管理器中定位文件。"""
        try:
            subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError as e:
            messagebox.showerror("错误", f"打开资源管理器失败: {e}")

    def reveal_mod(self, m, side):
        """在资源管理器中定位某 mod 在指定端别的文件。"""
        f = updater.find_installed_file(self.cfg, self.db, side, m["mod_id"])
        if f:
            self._reveal(f.path)
        else:
            messagebox.showinfo("提示", f"{SIDE_LABELS[side]}未找到该mod的文件")

    def _build_installed_tab(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=4)
        self.side_filter = tk.StringVar(value="all")
        for val, label in (("all", "全部"), ("client", "仅客户端"),
                           ("server", "仅服务端"), ("both", "双端")):
            ttk.Radiobutton(bar, text=label, value=val, variable=self.side_filter,
                            command=self.refresh_installed).pack(side="left")
        ttk.Label(bar, text="搜索:").pack(side="left", padx=(12, 2))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._debounce_inst_search())
        self.inst_search_entry = ttk.Entry(bar, textvariable=self.search_var, width=18)
        self.inst_search_entry.pack(side="left")
        self.only_update = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="仅可更新", variable=self.only_update,
                        command=self.refresh_installed).pack(side="left", padx=(6, 0))
        # 全局操作按钮放顶部（与过滤器同一行，列表下方不再重复）
        self.btn_dirs = ttk.Menubutton(bar, text="打开mods目录 ▾")
        dir_menu = tk.Menu(self.btn_dirs, tearoff=0)
        has_dir = False
        for s in SIDES:
            d = self.cfg.mods_dir(s)
            if d and d.is_dir():
                dir_menu.add_command(label=SIDE_LABELS[s],
                                     command=lambda d=d: subprocess.Popen(["explorer", str(d)]))
                has_dir = True
        if not has_dir:
            dir_menu.add_command(label="（未设置目录）", state="disabled")
        dir_menu.add_separator()
        dir_menu.add_command(label="备份管理（全部）…", command=self._backup_overview_dialog)
        self.btn_dirs["menu"] = dir_menu
        self.btn_dirs.pack(side="right", padx=(4, 0))
        self.btn_update_all = ttk.Button(bar, text="全部更新", command=self.update_all)
        self.btn_update_all.pack(side="right", padx=4)
        self.btn_update = ttk.Button(bar, text="更新选中", command=self.update_selected)
        self.btn_update.pack(side="right", padx=4)
        self.btn_check = ttk.Button(bar, text="检查更新", command=self.check_updates)
        self.btn_check.pack(side="right", padx=4)
        ttk.Button(bar, text="刷新", command=self.refresh_installed).pack(side="right", padx=4)

        # 新手引导（两端目录都未配置时显示，完成后自动隐藏）
        self.guide = ttk.LabelFrame(tab, text="三步上手")
        gf = ttk.Frame(self.guide)
        gf.pack(fill="x", padx=8, pady=4)
        ttk.Label(gf, text="① 到「设置」页配置客户端/服务端 mods 目录   →   "
                           "② 回到「可添加MOD」页点「刷新Wiki数据」获取可安装列表   →   "
                           "③ 勾选想装的 mod 批量安装",
                  font=FONT).pack(side="left")
        ttk.Button(gf, text="去设置页", command=lambda: self.nb.select(4)).pack(side="right")
        self.guide.pack(fill="x", padx=6, pady=(0, 4))
        self.guide.pack_forget()  # 默认隐藏，两端目录均未配置时显示

        cols = ("name", "side", "version", "inst_time", "latest", "status")
        heads = ("名称", "安装端别", "已装版本（✗=该端已禁用）", "本地更新时间", "最新版本", "状态")
        widths = (300, 80, 190, 105, 120, 90)
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6)
        self.inst_tree_frame = tree_frame  # guide 卡片 pack(before=) 用
        self.inst_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                      selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.inst_tree.heading(c, text=h)
            self.inst_tree.column(c, width=w, anchor="w")
        self.inst_tree.tag_configure("upd", background="#c9ecd8",
                                     foreground="#0a6b2d")
        self.inst_tree.tag_configure("incompat", background="#fdf3d1")
        self.inst_tree.tag_configure("locked", background="#e4e8fb",
                                     foreground="#3d4bb8")
        self.inst_tree.tag_configure("dis", foreground=STATUS_COLORS["disabled"])
        self.inst_tree.tag_configure("odd", background="#f4f5f7")
        self.inst_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.inst_tree, tree_frame)
        self.inst_tree._view_key = "installed"
        self._make_sortable(self.inst_tree, desc_first_cols=("latest", "inst_time"))
        self._restore_view(self.inst_tree)
        self.inst_tree.bind("<Double-1>", self._on_inst_double)
        self.inst_tree.bind("<Button-3>", lambda e: self._popup(self.inst_tree, self._inst_menu, e))
        self.busy_buttons = (self.btn_check, self.btn_update, self.btn_update_all,
                             self.btn_dirs)

    def _build_addable_tab(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=4)
        ttk.Label(bar, text="分类:").pack(side="left")
        self.cat_var = tk.StringVar()
        self.cat_combo = ttk.Combobox(bar, textvariable=self.cat_var, state="readonly", width=28)
        self.cat_combo.pack(side="left", padx=(0, 12))
        self.cat_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh_addable())
        ttk.Label(bar, text="搜索:").pack(side="left")
        self.add_search = tk.StringVar()
        self.add_search.trace_add("write", lambda *a: self._debounce_add_search())
        self.add_search_entry = ttk.Entry(bar, textvariable=self.add_search, width=22)
        self.add_search_entry.pack(side="left", padx=(2, 8))
        self.btn_refresh_wiki = ttk.Button(bar, text="刷新Wiki数据", command=self.refresh_wiki)
        self.btn_refresh_wiki.pack(side="right")

        cols = ("name", "cn", "side", "cat", "installed", "updated", "desc")
        heads = ("名称", "中文名", "端别", "分类", "已安装", "更新时间", "简介")
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6)
        self.add_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                     selectmode="extended")
        for c, h, w in zip(cols, heads, (250, 110, 70, 90, 80, 115, 265)):
            self.add_tree.heading(c, text=h)
            self.add_tree.column(c, width=w, anchor="w")
        self.add_tree.tag_configure("inst", foreground=STATUS_COLORS["update_avail"])
        self.add_tree.tag_configure("new", background="#fff3bf")
        self.add_tree.tag_configure("odd", background="#f4f5f7")
        self.add_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.add_tree, tree_frame)
        self.add_tree._view_key = "addable"
        self._make_sortable(self.add_tree, desc_first_cols=("updated",))
        # 默认按更新时间降序（有日期的在前，无日期的最后）；有保存的排序则恢复
        if not (self._view_cfg().get("addable") or {}).get("sort_col"):
            self.add_tree._sort_col, self.add_tree._sort_rev = "updated", True
        self._restore_view(self.add_tree)
        self.add_tree.bind("<Double-1>", lambda e: self.show_addable_detail())
        self.add_tree.bind("<Button-3>", lambda e: self._popup(self.add_tree, self._add_menu, e))
        self.only_uninstalled = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="仅未安装", variable=self.only_uninstalled,
                        command=self.refresh_addable).pack(side="right", padx=(4, 4))

        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="查看详情", command=self.show_addable_detail).pack(side="left", padx=2)
        ttk.Button(btns, text="安装（自动选择端别）", command=self.install_addable).pack(side="left", padx=2)
        ttk.Button(btns, text="打开下载页面", command=self.open_link_addable).pack(side="left", padx=2)

    def _build_unmanaged_tab(self, tab):
        ttk.Label(tab, text="以下 jar 未被识别（GTNH核心mod等不在可添加列表中的会出现在这里，请勿误删）:",
                  font=FONT).pack(anchor="w", padx=6, pady=(4, 0))
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=2)
        ttk.Label(bar, text="搜索:").pack(side="left")
        self.um_search = tk.StringVar()
        self.um_search.trace_add("write", lambda *a: self._debounce_um_search())
        self.um_search_entry = ttk.Entry(bar, textvariable=self.um_search, width=26)
        self.um_search_entry.pack(side="left", padx=(2, 0))
        cols = ("side", "file", "ver", "mtime", "size")
        heads = ("端别", "文件名", "识别版本", "修改时间", "大小(KB)")
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6)
        self.um_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, (70, 380, 90, 130, 80)):
            self.um_tree.heading(c, text=h)
            self.um_tree.column(c, width=w, anchor="w")
        self.um_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.um_tree, tree_frame)
        self.um_tree._view_key = "unmanaged"
        self._make_sortable(self.um_tree, desc_first_cols=("mtime", "size"))
        self._restore_view(self.um_tree)
        self.um_tree.bind("<Button-3>", lambda e: self._popup(self.um_tree, self._um_menu, e))
        self.um_tree.bind("<Double-1>", self._um_reveal)
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="关联到已有条目", command=self.associate_unmanaged).pack(side="left", padx=2)
        ttk.Button(btns, text="注册为自定义mod", command=self.register_unmanaged).pack(side="left", padx=2)
        ttk.Button(btns, text="忽略此文件", command=self.ignore_unmanaged).pack(side="left", padx=2)
        ttk.Button(btns, text="全部忽略", command=self.ignore_all_unmanaged).pack(side="left", padx=2)
        ttk.Button(btns, text="恢复已排除文件", command=self.restore_ignored).pack(side="left", padx=2)
        ttk.Button(btns, text="重新扫描", command=self.refresh_unmanaged).pack(side="left", padx=2)

    def _build_custom_tab(self, tab):
        cols = ("name", "cn", "side", "type", "src")
        heads = ("名称", "中文名", "端别", "源类型", "源")
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6, pady=4)
        self.cust_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        for c, h, w in zip(cols, heads, (200, 120, 70, 110, 360)):
            self.cust_tree.heading(c, text=h)
            self.cust_tree.column(c, width=w, anchor="w")
        self.cust_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.cust_tree, tree_frame)
        self.cust_tree._view_key = "custom"
        self._make_sortable(self.cust_tree)
        self._restore_view(self.cust_tree)
        self.cust_tree.bind("<Button-3>", lambda e: self._popup(self.cust_tree, self._cust_menu, e))
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="添加", command=self.add_custom_dialog).pack(side="left", padx=2)
        ttk.Button(btns, text="编辑", command=self.edit_custom_dialog).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self.remove_custom).pack(side="left", padx=2)

    def _build_settings_tab(self, tab):
        # ---- 目录 ----
        f = ttk.LabelFrame(tab, text="mods 目录")
        f.pack(fill="x", padx=12, pady=(12, 6))

        def path_row(row, label, attr):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=6)
            e = ttk.Entry(f, width=60)
            e.grid(row=row, column=1, sticky="we", padx=6)
            setattr(self, attr, e)

            def browse():
                p = filedialog.askdirectory(title=label)
                if p:
                    e.delete(0, "end")
                    e.insert(0, p)
            ttk.Button(f, text="浏览...", command=browse).grid(row=row, column=2)

        path_row(0, "客户端 mods 目录", "client_entry")
        path_row(1, "服务端 mods 目录", "server_entry")
        f.columnconfigure(1, weight=1)

        # ---- 网络（GitHub / 代理）----
        nf = ttk.LabelFrame(tab, text="网络（GitHub 访问）")
        nf.pack(fill="x", padx=12, pady=6)
        ttk.Label(nf, text="GitHub Token（可选，提高API限额；匿名 60 次/时）").grid(
            row=0, column=0, sticky="w", pady=6)
        self.token_entry = ttk.Entry(nf, width=46, show="*")
        self.token_entry.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(nf, text="代理地址（host:port，留空跟随系统）").grid(
            row=1, column=0, sticky="w", pady=6)
        self.proxy_entry = ttk.Entry(nf, width=46)
        self.proxy_entry.grid(row=1, column=1, sticky="we", padx=6)
        ttk.Button(nf, text="测试连接", command=self.test_network_settings).grid(
            row=0, column=2, rowspan=2, padx=6)
        ttk.Label(nf, text="获取 Token: github.com/settings/tokens（只需公开仓库读取权限）",
                  foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))
        nf.columnconfigure(1, weight=1)

        # ---- 常规 ----
        gf = ttk.LabelFrame(tab, text="常规")
        gf.pack(fill="x", padx=12, pady=6)
        ttk.Label(gf, text="检查更新结果缓存（小时）").grid(row=0, column=0, sticky="w", pady=6)
        self.interval_entry = ttk.Entry(gf, width=10)
        self.interval_entry.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(gf, text="每mod备份保留数").grid(row=1, column=0, sticky="w", pady=6)
        self.backup_entry = ttk.Entry(gf, width=10)
        self.backup_entry.grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(gf, text="GTNH整合包版本（兼容推荐用）").grid(row=2, column=0, sticky="w", pady=6)
        self.gtnh_entry = ttk.Entry(gf, width=10)
        self.gtnh_entry.grid(row=2, column=1, sticky="w", padx=6)
        act = ttk.Frame(gf)
        act.grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(act, text="保存设置", command=self.save_settings).pack(side="left", padx=2)
        ttk.Button(act, text="打开操作日志", command=self._open_log_file).pack(side="left", padx=8)

        # ---- Wiki 反爬 Cookie（站点开启 Cloudflare 人机验证后使用）----
        wf = ttk.LabelFrame(tab, text="Wiki 反爬 Cookie（Cloudflare 人机验证）")
        wf.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Label(wf, text="Wiki Cookie").grid(row=0, column=0, sticky="w", pady=4)
        self.wiki_cookie_entry = ttk.Entry(wf, width=60)
        self.wiki_cookie_entry.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(wf, text="配套 User-Agent").grid(row=1, column=0, sticky="w", pady=4)
        self.wiki_ua_entry = ttk.Entry(wf, width=60)
        self.wiki_ua_entry.grid(row=1, column=1, sticky="we", padx=6)
        wbtns = ttk.Frame(wf)
        wbtns.grid(row=2, column=0, columnspan=2, sticky="w", padx=2, pady=4)
        ttk.Button(wbtns, text="从剪贴板导入 cURL", command=self.import_wiki_curl).pack(side="left")
        ttk.Button(wbtns, text="测试抓取", command=self.test_wiki_fetch).pack(side="left", padx=6)
        ttk.Button(wbtns, text="清除", command=self.clear_wiki_cookie).pack(side="left")
        ttk.Label(wf, text="获取方法：浏览器打开 wiki 并通过人机验证 → F12 → Network → 刷新页面 → "
                           "点第一个文档请求 → 右键 Copy → Copy as cURL → 点「从剪贴板导入 cURL」。"
                           "cf_clearance 与浏览器 UA/出口 IP 绑定：若上面配了代理，浏览器需走同一出口；"
                           "Cookie 过期后重新导入即可。",
                  wraplength=780, foreground="#666", justify="left").grid(
            row=3, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
        wf.columnconfigure(1, weight=1)
        self.wiki_cookie_entry.insert(0, self.cfg.wiki_cookie)
        self.wiki_ua_entry.insert(0, self.cfg.wiki_ua)

        self.client_entry.insert(0, str(self.cfg.client_mods_dir or ""))
        self.server_entry.insert(0, str(self.cfg.server_mods_dir or ""))
        self.token_entry.insert(0, self.cfg.github_token)
        p = self.cfg.proxy
        self.proxy_entry.insert(0, f'{p["host"]}:{p.get("port", "")}' if p else "")
        self.interval_entry.insert(0, str(self.cfg.check_interval_hours))
        self.backup_entry.insert(0, str(self.cfg.backup_keep))
        self.gtnh_entry.insert(0, self.cfg.data.get("gtnh_version") or "")

    # ---------- 通用 ----------
    def _log(self, msg):
        # 已在底部才自动滚动；用户往上翻看历史时不打扰
        at_bottom = self.log.yview()[1] >= 0.99
        self.log.configure(state="normal")
        self.log.insert("end", f"{utils.now_str()}  {msg}\n")
        if at_bottom:
            self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_log_file(self):
        log_path = utils.log_file_path(self.cfg.data_dir)
        if log_path.exists():
            os.startfile(log_path)
        else:
            messagebox.showinfo("提示", "暂无操作日志（执行过安装/更新/开关等操作后生成）")

    def _set_busy(self, busy: bool):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in self.busy_buttons:
            b.configure(state=state)
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            fn = getattr(self, "_pending_debounce", None)
            if fn is not None:
                self._pending_debounce = None
                self.root.after(50, fn)  # 补执行 busy 期间挂起的刷新

    def _push_progress(self, done, total, label):
        """后台线程推送进度事件。"""
        self.queue.put(("_progress", None, (done, total, label)))

    def _on_progress(self, payload):
        done, total, label = payload
        if total:
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.progress.stop()
            self.progress.configure(value=min(100.0, done * 100.0 / total))
        self.status_var.set(label)

    def _download_progress_cb(self, label):
        """单文件下载的字节级进度回调（按百分比节流）。"""
        state = {"last_pct": -1}

        def cb(done, total):
            if not total:
                return
            pct = int(done * 100 / total)
            if pct != state["last_pct"]:
                state["last_pct"] = pct
                self._push_progress(done, total,
                                    f"{label} {done / 1048576:.1f}/{total / 1048576:.1f} MB")
        return cb

    def _run_async(self, fn, on_done=None):
        """在后台线程执行 fn()；完成后在主线程回调 on_done(result)。"""

        def worker():
            try:
                result = fn()
                self.queue.put(("_done", on_done, result))
            except Exception as e:
                self.queue.put(("_error", on_done, str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def _on_tab_changed(self, event):
        """切换页签时刷新对应页（设置页除外）。"""
        idx = self.nb.index("current")
        (self.refresh_installed, self.refresh_addable,
         self.refresh_unmanaged, self.refresh_custom, lambda: None)[idx]()

    def _poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind, payload = item[0], item[1:]
                if kind == "log":
                    self._log(payload[0])
                elif kind == "busy":
                    self._set_busy(payload[0])
                elif kind == "refresh":
                    self.refresh_all()
                elif kind == "_progress":
                    self._on_progress(payload[1])
                elif kind == "_done":
                    if payload[0]:
                        payload[0](payload[1])
                elif kind == "_error":
                    msg = payload[1]
                    if payload[0]:
                        # 有回调的（批量流程逐项跳过等）由回调决定呈现方式，
                        # 避免网络断开时 N 个失败连弹 N 个模态框
                        payload[0](None)
                        self._log(f"[错误] {msg}")
                    else:
                        messagebox.showerror("操作失败", msg)
                        self._log(f"[错误] {msg}")
        except queue.Empty:
            pass
        except Exception:
            # 单个回调异常不能杀死事件泵（否则 busy 永久卡死、界面假死）
            self._report_cb_exc(*sys.exc_info())
        finally:
            try:
                self.root.after(100, self._poll_queue)
            except tk.TclError:
                pass  # 窗口已销毁（退出中）

    def refresh_all(self):
        self.refresh_installed()
        self.refresh_addable()
        self.refresh_unmanaged()
        self.refresh_custom()

    # ---------- 已安装页 ----------
    def _merged_registry(self):
        """build_merged_registry 结果缓存：一次刷新内复用，搜索/状态栏不再重复扫盘。"""
        if self._merged_cache is None:
            self._merged_cache = updater.build_merged_registry(self.cfg, self.db, self.installed)
        return self._merged_cache

    def _debounce_inst_search(self):
        self._debounce("_inst_search_after", self._render_installed)

    def _debounce_add_search(self):
        self._debounce("_add_search_after", self.refresh_addable)

    def _debounce_um_search(self):
        self._debounce("_um_search_after", self.refresh_unmanaged)

    def _debounce(self, attr, fn):
        """搜索防抖：200ms 内连续输入只触发最后一次刷新；busy 时记挂起，
        操作结束后自动补执行（搜索词与列表不再不同步）。"""
        if self.busy:
            self._pending_debounce = fn
            return
        if getattr(self, attr, None):
            self.root.after_cancel(getattr(self, attr))
        setattr(self, attr, self.root.after(200, fn))

    def _inst_rows(self):
        """合并注册表 + 端别过滤 + 搜索 + 仅可更新过滤。"""
        merged = self._merged_registry()
        filt = self.side_filter.get()
        kw = self.search_var.get().strip().lower()
        only_upd = self.only_update.get()
        rows = []
        for m in merged:
            if filt != "all" and m["install_side"] != filt:
                continue
            if only_upd and m["status"] != "update_avail":
                continue
            hay = " ".join((m["name_en"], m["name_cn"] or "", m["mod_id"],
                            *(st.get("file_name") or "" for st in m["sides"].values()))).lower()
            if kw and kw not in hay:
                continue
            rows.append(m)
        return rows

    def refresh_installed(self):
        if not hasattr(self, "inst_tree"):
            return
        self._merged_cache = None  # 磁盘/数据库可能已变化
        self._render_installed()

    @staticmethod
    def _name_cell(m) -> str:
        """名称列：中文名为主（括号附英文名），无中文名用英文名。"""
        cn, en = (m.get("name_cn") or "").strip(), m.get("name_en") or m["mod_id"]
        base = f"{cn}（{en}）" if cn and cn != en else en
        lock = "🔒" if m["locked"] else ""
        dup = " ⚠重复jar" if m.get("duplicates") else ""
        return base + lock + dup

    @staticmethod
    def _fmt_ver(v) -> str:
        """版本显示统一 v 前缀（解析结果自带 v 时不叠加，如 v1.85-Multi 不变 vv…）。"""
        v = str(v or "").strip()
        if not v:
            return ""
        if v[0] in "vV" and v[1:2].isdigit():
            return "v" + v[1:]
        return "v" + v if v[0].isdigit() else v

    @staticmethod
    def _version_cell(m) -> str:
        """已装版本列：双端一致只显示一份；不一致或部分禁用时分别标注（✗=禁用）。"""
        def cell(side):
            st = m["sides"].get(side)
            if not st:
                return None
            return GuiApp._fmt_ver(st.get("version") or "?") + ("" if st["enabled"] else "✗")
        c, s = cell("client"), cell("server")
        if c and s:
            return c if c == s else f"客 {c} / 服 {s}"
        return c or s or "—"

    def _open_inst_detail_selection(self):
        """回车键：打开当前选中已安装mod的详情。"""
        sel = self._selected_inst()
        if sel:
            self._show_inst_detail(sel[0])

    def _render_installed(self):
        """用（缓存的）注册表重绘已安装列表，尽量恢复选中与滚动位置。"""
        keep_sel = set(self.inst_tree.selection())
        keep_yview = self.inst_tree.yview()
        self.inst_tree.delete(*self.inst_tree.get_children())
        self.inst_rows = {}
        # 新手引导：两端目录均未配置时显示
        if not self.cfg.client_mods_dir and not self.cfg.server_mods_dir:
            self.guide.pack(fill="x", padx=6, pady=(0, 4), before=self.inst_tree_frame)
        else:
            self.guide.pack_forget()
        for i, m in enumerate(self._inst_rows()):
            status = m["status"]
            base = {"update_avail": "upd", "update_incompat": "incompat",
                    "disabled": "dis"}.get(status, "")
            # 锁定优先级最高：锁定的 mod 不会被"全部更新"动到，整行蓝紫提示"这个不会动"，
            # 状态列注明是否恰好有新版
            if m["locked"]:
                tags = ("locked",)
                if status == "update_avail":
                    status_txt = "可更新（已锁定）"
                elif status == "update_incompat":
                    status_txt = "有新版（可能不兼容，已锁定）"
                else:
                    status_txt = "已锁定"
            elif base == "upd":
                tags, status_txt = ("upd",), STATUS_CN[status]
            elif base == "incompat":
                tags, status_txt = ("incompat",), STATUS_CN[status]
            elif base == "dis":
                tags, status_txt = ("dis",), STATUS_CN["disabled"]
            else:
                tags = ("odd",) if i % 2 else ()
                status_txt = STATUS_CN.get(status, status)
            self.inst_tree.insert(
                "", "end", iid=m["mod_id"], tags=tags,
                values=(self._name_cell(m),
                        INSTALL_SIDE_CN.get(m["install_side"], m["install_side"]),
                        self._version_cell(m),
                        m["install_time"] or "—",
                        self._fmt_ver(m["latest_version"]),
                        status_txt))
            self.inst_rows[m["mod_id"]] = m
        # 底部状态栏（路径缩略显示，完整路径见设置页）
        merged = self._merged_registry()
        client = self._short_path(str(self.cfg.client_mods_dir or "未设置"))
        server = self._short_path(str(self.cfg.server_mods_dir or "未设置"))
        self._set_status(f"客户端: {client}   服务端: {server}   "
                         f"已装受管mod: {len(merged)} 个"
                         f"（显示 {len(self.inst_rows)} 个）")
        self._reapply_sort(self.inst_tree)
        if keep_sel:
            self.inst_tree.selection_set([i for i in keep_sel if i in self.inst_rows])
        if keep_yview[0] > 0:
            self.inst_tree.yview_moveto(keep_yview[0])

    @staticmethod
    def _short_path(p: str, max_parts: int = 2) -> str:
        """状态栏用的路径缩略：保留盘符与最后两级目录（F:\…\instance\mods）。"""
        if not p or p == "未设置":
            return p
        path = Path(p)
        parts = path.parts
        if len(parts) <= max_parts + 1:
            return p
        return str(path.drive + "\\…\\" + "\\".join(parts[-max_parts:]))

    def _selected_inst(self):
        sel = self.inst_tree.selection()
        return [self.inst_rows[i] for i in sel if i in self.inst_rows]

    def check_updates(self):
        if self.busy:
            return
        if not self._dirs_ok():
            return
        self._set_busy(True)
        self._log("开始检查更新（已安装且未锁定的mod）...")
        progress_cb = lambda s, m: self.queue.put(("log", f"  已检查 {SIDE_LABELS[s]}: {m}"))
        self._run_async(
            lambda: updater.check_updates(self.cfg, self.db, self.installed,
                                          progress_cb=progress_cb),
            on_done=self._on_check_done)

    def _on_check_done(self, results):
        if results is None:
            results = []
        self._set_busy(False)
        reg = updater.build_registry(self.cfg, self.db, self.installed)
        counts = updater.summarize_check(results, reg)
        for side, mod_id, info, err in sorted(results, key=lambda r: r[1]):
            entry = self.db.get(mod_id) or {}
            name = entry.get("name_en") or mod_id
            if err:
                self._log(f"[错误] {SIDE_LABELS[side]} {name}: {err}")
                continue
            if not info or not info.latest_version:
                self._log(f"{SIDE_LABELS[side]} {name}: {info.note if info else '无信息'}")
                continue
            cur = (reg[side].get(mod_id) or {}).get("version")
            st = updater.version_status(cur, info.latest_version)
            if st == "update":
                self._log(f"{SIDE_LABELS[side]} {name}: v{cur} → v{info.latest_version} 可更新")
            elif st == "uptodate":
                self._log(f"{SIDE_LABELS[side]} {name}: v{cur} 已最新")
            else:
                self._log(f"{SIDE_LABELS[side]} {name}: 当前v{cur}，最新v{info.latest_version}（请手动判断）")
            if info.candidates is None and info.latest_version:
                self._log(f"  （{name} 无自动下载资产，需手动下载）")
        self._log(f"检查完成：发现 {counts['update']} 个可更新"
                  + (f"，{counts['error']} 个出错" if counts["error"] else ""))
        self.refresh_installed()

    def update_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要更新的mod（Ctrl可多选）")
            return
        if self.busy:
            return
        ans = messagebox.askyesnocancel(
            "确认更新", f"更新选中的 {len(sel)} 个mod（所有已装端别）？\n\n"
                       "「是」：逐个弹出版本选择器，可精确挑选\n"
                       "「否」：全部使用推荐版本（最新兼容版），不再逐个询问\n"
                       "「取消」：返回\n\n旧版本自动备份到 data/backup。")
        if ans is None:
            return
        self._start_update_flow(list(sel), mode="pick" if ans else "recommend")

    def _start_update_flow(self, mods, mode="pick"):
        """逐个处理：弹版本选择器 → 更新 → 下一个（单个mod失败不影响其他）。

        mode="recommend" 跳过版本选择器，直接用推荐版本（最新兼容版）。
        """
        self._pending_updates = list(mods)
        self._update_total = len(mods)
        self._update_recommend = mode == "recommend"
        self._update_results = []
        self._set_busy(True)
        self._process_next_update()

    def _process_next_update(self):
        if not self._pending_updates:
            self._finish_updates()
            return
        m = self._pending_updates.pop(0)
        entry = self.db.get(m["mod_id"]) or {}
        idx = self._update_total - len(self._pending_updates)  # pop 后即第 idx 个（1-based）
        self._push_progress(idx, self._update_total,
                            f"批量更新 {idx}/{self._update_total}: {m['name_en']}")
        if getattr(self, "_update_recommend", False):
            self._run_update_one(m, None)  # 推荐模式：跳过版本选择器
            return
        self._log(f"正在获取 {m['name_en']} 的可用版本列表...")
        self._run_async(
            lambda: updater.list_install_options(entry, self.cfg, self.db, force=True),
            on_done=lambda result: self._on_versions_for_update(result, m))

    def _on_versions_for_update(self, result, m):
        if result is None:
            self._log(f"[错误] {m['name_en']}: 获取版本列表失败，已跳过")
            self._process_next_update()
            return
        options, err = result
        if err:
            self._log(f"[错误] {m['name_en']}: 获取版本列表失败（{err}），已跳过")
            self._process_next_update()
            return
        if not options:
            # manual/curseforge 源无版本列表 → 直接更新到最新
            self._run_update_one(m, None)
            return
        cur = m["sides"].get("client") or next(iter(m["sides"].values()))
        ver = self._version_picker(options, current=cur["version"],
                                   title=f"选择要更新到的版本 - {m['name_en']}")
        if ver is None:
            self._log(f"已跳过 {m['name_en']}（未选择版本）")
            self._process_next_update()
            return
        self._run_update_one(m, ver, prefetched=options)

    def _run_update_one(self, m, ver, prefetched=None):
        self._set_busy(True)

        def job():
            out = []
            for side in m["sides"]:
                try:
                    r = updater.update_mod(self.cfg, self.db, self.installed,
                                           m["mod_id"], side, version=ver,
                                           progress_cb=self._download_progress_cb(
                                               f"下载 {m['name_en']}"),
                                           prefetched=prefetched)
                except Exception as e:  # 单个mod异常不影响其他
                    r = {"action": "error", "error": str(e)}
                r["side"], r["mod_id"], r["name"] = side, m["mod_id"], m["name_en"]
                out.append(r)
            return out

        def done(rs):
            self._update_results.extend(rs or [])
            self._process_next_update()
        self._run_async(job, on_done=done)

    def _finish_updates(self):
        self._on_update_done(self._update_results)

    def update_all(self):
        if self.busy:
            return
        if not messagebox.askyesno(
                "确认", "更新所有已安装且未锁定、启用的mod？\n"
                       "只更新有新版本的（已最新/禁用/锁定的跳过），一律装最新兼容版，\n"
                       "旧版本会自动备份。"):
            return
        self._set_busy(True)

        def job():
            reg = updater.build_registry(self.cfg, self.db, self.installed)

            def cb(done, total, name):
                self._push_progress(done, total, f"全部更新 {done}/{total}: {name}")
            return updater.update_all(self.cfg, self.db, self.installed,
                                      progress_cb=cb, registry=reg)

        self._run_async(job, on_done=lambda rs: self._on_update_done(rs or []))

    def _offer_open_download_page(self, entry):
        """manual 源的引导闭环：询问是否打开下载页（对齐 CLI）。"""
        urls = (entry or {}).get("urls") or {}
        url = urls.get("curseforge") or urls.get("github")
        if url and messagebox.askyesno("需要手动下载", f"打开下载页面？\n{url}"):
            webbrowser.open(url)

    def _on_update_done(self, results):
        self._set_busy(False)
        n_ok = 0
        for r in results:
            name = r.get("name") or r.get("mod_id", "?")
            label = SIDE_LABELS.get(r.get("side", "?"), r.get("side", "?"))
            if r["action"] == "updated":
                n_ok += 1
                self._log(f"已更新 {label} {name}: v{r['from']} → v{r['to']}")
                if r.get("body"):
                    self._log(f"  更新日志: {r['body'][:200]}…")
                if r.get("warning"):
                    self._log(f"  [端别提示] {r['warning']}")
            elif r["action"] == "uptodate":
                self._log(f"{label} {name}: 已是最新")
            elif r["action"] == "manual":
                self._log(f"{label} {name}: {r.get('note') or '需手动下载'}")
                if not self.busy:
                    self._offer_open_download_page(r.get("entry"))
            elif r["action"] == "skipped_incompatible":
                self._log(f"[跳过] {label} {name}: {r.get('note')}")
            else:
                self._log(f"[错误] {label} {name}: {r.get('error')}")
                if r.get("warning"):
                    self._log(f"  [端别提示] {r['warning']}")
            if r.get("leftover"):
                self._log(f"[提示] {label} 旧版本文件被占用未能移除: {'、'.join(r['leftover'])}"
                          "（请关闭游戏/服务端后右键「清理重复jar」）")
        self._log(f"完成：成功更新 {n_ok} 个")
        self.refresh_all()
        if results:
            self._update_summary_dialog(results)

    def _update_summary_dialog(self, results):
        """批量更新完成后的摘要窗：成功/失败/需手动分组，可看完整更新日志。"""
        groups = {"updated": [], "error": [], "manual": [], "other": []}
        for r in results:
            name = f"{SIDE_LABELS.get(r.get('side', '?'), '?')} {r.get('name') or r.get('mod_id', '?')}"
            if r["action"] == "updated":
                groups["updated"].append((name, r))
            elif r["action"] in ("manual", "skipped_incompatible"):
                groups["manual"].append((name + f"：{r.get('note') or '需手动'}", r))
            elif r["action"] != "uptodate":
                groups["error"].append((name + f"：{r.get('error') or '失败'}", r))
        top = tk.Toplevel(self.root)
        top.bind("<Escape>", lambda _e: top.destroy())
        top.title("批量更新完成")
        top.geometry("720x480")
        top.transient(self.root)
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        t = tk.Text(frame, wrap="word", font=FONT)
        t.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(t, frame)

        def section(title, items, with_body=False):
            if not items:
                return
            nl = chr(10)
            t.insert("end", f"■ {title}（{len(items)} 个）" + nl, "h")
            for name, r in items:
                t.insert("end", "  ● " + name + nl)
                if with_body and r.get("body"):
                    indented = nl + "    ".join(("    " + r["body"].strip()[:600]).split(nl))
                    t.insert("end", "    " + indented.strip() + nl)
            t.insert("end", nl)
        t.tag_configure("h", font=(FONT[0], FONT[1], "bold"))
        section("已更新", groups["updated"], with_body=True)
        section("需手动处理", groups["manual"])
        section("失败", groups["error"])
        n_upd = len(groups["updated"])
        n_fail = len(groups["error"]) + len(groups["manual"])
        t.insert("end", f"合计：成功 {n_upd}，需手动 {len(groups['manual'])}，失败 {len(groups['error'])}")
        t.configure(state="disabled")
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _check_not_busy(self) -> bool:
        """操作前检查；busy 时给出反馈而不是静默吞掉。"""
        if self.busy:
            messagebox.showinfo("提示", "有操作正在进行，请等它完成后再试")
            return False
        return True

    def toggle_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要切换的mod")
            return
        if not self._check_not_busy():
            return
        # 确认框在主线程弹；文件操作移后台线程（不冻结界面、不与更新并发）
        jobs = []
        for m in sel:
            want = not all(st["enabled"] for st in m["sides"].values())
            if not want and self.cfg.data.get("core_mod_confirm") \
                    and not messagebox.askyesno("确认", f"禁用 {m['name_en']}？\n（影响端别：{INSTALL_SIDE_CN[m['install_side']]}）"):
                return
            jobs.append((m, want))
        self._set_busy(True)

        def job():
            out = []
            for m, want in jobs:
                for side in m["sides"]:
                    r = updater.set_enabled(self.cfg, self.db, self.installed,
                                            m["mod_id"], side, want)
                    out.append((want, side, m["name_en"], r))
            return out

        def done(out):
            self._set_busy(False)
            for want, side, name, r in out:
                if r["action"] in ("enabled", "disabled"):
                    self._log(f"已{'启用' if want else '禁用'} {SIDE_LABELS[side]} {name}")
                elif r["action"] != "unchanged":
                    self._log(f"[错误] {SIDE_LABELS[side]} {name}: {r.get('error')}")
            self.refresh_installed()
        self._run_async(job, on_done=done)

    def toggle_one_side(self, m, side, enable):
        """按端别单独启用/禁用（双端安装的mod）。"""
        if not self._check_not_busy():
            return
        self._set_busy(True)

        def job():
            return updater.set_enabled(self.cfg, self.db, self.installed,
                                       m["mod_id"], side, enable)

        def done(r):
            self._set_busy(False)
            if r["action"] in ("enabled", "disabled"):
                self._log(f"已{'启用' if enable else '禁用'} {SIDE_LABELS[side]} {m['name_en']}")
            elif r["action"] != "unchanged":
                self._log(f"[错误] {SIDE_LABELS[side]} {m['name_en']}: {r.get('error')}")
            self.refresh_installed()
        self._run_async(job, on_done=done)

    def lock_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中mod")
            return
        if not self._check_not_busy():
            return
        jobs = [(m, not m["locked"]) for m in sel]
        self._set_busy(True)

        def job():
            out = []
            for m, new in jobs:
                for side in m["sides"]:
                    updater.set_lock(self.installed, m["mod_id"], side, new)
                out.append((m["name_en"], new))
            return out

        def done(out):
            self._set_busy(False)
            for name, new in out:
                self._log(f"{name}: 已{'锁定' if new else '解锁'}（两端同步）")
            self.refresh_installed()
        self._run_async(job, on_done=done)

    def exclude_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要剔除的mod")
            return
        if not self._check_not_busy():
            return
        names = "、".join(m["name_en"] for m in sel)
        if not messagebox.askyesno("确认", f"从受管列表剔除 {names}？\n"
                                    "（不会删除文件；可在「未受管MOD」页的“恢复已排除文件”中恢复显示）"):
            return
        self._set_busy(True)

        def job():
            return [(m["name_en"],
                     updater.exclude_installed(self.cfg, self.db, self.installed, m["mod_id"]))
                    for m in sel]

        def done(out):
            self._set_busy(False)
            for name, files in out:
                self._log(f"已剔除 {name}: {', '.join(files) or '无文件'}")
            self.refresh_all()
        self._run_async(job, on_done=done)

    def delete_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的mod")
            return
        if not self._check_not_busy():
            return
        names = "\n".join("  " + m["name_en"]
                          + f"（{INSTALL_SIDE_CN[m['install_side']]}）" for m in sel)
        if not messagebox.askyesno(
                "确认删除",
                f"删除选中的 {len(sel)} 个mod？\n{names}\n\n"
                "jar 会移入 data/backup 备份目录并加 .deleted 后缀，可手动恢复"):
            return
        self._set_busy(True)

        def job():
            return [(m["name_en"],
                     updater.delete_mod(self.cfg, self.db, self.installed, m["mod_id"]))
                    for m in sel]

        def done(out):
            self._set_busy(False)
            for name, r in out:
                if r["action"] == "deleted":
                    for d in r["deleted"]:
                        self._log(f"已删除 {d}")
                if r.get("error"):
                    self._log(f"[错误] 删除 {name}: {r['error']}")
            self.refresh_all()
        self._run_async(job, on_done=done)

    def open_link_selected(self):
        sel = self._selected_inst()
        items = []
        for m in sel:
            entry = self.db.get(m["mod_id"]) or {}
            url = (entry.get("urls") or {}).get("github") or (entry.get("urls") or {}).get("curseforge")
            if url:
                items.append((m["name_en"], url))
        self._open_link_items(items)

    def _open_link_items(self, items):
        """单个直接打开；多个弹链接清单（避免一次炸开 N 个浏览器标签）。"""
        if not items:
            messagebox.showinfo("提示", "选中的mod没有可用下载链接")
            return
        if len(items) == 1:
            webbrowser.open(items[0][1])
            self._log(f"已打开下载页: {items[0][0]}")
            return
        top = self._dialog(f"链接清单（{len(items)} 个）", "680x380")
        ttk.Label(top, text="双击行打开；「复制全部链接」可批量粘贴。").pack(
            anchor="w", padx=8, pady=(8, 2))
        lb = tk.Listbox(top, width=92, height=14, font=FONT, exportselection=False)
        lb.pack(fill="both", expand=True, padx=8, pady=4)
        for label, url in items:
            lb.insert("end", f"{label} - {url}")

        def open_sel():
            i = lb.curselection()
            if i:
                webbrowser.open(items[i[0]][1])
                self._log(f"已打开下载页: {items[i[0]][0]}")
        lb.bind("<Double-Button-1>", lambda _e: open_sel())
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="打开选中", command=open_sel).pack(side="left")

        def copy_all():
            text = (chr(10) + "").join(u for _, u in items)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self._log(f"已复制 {len(items)} 个链接到剪贴板")
        ttk.Button(btns, text="复制全部链接", command=copy_all).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _open_link_mod(self, m):
        entry = self.db.get(m["mod_id"]) or {}
        url = (entry.get("urls") or {}).get("github") or (entry.get("urls") or {}).get("curseforge")
        if url:
            webbrowser.open(url)
            self._log(f"已打开 {m['name_en']} 下载页")
        else:
            self._log(f"{m['name_en']} 没有可用下载链接")

    def _check_single(self, m):
        """右键：只检查这一个mod的更新。"""
        if self.busy:
            return
        self._set_busy(True)
        self._log(f"检查更新: {m['name_en']}...")
        self._run_async(
            lambda: updater.check_updates(self.cfg, self.db, self.installed,
                                          only={m["mod_id"]}),
            on_done=self._on_check_done)

    def _on_inst_double(self, event):
        # 双击 → 查看详情（与可添加页一致；资源管理器定位在右键菜单）
        iid = self.inst_tree.identify_row(event.y)
        m = self.inst_rows.get(iid)
        if m:
            self._show_inst_detail(m)

    # ---------- 右键菜单 ----------
    def _inst_menu(self, menu, iid):
        m = self.inst_rows.get(iid)
        if not m:
            menu.add_command(label="（未选中mod）", state="disabled")
            return
        sel = self._selected_inst()
        multi = len(sel) > 1
        tag = f"（选中{len(sel)}个）" if multi else ""
        menu.add_command(label="查看详情", command=lambda: self._show_inst_detail(m))
        menu.add_command(label=f"检查更新: {m['name_en']}", command=lambda: self._check_single(m))
        if multi:
            menu.add_command(label=f"批量更新{tag}",
                             command=lambda: self._start_update_flow(list(sel)))
        else:
            menu.add_command(label="更新...", command=lambda: self._start_single_update(m))
        if multi:
            menu.add_command(label=f"回滚到更新前版本{tag}",
                             command=lambda: self._rollback_mods(list(sel)))
        else:
            menu.add_command(label="回滚到更新前版本...",
                             command=lambda: self._rollback_mods([m]))
        if multi:
            # 多选时批量操作（与单选同菜单，按选中数标注）
            menu.add_command(label=f"启用/禁用{tag}", command=self.toggle_selected)
            menu.add_command(label=f"锁定/解锁{tag}", command=self.lock_selected)
            menu.add_command(label=f"打开下载页{tag}", command=self.open_link_selected)
        else:
            menu.add_command(label="启用/禁用", command=self.toggle_selected)
            menu.add_command(label="锁定/解锁", command=self.lock_selected)
            menu.add_command(label="打开下载页", command=lambda: self._open_link_mod(m))
            menu.add_command(label="浏览备份…", command=lambda: self._backup_dialog(m))
        # 双端安装时可按端别单独启停（对齐 CLI 能力）
        if not multi and len(m["sides"]) > 1:
            sub2 = tk.Menu(menu, tearoff=0)
            for s, st in m["sides"].items():
                want = not st["enabled"]
                label = f"{'禁用' if st['enabled'] else '启用'}（{SIDE_LABELS[s]}）"
                sub2.add_command(label=label,
                                 command=lambda s=s, w=want: self.toggle_one_side(m, s, w))
            menu.add_cascade(label="按端别启用/禁用", menu=sub2)
        menu.add_separator()
        sides = list(m["sides"])
        if len(sides) == 1:
            menu.add_command(label=f"在资源管理器中打开（{SIDE_LABELS[sides[0]]}）",
                             command=lambda: self.reveal_mod(m, sides[0]))
        else:
            sub = tk.Menu(menu, tearoff=0)
            for s in sides:
                sub.add_command(label=SIDE_LABELS[s],
                                command=lambda s=s: self.reveal_mod(m, s))
            menu.add_cascade(label="在资源管理器中打开", menu=sub)
        menu.add_separator()
        if multi:
            menu.add_command(label=f"删除mod...{tag}", command=self.delete_selected)
            menu.add_command(label=f"从列表剔除{tag}", command=self.exclude_selected)
        else:
            menu.add_command(label="删除mod...", command=self.delete_selected)
            menu.add_command(label="从列表剔除", command=self.exclude_selected)
        if m.get("duplicates"):
            menu.add_separator()
            menu.add_command(label=f"清理重复jar（{sum(len(v) for v in m['duplicates'].values())}个）",
                             command=lambda: self._cleanup_dups(m))

    def _backup_overview_dialog(self):
        """全局备份总览：跨mod列出全部备份，支持定位与删除。"""
        by_side = updater.list_backups(self.cfg)
        rows = [(side, mod, p) for side in ("client", "server")
                for mod, files in by_side.get(side, {}).items() for p in files]
        top = self._dialog("备份管理（全部）", "880x480")
        total_kb = 0
        for _, _, p in rows:
            try:
                total_kb += p.stat().st_size // 1024
            except OSError:
                pass
        ttk.Label(top, text=f"共 {len(rows)} 个备份，合计约 {total_kb // 1024} MB"
                            "（超出保留数的会在启动时自动清理）",
                  font=FONT).pack(anchor="w", padx=8, pady=(8, 2))
        cols = ("side", "mod", "file", "time", "size")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        for c, h, w in zip(cols, ("端别", "mod", "备份文件", "备份时间", "大小(KB)"),
                           (70, 200, 380, 120, 70)):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(tree, frame)

        def reload_rows():
            tree.delete(*tree.get_children())
            data = updater.list_backups(self.cfg)
            n = 0
            for side in ("client", "server"):
                for mod, files in data.get(side, {}).items():
                    for p in files:
                        try:
                            ts = self._fmt_time(p.stat().st_mtime)
                            kb = p.stat().st_size // 1024
                        except OSError:
                            ts, kb = "?", "?"
                        tree.insert("", "end", iid=f"{side}|{mod}|{p}",
                                    values=(SIDE_LABELS[side], mod, p.name, ts, kb))
                        n += 1
            return n

        reload_rows()

        def selected():
            got = []
            for iid in tree.selection():
                side, mod, path = iid.split("|", 2)
                got.append((side, mod, Path(path)))
            return got

        def do_reveal():
            for side, mod, p in selected():
                self._reveal(p)

        def do_delete():
            if self.busy:
                messagebox.showinfo("提示", "有更新/安装正在进行，请等它完成后再删除")
                return
            got = selected()
            if not got:
                return
            if not messagebox.askyesno("删除备份",
                                       f"删除选中的 {len(got)} 个备份文件？（不可恢复）"):
                return
            n = 0
            for side, mod, p in got:
                try:
                    p.unlink()
                    n += 1
                    self._log(f"已删除备份 {side}/{mod}: {p.name}")
                except OSError as e:
                    messagebox.showerror("删除失败", f"{p.name}: {e}")
            if n:
                # 只 prune 被删文件所属的目录（不越权清理其他 mod 的备份）
                from gtnhmod import downloader as _dl
                touched = {(side, mod) for side, mod, _ in got}
                for side, mod in touched:
                    _dl.prune_backups(self.cfg.backup_dir / side / mod,
                                      self.cfg.backup_keep)
                reload_rows()
                self._log(f"已删除 {n} 个备份文件")

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="在资源管理器中打开", command=do_reveal).pack(side="left")
        ttk.Button(btns, text="删除选中", command=do_delete).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _show_inst_detail(self, m):
        """已安装 mod 的详情窗口：端别版本/文件、远端最新、wiki 详情。"""
        e = self.db.get(m["mod_id"]) or {}
        src = updater.current_source_url(e)
        lines = [
            f"名称: {m['name_en']} {m.get('name_cn') or ''}",
            f"分类: {m.get('group')} / {m.get('category')}",
            f"安装端别: {INSTALL_SIDE_CN.get(m.get('install_side'), '?')}",
            f"状态: {STATUS_CN.get(m.get('status'), m.get('status'))}"
            + ("（已锁定，跳过检查更新）" if m.get("locked") else ""),
            f"下载源: {src or '（无）'}",
        ]
        for side in SIDES:
            st = m["sides"].get(side)
            if not st:
                continue
            dups = list(st.get("duplicates") or [])
            lines.append(f"\n{SIDE_LABELS[side]}绑定的jar（{1 + len(dups)}个）:")
            lines.append(f"  ● {st.get('file_name')}（当前使用 v{st.get('version')}）")
            for d in dups:
                lines.append(f"  ○ {d}（重复，建议清理）")
            if st.get("latest_version"):
                lines.append(f"  远端最新: v{st['latest_version']}")
        if e.get("desc"):
            lines.append(f"\n简介:\n{e['desc']}")
        if e.get("detail"):
            lines.append(f"\n详细信息:\n{e['detail']}")
        top = self._dialog(f"{m['name_en']} {m.get('name_cn') or ''}", "680x460")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        t = tk.Text(frame, wrap="word", font=FONT)
        t.insert("1.0", "\n".join(lines))
        t.configure(state="disabled")
        t.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(t, frame)
        # 操作按钮（更新/回滚/下载页/定位），先关窗再触发避免模态叠加
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        first_side = next(iter(m["sides"]), "client")
        ttk.Button(btns, text="更新…",
                   command=lambda: (top.destroy(), self._start_single_update(m))
                   ).pack(side="left")
        ttk.Button(btns, text="回滚…",
                   command=lambda: (top.destroy(), self._rollback_mods([m]))
                   ).pack(side="left", padx=6)
        entry = self.db.get(m["mod_id"]) or {}
        url = updater.current_source_url(entry) or (entry.get("urls") or {}).get("curseforge")
        if url:
            ttk.Button(btns, text="打开下载页",
                       command=lambda u=url: webbrowser.open(u)).pack(side="left", padx=6)
        ttk.Button(btns, text="打开目录",
                   command=lambda: self.reveal_mod(m, first_side)).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _backup_dialog(self, m):
        """备份管理：列出该mod的全部备份，可恢复任意版本（对齐 CLI 菜单9）。"""
        by_side = updater.list_backups(self.cfg)
        rows = [(side, p) for side in ("client", "server")
                for p in by_side.get(side, {}).get(m["mod_id"], [])]
        top = self._dialog(f"备份管理 - {m['name_en']}", "720x400")
        ttk.Label(top, text="按备份时间倒序；选中一份后点「恢复」，当前文件会先自动备份。",
                  font=FONT).pack(anchor="w", padx=8, pady=(8, 2))
        cols = ("side", "file", "time")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, ("端别", "备份文件", "备份时间"), (80, 440, 140)):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(tree, frame)
        for side, p in rows:
            try:
                ts = self._fmt_time(p.stat().st_mtime)
            except OSError:
                ts = "?"
            tree.insert("", "end", iid=f"{side}|{p}",
                        values=(SIDE_LABELS[side], p.name, ts))
        if not rows:
            tree.insert("", "end", values=("—", "（暂无备份）", "—"))

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        sel_var = {"p": None}

        def selected():
            iid = tree.selection()[0] if tree.selection() else None
            if not iid:
                return None
            side, _, path = iid.partition("|")
            return side, Path(path)

        def do_restore():
            # 对话框可能开着时后台开始了更新（如"全部更新"），恢复必须避开并发
            if self.busy:
                messagebox.showinfo("提示", "有更新/安装正在进行，请等它完成后再恢复")
                return
            got = selected()
            if not got:
                return
            side, path = got
            if not messagebox.askyesno(
                    "确认恢复",
                    f"恢复 {SIDE_LABELS[side]} 的 {path.name}？\n当前文件会先自动备份。"):
                return
            self._set_busy(True)

            def job():
                return updater.restore_backup(self.cfg, self.db, self.installed,
                                              side, path)

            def done(r):
                self._set_busy(False)
                if r["action"] == "restored":
                    self._log(f"已恢复 {m['name_en']}（{SIDE_LABELS[side]}）→ {r['file']}")
                    top.destroy()
                    self.refresh_installed()
                else:
                    messagebox.showerror("恢复失败", r.get("error") or "未知错误")
            self._run_async(job, on_done=done)

        def do_reveal():
            got = selected()
            if got:
                self._reveal(got[1])

        ttk.Button(btns, text="恢复选中备份", command=do_restore).pack(side="left")
        ttk.Button(btns, text="在资源管理器中打开", command=do_reveal).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _cleanup_dups(self, m):
        dups = []
        for side, names in (m.get("duplicates") or {}).items():
            dups += [f"{SIDE_LABELS[side]}: {n}" for n in names]
        msg = (f"清理 {m['name_en']} 的重复jar？\n"
               "（保留最近一次安装/回滚的版本，其余备份到 data/backup 后移除）\n\n"
               f"将清理:\n  " + ("\n  ".join(dups) or "无"))
        if not messagebox.askyesno("确认", msg):
            return
        r = updater.cleanup_duplicates(self.cfg, self.db, self.installed, m["mod_id"])
        for k in r.get("kept") or []:
            self._log(f"保留 {k}")
        if r["action"] == "cleaned":
            for c in r["cleaned"]:
                self._log(f"已清理重复jar {c}")
        else:
            self._log(f"{m['name_en']} 没有需要清理的重复jar")
        for s in r.get("skipped") or []:
            self._log(f"[提示] {s} 未能移除（文件被占用？请关闭游戏/服务端后重试）")
        self.refresh_all()

    def _start_single_update(self, m):
        if self.busy:
            return
        if not messagebox.askyesno("确认", f"更新 {m['name_en']}（所有已装端别）？"):
            return
        self._start_update_flow([m])

    def _rollback_mods(self, mods):
        """一键回滚：恢复每端最近一次更新/替换前的备份 jar。"""
        if self.busy:
            return
        names = "、".join(m["name_en"] for m in mods)
        if not messagebox.askyesno("确认", f"回滚 {names} 到更新前版本？\n"
                                   "（取两端最近一次更新/替换前的备份；"
                                   "双端会恢复到同一版本，当前版本会先备份）"):
            return
        self._set_busy(True)
        self._log(f"开始回滚 {names} ...")

        def job():
            out = []
            for m in mods:
                for r in updater.rollback_mod(self.cfg, self.db, self.installed,
                                              m["mod_id"]):
                    r["mod_id"], r["name"] = m["mod_id"], m["name_en"]
                    out.append(r)
            return out

        def done(rs):
            self._set_busy(False)
            for r in rs or []:
                label = SIDE_LABELS.get(r.get("side", "?"), "?")
                if r["action"] == "restored":
                    self._log(f"已回滚 {label} {r['name']} → {r['file']}")
                    if r.get("leftover"):
                        self._log(f"[提示] {label} 旧版本文件被占用未能移除: "
                                  f"{'、'.join(r['leftover'])}"
                                  "（请关闭游戏/服务端后右键「清理重复jar」）")
                elif r["action"] == "nobackup":
                    self._log(f"{label} {r['name']}: 没有可回滚的备份")
                else:
                    self._log(f"[错误] {label} {r['name']}: {r.get('error')}")
            self.refresh_all()
        self._run_async(job, on_done=done)

    def _add_menu(self, menu, iid):
        e = self.add_rows.get(iid)
        if not e:
            menu.add_command(label="（未选中mod）", state="disabled")
            return
        sel = self._selected_addable()
        menu.add_command(label="查看详情", command=self.show_addable_detail)
        if len(sel) > 1:
            menu.add_command(label=f"批量安装选中({len(sel)}个，自动选择端别)",
                             command=self._batch_install)
        else:
            menu.add_command(label="安装（自动选择端别）", command=self.install_addable)
        menu.add_command(label="打开下载页面", command=self.open_link_addable)
        menu.add_separator()
        menu.add_command(label="编辑中文名...", command=lambda: self._edit_name_cn(e))
        menu.add_command(label="选择下载源...", command=lambda: self._bind_source_dialog(e))

    def _edit_name_cn(self, e):
        """编辑/新增中文名（留空恢复wiki原名）。"""
        top = self._dialog(f"编辑中文名 - {e['name_en']}", "420x150")
        ttk.Label(top, text="中文名（留空=恢复wiki原文）:", font=FONT).pack(anchor="w", padx=12, pady=(12, 4))
        var = tk.StringVar(value=e.get("name_cn") or "")
        ent = ttk.Entry(top, textvariable=var, width=40)
        ent.pack(fill="x", padx=12)
        ent.focus_set()

        def ok():
            r = updater.set_name_cn(self.db, e["id"], var.get())
            if r["action"] == "saved":
                self._log(f"{e['name_en']}: 中文名已更新为 {r['name_cn'] or '（恢复wiki原名）'}")
                top.destroy()
                self.refresh_all()
            else:
                messagebox.showerror("错误", r.get("error"), parent=top)
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="保存", command=ok).pack(side="left", padx=2)
        ttk.Button(btns, text="取消", command=top.destroy).pack(side="left", padx=6)
        top.transient(self.root)
        top.grab_set()

    def _batch_install(self):
        """批量安装选中的mod（按各自端别声明自动选择端别；逐个进行，失败不中断）。"""
        sel = self._selected_addable()
        if not sel:
            messagebox.showinfo("提示", "请先选中mod")
            return
        if self.busy:
            return
        if not messagebox.askyesno("确认", f"批量安装 {len(sel)} 个mod？\n"
                                    "（按每个mod的端别标注自动选择安装位置；装最新版本）"):
            return
        self._set_busy(True)
        self._log(f"批量安装 {len(sel)} 个mod（自动端别）...")

        def job():
            out = []
            done = 0
            for e in sel:
                sides, note = updater.auto_install_sides(e, self.cfg)
                done += 1
                self._push_progress(done, len(sel),
                                    f"批量安装 {done}/{len(sel)}: {e['name_en'] or e['id']}")
                for side in sides:
                    r = updater.install_mod(self.cfg, self.db, self.installed, e["id"], side)
                    r["side"], r["name"] = side, e["name_en"] or e["id"]
                    out.append(r)
                if note:
                    out.append({"action": "skip", "name": e["name_en"] or e["id"], "note": note})
            return out

        def done(rs):
            self._set_busy(False)
            n_ok = 0
            for r in rs or []:
                label = SIDE_LABELS.get(r.get("side", "?"), "?")
                if r["action"] == "installed":
                    n_ok += 1
                    self._log(f"已安装到{label}: {r['name']} v{r['version']}")
                elif r["action"] == "skip":
                    self._log(f"已跳过 {r['name']}: {r.get('note')}")
                elif r["action"] == "manual":
                    self._log(f"{label} {r['name']}: {r.get('note') or '需手动下载'}")
                    self._offer_open_download_page(r.get("entry"))
                elif r["action"] == "skipped_incompatible":
                    self._log(f"[跳过] {label} {r['name']}: {r.get('note')}")
                else:
                    self._log(f"[错误] {label} {r['name']}: {r.get('error')}")
            self._log(f"批量安装完成：成功 {n_ok} 个")
            self.refresh_all()
        self._run_async(job, on_done=done)

    def _bind_source_dialog(self, e):
        """选择该mod的一个下载链接绑定为下载源（检查更新/下载用它）。"""
        cand = updater.bindable_links(e)
        if not cand:
            if messagebox.askyesno("链接列表缺失",
                                   "该mod的下载链接列表不完整（可能是旧版工具保存的数据）。\n"
                                   "是否现在刷新Wiki数据以获取完整链接？"):
                self.refresh_wiki()
            return
        cur = updater.current_source_url(e)
        top = self._dialog(f"选择下载源 - {e['name_en']}", "640x360")
        var = tk.StringVar(value=cur)
        for l in cand:
            mark = "  ← 当前绑定" if l["url"] == cur else ""
            tk.Radiobutton(top, text=f"{l['label']} - {l['url']}{mark}",
                           variable=var, value=l["url"], font=FONT,
                           wraplength=600, justify="left",
                           anchor="w").pack(anchor="w", padx=12, pady=3, fill="x")

        def ok():
            r = updater.bind_source(self.db, e["id"], var.get())
            if r["action"] == "bound":
                self._log(f"{e['name_en']}: 已绑定下载源 {var.get()}")
                top.destroy()
                self.refresh_addable()
            else:
                messagebox.showerror("绑定失败", r.get("error"), parent=top)
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="绑定此链接", command=ok).pack(side="left", padx=2)
        ttk.Button(btns, text="取消", command=top.destroy).pack(side="left", padx=6)
        top.transient(self.root)
        top.grab_set()

    def _um_menu(self, menu, iid):
        match = next(((s, f) for i, s, f in self.um_rows if i == iid), None)
        if not match:
            menu.add_command(label="（未选中文件）", state="disabled")
            return
        side, f = match
        menu.add_command(label="关联到已有条目", command=self.associate_unmanaged)
        menu.add_command(label="注册为自定义mod", command=self.register_unmanaged)
        menu.add_command(label="忽略此文件", command=self.ignore_unmanaged)
        menu.add_command(label="在资源管理器中打开", command=lambda: self._reveal(f.path))

    def _cust_menu(self, menu, iid):
        e = self.db.get(iid)
        if not e:
            menu.add_command(label="（未选中条目）", state="disabled")
            return
        menu.add_command(label="编辑", command=lambda: self._custom_source_dialog(e))
        menu.add_command(label="删除", command=self.remove_custom)

    # ---------- 可添加页 ----------
    def _installed_marks(self) -> dict:
        """{mod_id: install_side}，用于标记可添加列表中已安装的mod。"""
        marks = {}
        for m in self._merged_registry():
            marks[m["mod_id"]] = m["install_side"]
        return marks

    def _addable_row_style(self, e, installed: bool, i: int) -> tuple:
        """可添加行的 (tags, 名称文本)。优先级：已安装(✓绿字) > 新增(🆕浅黄底) > 隔行灰。"""
        if installed:
            return ("inst",), "✓ " + e["name_en"]
        if e["id"] in getattr(self, "_new_ids", ()):
            return ("new",), "🆕 " + e["name_en"]
        if i % 2:
            return ("odd",), e["name_en"]
        return (), e["name_en"]

    def _clear_new_mark(self, e) -> None:
        """查看详情后清除该行的新增高亮（原地更新，不整页刷新）。"""
        if e["id"] not in getattr(self, "_new_ids", ()):
            return
        self._new_ids.discard(e["id"])
        if not hasattr(self, "add_tree") or not e["id"] in self.add_rows:
            return
        i = list(self.add_tree.get_children("")).index(e["id"])
        tags, name_txt = self._addable_row_style(e, e["id"] in self._installed_marks(), i)
        self.add_tree.item(e["id"], tags=tags,
                           values=(name_txt,) + tuple(self.add_tree.set(e["id"], c) for c in
                                                      ("cn", "side", "cat", "installed", "updated", "desc")))

    def refresh_addable(self):
        if not hasattr(self, "add_tree"):
            return
        entries = self.db.wiki_mods()
        groups = {}
        for e in entries:
            groups.setdefault(f"{e['group']}/{e['category']}", []).append(e)
        cats = sorted(groups, key=lambda k: (k.split("/")[0] != "星门规则", k))
        if not self.cat_var.get() or self.cat_var.get() not in cats:
            if cats:
                self.cat_var.set(cats[0])
        self.cat_combo["values"] = cats
        keep_sel = set(self.add_tree.selection())
        keep_yview = self.add_tree.yview()
        self.add_tree.delete(*self.add_tree.get_children())
        cur = self.cat_var.get()
        kw = self.add_search.get().strip().lower()
        marks = self._installed_marks()
        self.add_rows = {}
        for i, e in enumerate(sorted(groups.get(cur, []), key=lambda x: x["id"])):
            hay = " ".join((e["name_en"], e["name_cn"] or "", e["desc"] or "",
                            e["category"] or "")).lower()
            if kw and kw not in hay:
                continue
            if self.only_uninstalled.get() and e["id"] in marks:
                continue
            side_txt = SIDE_LABELS.get(e["side"], e["side"]) + ("?" if e["side_uncertain"] else "")
            installed_txt = INSTALL_SIDE_CN.get(marks.get(e["id"]), "")
            tags, name_txt = self._addable_row_style(e, e["id"] in marks, i)
            desc = e["desc"] or ""
            self.add_tree.insert("", "end", iid=e["id"], tags=tags,
                                 values=(name_txt, e["name_cn"], side_txt,
                                         e["category"], installed_txt or "—",
                                         ((e.get("release_date") or "")
                                          .replace("T", " "))[:16] or "—",
                                         desc[:60] + ("…" if len(desc) > 60 else "")))
            self.add_rows[e["id"]] = e
        n_new = sum(1 for i in self.add_rows if i in self._new_ids)
        self._set_status(f"可添加MOD：分类「{cur}」共 {len(groups.get(cur, []))} 个"
                         f"，显示 {len(self.add_rows)} 个"
                         + (f"，本次新增 {n_new} 个（🆕）" if n_new else ""))
        self._reapply_sort(self.add_tree)
        if keep_sel:
            self.add_tree.selection_set([i for i in keep_sel if i in self.add_rows])
        if keep_yview[0] > 0:
            self.add_tree.yview_moveto(keep_yview[0])

    def _selected_addable(self):
        sel = self.add_tree.selection()
        return [self.add_rows[i] for i in sel if i in self.add_rows]

    def show_addable_detail(self):
        sel = self._selected_addable()
        if not sel:
            return
        e = sel[0]
        self._clear_new_mark(e)  # 查看过详情后不再高亮"新增"
        nav_ids = list(self.add_tree.get_children(""))  # 当前视图顺序（上一个/下一个导航用）
        marks = self._installed_marks()
        installed_txt = INSTALL_SIDE_CN.get(marks.get(e["id"]), "未安装")
        top = self._dialog(f"{e['name_en']} {e['name_cn']}", "680x520")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        t = tk.Text(frame, wrap="word", font=FONT)
        t.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(t, frame)

        # 逐行写入并记录链接位置（双击打开）
        links = (e.get("urls") or {}).get("links") or []
        offset = 0

        def emit(line, link_url=None):
            nonlocal offset
            t.insert("end", line + "\n")
            if link_url:
                start = offset + line.rindex(link_url)
                tag = f"lnk{start}"
                t.tag_configure(tag, foreground="#0a58ca", underline=True)
                t.tag_add(tag, f"1.0+{start}c", f"1.0+{end_c(start, link_url)}c")
                t.tag_bind(tag, "<Double-Button-1>",
                           lambda _e, u=link_url: webbrowser.open(u))
                t.tag_bind(tag, "<Enter>",
                           lambda _e: t.configure(cursor="hand2"))
                t.tag_bind(tag, "<Leave>",
                           lambda _e: t.configure(cursor=""))
            offset += len(line) + 1

        def end_c(start, url):
            return start + len(url)

        emit(f"名称: {e['name_en']} {e['name_cn']}")
        emit(f"分类: {e['group']} / {e['category']}")
        emit("端别: " + SIDE_LABELS.get(e["side"], e["side"])
             + ("（wiki标注不确定）" if e["side_uncertain"] else ""))
        emit(f"已安装: {installed_txt}")
        emit("发布时间: " + (((e.get("release_date") or "").replace("T", " "))[:16]
                            or "（未知，检查更新后获取）"))
        emit("下载源: " + (updater.current_source_url(e) or "（默认第一个GitHub链接）"))
        if e.get("compat"):
            emit("兼容性（wiki 标注）:")
            for r in e["compat"]:
                emit("  - " + (r.get("raw") or str(r)))
        if links:
            emit("链接（双击打开）:")
            for l in links:
                emit(f"  - {l['label']}: {l['url']}", link_url=l["url"])
        if e.get("desc"):
            emit("")
            emit("简介: " + e["desc"])
        emit("")
        emit("详细信息:")
        emit(e.get("detail") or "（无）")
        t.configure(state="disabled")

        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        url = (e.get("urls") or {}).get("github") or (e.get("urls") or {}).get("curseforge")
        if url:
            ttk.Button(btns, text="打开下载页",
                       command=lambda u=url: webbrowser.open(u)).pack(side="left")
        ttk.Button(btns, text="绑定源…",
                   command=lambda: (top.destroy(), self._bind_source_dialog(e))
                   ).pack(side="left", padx=6)
        ttk.Button(btns, text="安装…",
                   command=lambda: (top.destroy(), self._batch_install([e]))
                   ).pack(side="left", padx=6)
        # 上一个/下一个：配合 🆕 新增审阅流，在当前视图顺序内翻页
        idx = nav_ids.index(e["id"]) if e["id"] in nav_ids else 0
        ttk.Label(btns, text=f" {idx + 1}/{len(nav_ids)} ").pack(side="left", padx=4)
        if idx > 0:
            ttk.Button(btns, text="◀ 上一个",
                       command=lambda: self._reopen_addable_detail(nav_ids[idx - 1], top)
                       ).pack(side="left")
        if idx < len(nav_ids) - 1:
            ttk.Button(btns, text="下一个 ▶",
                       command=lambda: self._reopen_addable_detail(nav_ids[idx + 1], top)
                       ).pack(side="left")
        ttk.Button(btns, text="关闭", command=top.destroy).pack(side="right")

    def _reopen_addable_detail(self, mod_id, old_top=None):
        """详情窗的上一个/下一个：先关掉旧窗（否则点一次叠一层），再开新窗。"""
        if old_top is not None:
            old_top.destroy()
        if mod_id in self.add_rows:
            self.add_tree.selection_set(mod_id)
            self.add_tree.see(mod_id)
            self.show_addable_detail()

    def install_addable(self):
        """安装：按mod端别声明自动选择端别。"""
        sel = self._selected_addable()
        if not sel:
            messagebox.showinfo("提示", "请先在列表中选择mod")
            return
        if self.busy:
            return
        e = sel[0]
        sides, note = updater.auto_install_sides(e, self.cfg)
        if not sides:
            messagebox.showerror("错误", note or "两端mods目录均未设置，请先到「设置」页配置")
            return
        side_txt = "、".join(SIDE_LABELS[s] for s in sides)
        if not messagebox.askyesno("确认", f"安装 {e['name_en']} 到 {side_txt}？\n"
                                            f"（自动判断：该mod标注为{SIDE_LABELS.get(e.get('side') or 'both', '?')}）"
                                            + (f"\n{note}" if note else "")):
            return
        self._set_busy(True)
        self._log(f"正在获取 {e['name_en']} 的可用版本列表...")
        self._run_async(
            lambda: updater.list_install_options(e, self.cfg, self.db, force=True),
            on_done=lambda result: self._on_versions_for_install(result, e, sides, note))

    def _on_versions_for_install(self, result, e, sides, note):
        self._set_busy(False)
        if result is None:
            return
        options, err = result
        if err:
            messagebox.showerror("获取版本列表失败", err)
            self._log(f"[错误] {e['name_en']}: {err}")
            return
        if not options:
            # manual/curseforge 源无版本列表 → 直接原流程
            self._run_install(e, sides, None, note)
            return
        ver = self._version_picker(options, current=None,
                                   title=f"选择要安装的版本 - {e['name_en']}")
        if ver is None:
            return
        self._run_install(e, sides, ver, note, prefetched=options)

    def _run_install(self, e, sides, ver, note="", prefetched=None):
        self._set_busy(True)
        self._log(f"开始安装 {e['name_en']}（{ver or '最新'}）→ "
                  + "、".join(SIDE_LABELS[s] for s in sides) + "...")
        if note:
            self._log(f"  {note}")

        def job():
            out = []
            for side in sides:
                r = updater.install_mod(self.cfg, self.db, self.installed, e["id"], side,
                                        version=ver,
                                        progress_cb=self._download_progress_cb(
                                            f"下载 {e['name_en']}"),
                                        prefetched=prefetched)
                r["side"], r["name"] = side, e["name_en"] or e["id"]
                out.append(r)
            return out
        self._run_async(job, on_done=self._on_install_batch_done)

    def _on_install_batch_done(self, rs):
        self._set_busy(False)
        n_ok = 0
        for r in rs or []:
            label = SIDE_LABELS.get(r.get("side", "?"), "?")
            if r["action"] == "installed":
                n_ok += 1
                self._log(f"已安装到{label}: {r['name']} v{r['version']}")
            elif r["action"] == "manual":
                self._log(f"{label} {r['name']}: {r.get('note') or '需手动下载'}")
                self._offer_open_download_page(r.get("entry"))
            elif r["action"] == "skipped_incompatible":
                self._log(f"[跳过] {label} {r['name']}: {r.get('note')}")
            else:
                self._log(f"[错误] {label} {r['name']}: {r.get('error')}")
                if r.get("warning"):
                    self._log(f"  [端别提示] {r['warning']}")
            if r.get("leftover"):
                self._log(f"[提示] {label} 旧版本文件被占用未能移除: {'、'.join(r['leftover'])}"
                          "（请关闭游戏/服务端后右键「清理重复jar」）")
        self.refresh_all()

    def _version_picker(self, options, current=None, title="选择版本"):
        """模态版本选择对话框：过滤、仅显示适配、预选推荐/已装版本。返回版本或 None。"""
        top = self._dialog(title, "640x500")
        gtnh = self.cfg.data.get("gtnh_version") or ""
        ttk.Label(top, text=f"你的整合包版本: {gtnh or '（未设置，设置页可填写以获得推荐标记）'}",
                  font=FONT).pack(anchor="w", padx=10, pady=(10, 2))
        bar = ttk.Frame(top)
        bar.pack(fill="x", padx=10)
        ttk.Label(bar, text="过滤:").pack(side="left")
        filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=filter_var, width=16).pack(side="left", padx=4)
        only_compat = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="仅显示适配", variable=only_compat).pack(side="left", padx=4)
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        lb = tk.Listbox(frame, width=70, height=12, font=FONT, exportselection=False)
        lb.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(lb, frame)
        detail = tk.Text(top, height=6, wrap="word", font=FONT, state="disabled")
        detail.pack(fill="x", padx=10, pady=4)

        def row_text(o):
            marks = []
            if o["recommended"]:
                marks.append("推荐")
            if o["latest"]:
                marks.append("最新")
            if o.get("prerelease"):
                marks.append("预发布")
            if o["compat"] == "incompatible":
                marks.append("不适配当前GTNH")
            if current and o["version"] == current:
                marks.append("已安装")
            pub = (o.get("published_at") or "")[:10]
            tag = f"  [{'/'.join(marks)}]" if marks else ""
            return f"{o['version']}{tag}" + (f"  {pub}" if pub else "")

        shown = []  # 与 Listbox 行一一对应的 options 子集

        def show_body(o):
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            body = (o.get("body") or "（该版本无更新日志）").strip()
            if o["compat"] == "incompatible":
                body = ("⚠ 按 wiki 兼容表可能与 GTNH " + gtnh
                        + " 不适配" + chr(10) * 2) + body
            detail.insert("1.0", body)
            detail.configure(state="disabled")

        def repopulate(*_a):
            nonlocal shown
            kw = filter_var.get().strip().lower()
            shown = [o for o in options
                     if (not only_compat.get() or o["compat"] != "incompatible")
                     and (not kw or kw in o["version"].lower()
                          or kw in (o.get("body") or "").lower())]
            lb.delete(0, "end")
            for o in shown:
                lb.insert("end", row_text(o))
            if not shown:
                detail.configure(state="normal")
                detail.delete("1.0", "end")
                detail.insert("1.0", "（没有符合过滤条件的版本）")
                detail.configure(state="disabled")
                return
            preselect()

        def on_sel(event):
            idx = lb.curselection()
            if idx:
                show_body(shown[idx[0]])
        lb.bind("<<ListboxSelect>>", on_sel)
        filter_var.trace_add("write", lambda *a: repopulate())
        only_compat.trace_add("write", lambda *a: repopulate())
        repopulate()

        def preselect():
            prefer = None
            if current:
                prefer = next((o for o in shown if o["version"] == current), None)
            if prefer is None:
                prefer = next((o for o in shown if o["recommended"]),
                              shown[0] if shown else None)
            if prefer is not None:
                i = shown.index(prefer)
                lb.selection_set(i)
                lb.see(i)
                show_body(prefer)
        preselect()

        result = {"version": None}

        def ok():
            idx = lb.curselection()
            if idx:
                result["version"] = shown[idx[0]]["version"]
            top.destroy()
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="使用选中版本", command=ok).pack(side="left", padx=2)
        ttk.Button(btns, text="取消", command=top.destroy).pack(side="left", padx=6)
        lb.bind("<Double-Button-1>", lambda e: ok())   # 双击即确认
        top.bind("<Return>", lambda e: ok())           # 回车即确认
        top.transient(self.root)
        top.grab_set()
        top.wait_window()
        return result["version"]

    def open_link_addable(self):
        sel = self._selected_addable()
        if not sel:
            return
        items = []
        for e in sel:
            url = (e["urls"] or {}).get("github") or (e["urls"] or {}).get("curseforge")
            if url:
                items.append((e["name_en"], url))
        self._open_link_items(items)

    def refresh_wiki(self):
        if self.busy:
            return
        self._set_busy(True)
        self._log("正在抓取 wiki 数据...")
        self._run_async(
            lambda: (wikimod.fetch_and_parse(self.cfg), ),
            on_done=lambda r: self._on_wiki_done(r))

    def _on_wiki_done(self, r):
        if r is None:
            self._set_busy(False)
            return
        mods, warnings = r[0]
        for w in warnings:
            self._log(f"[警告] {w}")
        try:
            before = {m["id"] for m in self.db.mods}
            changes = self.db.merge_wiki(mods)
        except Exception as e:
            self._log(f"[错误] {e}")
            messagebox.showerror("刷新Wiki失败", str(e))
            self._set_busy(False)
            return
        # 本次新增的 mod 高亮显示（查看详情后清除；下次刷新覆盖）
        self._new_ids = {m["id"] for m in self.db.mods} - before
        if self._new_ids:
            self._log(f"本次新增 {len(self._new_ids)} 个mod，已在可添加列表中高亮"
                      "（🆕 标记，查看详情后取消）")
        self._log(f"wiki 数据已更新，共 {len(self.db.wiki_mods())} 个mod，{len(changes)} 处变化")
        for c in changes[:30]:
            self._log(f"  - {c}")
        self.refresh_addable()
        # Wiki刷新不只更新条目，也同步刷新下载页最新版发布时间。
        self._log("正在更新下载页最新版发布时间...")
        self._run_async(
            lambda: updater.refresh_release_dates(self.cfg, self.db),
            on_done=self._on_release_dates_done)

    def _on_release_dates_done(self, result):
        self._set_busy(False)
        result = result or {}
        self._log(f"下载页发布时间已更新：{result.get('updated', 0)} 个，"
                  f"{result.get('failed', 0)} 个失败")
        self.refresh_addable()

    # ---------- 未受管页 ----------
    def _um_rows(self):
        return updater.unmatched_files(self.cfg, self.db)

    def _fmt_time(self, ts):
        from datetime import datetime
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            return "?"

    def refresh_unmanaged(self):
        if not hasattr(self, "um_tree"):
            return
        kw = getattr(self, "um_search", tk.StringVar()).get().strip().lower()
        keep_yview = self.um_tree.yview()
        self.um_tree.delete(*self.um_tree.get_children())
        self.um_rows = []
        for side in SIDES:
            for f in self._um_rows()[side]:
                if kw and kw not in f.file_name.lower():
                    continue
                iid = f"{side}|{f.file_name}"
                try:
                    st = f.path.stat()
                    mtime, size = self._fmt_time(st.st_mtime), f"{st.st_size // 1024}K"
                except OSError:
                    mtime, size = "?", "?"
                self.um_tree.insert("", "end", iid=iid,
                                    values=(SIDE_LABELS[side], f.file_name,
                                            f.version or "", mtime, size))
                self.um_rows.append((iid, side, f))
        self._set_status(f"未受管文件：{len(self.um_rows)} 个")
        self._reapply_sort(self.um_tree)
        if keep_yview[0] > 0:
            self.um_tree.yview_moveto(keep_yview[0])

    def _um_reveal(self, event):
        """双击未受管文件 → 资源管理器定位。"""
        iid = self.um_tree.identify_row(event.y)
        match = next(((s, f) for i, s, f in self.um_rows if i == iid), None)
        if match:
            self._reveal(match[1].path)

    def ignore_all_unmanaged(self):
        rows = self._um_rows()
        total = sum(len(v) for v in rows.values())
        if not total:
            messagebox.showinfo("提示", "没有未受管文件")
            return
        if not messagebox.askyesno("确认", f"忽略全部 {total} 个未受管文件？\n"
                                   "（GTNH核心mod建议忽略；可在「恢复已排除文件」中恢复显示）"):
            return
        for side in SIDES:
            for f in rows[side]:
                updater.ignore_unmanaged(self.cfg, f.file_name)
        self._log(f"已忽略全部 {total} 个未受管文件")
        self.refresh_unmanaged()

    def _selected_um(self):
        sel = self.um_tree.selection()
        return [(s, f) for i, s, f in self.um_rows if i in sel]

    def associate_unmanaged(self):
        sel = self._selected_um()
        if not sel:
            messagebox.showinfo("提示", "请先选中文件")
            return
        f_names = "、".join(f.file_name for _, f in sel)
        top = self._dialog("选择要关联的mod", "640x460")
        ttk.Label(top, text=f"将把 {f_names} 关联到选中的mod条目（可搜索）",
                  font=FONT).pack(anchor="w", padx=8, pady=(8, 2))
        bar = ttk.Frame(top)
        bar.pack(fill="x", padx=8)
        ttk.Label(bar, text="搜索:").pack(side="left")
        kw_var = tk.StringVar()
        ttk.Entry(bar, textvariable=kw_var, width=26).pack(side="left", padx=4)
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        lb = tk.Listbox(frame, width=72, height=15, font=FONT, exportselection=False)
        lb.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(lb, frame)
        entries = sorted(self.db.all(), key=lambda e: (e["name_en"] or e["id"]).lower())

        def key_of(e):
            return f"{e['name_en'] or e['id']} {e['name_cn'] or ''}".lower()

        shown = []

        def repopulate(*_a):
            nonlocal shown
            kw = kw_var.get().strip().lower()
            named = [(key_of(e), e) for e in entries]
            if kw:
                named = [(k, e) for k, e in named if kw in k]
            named.sort(key=lambda x: x[0])
            shown = [e for _, e in named]
            lb.delete(0, "end")
            for e in shown:
                lb.insert("end", f"{e['name_en'] or e['id']}（{e['name_cn']}）")
        kw_var.trace_add("write", lambda *a: repopulate())
        repopulate()

        # 按未受管文件名与条目英文名的包含关系预选最可能的条目
        first_file = (sel[0][1].name_part or sel[0][1].file_name).lower()
        for i, e in enumerate(shown):
            if e["name_en"] and len(e["name_en"]) >= 4 and e["name_en"].lower() in first_file:
                lb.selection_set(i)
                lb.see(i)
                break

        def ok():
            idx = lb.curselection()
            if not idx:
                return
            e = shown[idx[0]]
            for side, f in sel:
                updater.associate_unmanaged(self.db, e["id"], f.name_part or f.file_name)
            self._log(f"已关联到 {e['name_en'] or e['id']}，重新扫描后生效")
            top.destroy()
            self.refresh_unmanaged()
        btns = ttk.Frame(top)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="确认关联", command=ok).pack(side="left", padx=2)
        ttk.Button(btns, text="取消", command=top.destroy).pack(side="left", padx=6)

    def register_unmanaged(self):
        sel = self._selected_um()
        if not sel:
            messagebox.showinfo("提示", "请先选中文件")
            return
        names = (chr(10) + "").join("  " + f.file_name for _, f in sel)
        if not messagebox.askyesno(
                "注册为自定义mod",
                "注册 " + str(len(sel)) + " 个文件为自定义源？"
                "（端别默认双端，之后可在「自定义源」页修改）" + chr(10) + names):
            return
        for side, f in sel:
            eid = updater.register_unmanaged(self.cfg, self.db, side, f.file_name)
            self._log(f"已注册为自定义mod: {eid}（端别默认为双端，可在「自定义源」页修改）")
        self.refresh_unmanaged()
        self.refresh_custom()

    def ignore_unmanaged(self):
        sel = self._selected_um()
        if not sel:
            messagebox.showinfo("提示", "请先选中文件")
            return
        for side, f in sel:
            updater.ignore_unmanaged(self.cfg, f.file_name)
            self._log(f"已忽略 {f.file_name}")
        self.refresh_unmanaged()

    def restore_ignored(self):
        ignored = list(self.cfg.data.get("ignored_files") or [])
        if not ignored:
            messagebox.showinfo("提示", "没有被剔除/忽略的文件")
            return
        top = self._dialog("恢复已排除文件", "560x320")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        lb = tk.Listbox(frame, width=70, height=14, font=FONT, selectmode="extended")
        lb.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(lb, frame)
        for name in ignored:
            lb.insert("end", name)

        def ok():
            for idx in lb.curselection():
                updater.unignore(self.cfg, ignored[idx])
                self._log(f"已恢复显示: {ignored[idx]}")
            top.destroy()
            self.refresh_all()
        ttk.Button(top, text="恢复选中文件（重新显示）", command=ok).pack(pady=4)

    # ---------- 自定义源页 ----------
    def refresh_custom(self):
        if not hasattr(self, "cust_tree"):
            return
        self.cust_tree.delete(*self.cust_tree.get_children())
        self.cust_rows = []
        for e in self.db.custom_mods():
            src = e.get("source") or {}
            src_txt = (f"{src.get('owner', '')}/{src.get('repo', '')}" if e["source_type"] == "github"
                       else (e.get("urls") or {}).get("curseforge", "") if e["source_type"] == "curseforge"
                       else src.get("path", "") if e["source_type"] == "local_folder" else "")
            self.cust_tree.insert("", "end", iid=e["id"],
                                  values=(e["name_en"] or e["id"], e["name_cn"],
                                          SIDE_LABELS.get(e["side"], e["side"]),
                                          e["source_type"], src_txt))
            self.cust_rows.append(e)
        self._set_status(f"自定义源：{len(self.cust_rows)} 个")
        self._reapply_sort(self.cust_tree)

    def add_custom_dialog(self):
        self._custom_source_dialog()

    def edit_custom_dialog(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中条目")
            return
        e = self.db.get(sel[0])
        if e:
            self._custom_source_dialog(e)

    def _custom_source_dialog(self, existing=None):
        editing = existing is not None
        top = self._dialog("编辑自定义源" if editing else "添加自定义源", "500x360")
        f = ttk.Frame(top)
        f.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(f, text="类型:").grid(row=0, column=0, sticky="w", pady=4)
        initial_type = (existing or {}).get("source_type") or "github"
        kind = tk.StringVar(value=initial_type)
        ttk.Combobox(f, textvariable=kind, state="readonly", width=22,
                     values=("github", "curseforge", "local_folder", "manual")).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="英文名(匹配jar用):").grid(row=1, column=0, sticky="w", pady=4)
        en = ttk.Entry(f, width=30)
        en.insert(0, (existing or {}).get("name_en") or "")
        en.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="中文名(可选):").grid(row=2, column=0, sticky="w", pady=4)
        cn = ttk.Entry(f, width=30)
        cn.insert(0, (existing or {}).get("name_cn") or "")
        cn.grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="端别:").grid(row=3, column=0, sticky="w", pady=4)
        side = tk.StringVar(value=(existing or {}).get("side") or "both")
        ttk.Combobox(f, textvariable=side, state="readonly", width=22,
                     values=("client", "server", "both")).grid(row=3, column=1, sticky="w")
        old_src = (existing or {}).get("source") or {}
        old_urls = (existing or {}).get("urls") or {}
        src_label = ttk.Label(f, text="源:")
        src_label.grid(row=4, column=0, sticky="w", pady=4)
        src = ttk.Entry(f, width=40)
        src.grid(row=4, column=1, sticky="we")
        regex_label = ttk.Label(f, text="文件名匹配正则（可选）:")
        regex_entry = ttk.Entry(f, width=30)

        def browse_src():
            d = filedialog.askdirectory(title="选择本地目录")
            if d:
                src.delete(0, "end")
                src.insert(0, d)
        browse_btn = ttk.Button(f, text="浏览...", command=browse_src)
        browse_btn.grid(row=4, column=2)

        def on_kind(*_a):
            k = kind.get()
            is_local = k == "local_folder"
            is_manual = k == "manual"
            src_label.configure(text={"github": "GitHub owner/repo:",
                                      "curseforge": "CurseForge 页面链接:",
                                      "local_folder": "本地目录:",
                                      "manual": "（手动维护，无源）"}.get(k, "源:"))
            if is_local:
                browse_btn.grid()
                regex_label.grid()
                regex_entry.grid(row=5, column=1, sticky="w")
            else:
                browse_btn.grid_remove()
                regex_label.grid_remove()
                regex_entry.grid_remove()
            src.configure(state="disabled" if is_manual else "normal")

        kind.trace_add("write", lambda *a: on_kind())
        if initial_type == "github":
            src.insert(0, f"{old_src.get('owner', '')}/{old_src.get('repo', '')}")
        elif initial_type == "curseforge":
            src.insert(0, old_urls.get("curseforge") or "")
        elif initial_type == "local_folder":
            src.insert(0, old_src.get("path") or "")
            regex_entry.insert(0, old_src.get("name_regex") or "")
        on_kind()

        def ok():
            k = kind.get()
            name_en = en.get().strip()
            if not name_en:
                messagebox.showwarning("提示", "英文名不能为空", parent=top)
                return
            fields = {"name_en": name_en, "name_cn": cn.get().strip(), "side": side.get(),
                      "source_type": k, "source": {}, "urls": {
                          "github": None, "curseforge": None, "mcmod": None,
                          "bilibili": None, "other": [], "links": []},
                      "source_override": True}
            if k == "github":
                parts = src.get().strip().replace("https://github.com/", "").split("/")
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    messagebox.showwarning("提示", "GitHub源需填 owner/repo", parent=top)
                    return
                url = f"https://github.com/{parts[0]}/{parts[1]}"
                fields["source"] = {"owner": parts[0], "repo": parts[1],
                                     "asset_regex": "", "exclude_regex": wikimod.DEFAULT_EXCLUDE_REGEX}
                fields["urls"].update(github=url, links=[{"url": url, "label": "github"}])
            elif k == "curseforge":
                url = src.get().strip()
                if not url or "curseforge.com" not in url.lower():
                    messagebox.showwarning("提示", "CurseForge源需填写 curseforge.com 页面链接", parent=top)
                    return
                fields["urls"].update(curseforge=url, links=[{"url": url, "label": "curseforge"}])
            elif k == "local_folder":
                p = src.get().strip()
                if not p:
                    messagebox.showwarning("提示", "请填写本地目录路径", parent=top)
                    return
                fields["source"] = {"path": p, "name_regex": regex_entry.get().strip()}
            if editing:
                self.db.update_custom(existing["id"], fields)
                self._log(f"已编辑自定义源: {existing['id']}")
            else:
                # add_custom 通过 github_url/curseforge_url 写入链接
                if k == "github":
                    fields["github_url"] = fields["urls"]["github"]
                elif k == "curseforge":
                    fields["curseforge_url"] = fields["urls"]["curseforge"]
                self.db.add_custom(fields)
                self._log(f"已添加自定义源: {name_en}")
            top.destroy()
            self.refresh_all()

        ttk.Button(f, text="保存" if editing else "添加", command=ok).grid(row=5, column=1, sticky="w", pady=10)
        ttk.Button(f, text="取消", command=top.destroy).grid(row=5, column=1, sticky="e", pady=10)
        top.transient(self.root)
        top.grab_set()

    def remove_custom(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中条目")
            return
        e = self.db.get(sel[0])
        if e and messagebox.askyesno("确认", f"删除自定义源 {e['name_en']}？\n（不影响已安装的文件）"):
            self.db.remove_custom(e["id"])
            self.refresh_custom()

    # ---------- 设置页 ----------
    def _save_wiki_cookie_from_ui(self):
        self.cfg.set_wiki_cookie(self.wiki_cookie_entry.get().strip(),
                                 self.wiki_ua_entry.get().strip())

    def import_wiki_curl(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showerror("错误", "剪贴板为空，请先在浏览器 DevTools 里 Copy as cURL")
            return
        cookie, ua = cookies.parse_paste(text)
        if not cookie and not ua:
            messagebox.showerror(
                "错误", "未能从剪贴板解析出 Cookie/User-Agent。\n"
                        "请复制「Copy as cURL」内容，或直接粘贴 Cookie 请求头的值。")
            return
        self.wiki_cookie_entry.delete(0, "end")
        if cookie:
            self.wiki_cookie_entry.insert(0, cookie)
        self.wiki_ua_entry.delete(0, "end")
        if ua:
            self.wiki_ua_entry.insert(0, ua)
        self._save_wiki_cookie_from_ui()
        self._log(f"Wiki Cookie 已导入并保存"
                  f"（{len(cookie.split(';')) if cookie else 0} 个cookie，UA{'已' if ua else '未'}导入）")

    def clear_wiki_cookie(self):
        self.wiki_cookie_entry.delete(0, "end")
        self.wiki_ua_entry.delete(0, "end")
        self.cfg.set_wiki_cookie("", "")
        self._log("Wiki Cookie 已清除")

    def test_wiki_fetch(self):
        if self.busy:
            return
        self._save_wiki_cookie_from_ui()
        self._set_busy(True)
        self._log("正在测试 wiki 抓取...")
        self._run_async(lambda: (wikimod.fetch_and_parse(self.cfg), ),
                        on_done=lambda r: self._on_wiki_test_done(r))

    def _on_wiki_test_done(self, r):
        self._set_busy(False)
        if r is None:
            return
        mods, warnings = r[0]
        if mods:
            msg = f"抓取成功，解析到 {len(mods)} 个mod"
            self._log(f"[OK] {msg}" + (f"（警告：{'；'.join(warnings)}）" if warnings else ""))
            messagebox.showinfo("测试成功", msg)
        else:
            self._log("[失败] 抓取到内容但解析不到mod（Cookie 可能已过期，或页面结构变更）")
            messagebox.showwarning("测试失败", "抓取内容解析不到mod，请重新导入有效的 Cookie")

    def test_network_settings(self):
        """用输入框当前的 Token/代理请求 GitHub API，报告连通性与剩余配额。"""
        token = self.token_entry.get().strip()
        proxy = None
        p = self.proxy_entry.get().strip()
        if p:
            host, _, port = p.partition(":")
            try:
                proxy = {"host": host, "port": int(port or 8080)}
            except ValueError:
                messagebox.showerror("错误", "代理端口必须是数字")
                return
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.busy:
            return
        self._set_busy(True)
        self._log("测试 GitHub 连接...")

        def job():
            raw, _ = net.http_get("https://api.github.com/rate_limit",
                                  headers=headers, proxy=proxy, retries=0)
            return json.loads(raw).get("resources", {}).get("core", {})

        def done(core):
            self._set_busy(False)
            if core:
                messagebox.showinfo(
                    "连接成功",
                    "GitHub API 连接正常。" + chr(10)
                    + f"剩余配额: {core.get('remaining')}/{core.get('limit')}" + chr(10)
                    + ("已配置 Token" if token else "匿名访问（建议配置 Token 提高限额）"))
            else:
                messagebox.showwarning("响应异常", "连接成功但响应格式不符")
        self._run_async(job, on_done=done)

    def save_settings(self):
        self.cfg.set_mods_dir("client", self.client_entry.get().strip())
        self.cfg.set_mods_dir("server", self.server_entry.get().strip())
        for side, p in (("客户端", self.cfg.client_mods_dir),
                        ("服务端", self.cfg.server_mods_dir)):
            if p and not p.is_dir():
                if not messagebox.askyesno("确认",
                        f"{side} mods 目录不存在：\n{p}\n仍要保存吗？（之后可再改）"):
                    return
        self.cfg.data["github_token"] = self.token_entry.get().strip()
        p = self.proxy_entry.get().strip()
        if p:
            if ":" in p:
                host, _, port = p.partition(":")
                try:
                    self.cfg.data["proxy"] = {"host": host, "port": int(port or 8080)}
                except ValueError:
                    messagebox.showerror("错误", "代理端口必须是数字")
                    return
            else:
                self.cfg.data["proxy"] = {"host": p, "port": 8080}
        else:
            self.cfg.data["proxy"] = None
        try:
            self.cfg.data["check_interval_hours"] = float(self.interval_entry.get().strip() or 6)
            self.cfg.data["backup_keep"] = int(self.backup_entry.get().strip() or 3)
        except ValueError:
            messagebox.showerror("错误", "缓存时长/备份数必须是数字")
            return
        self.cfg.data["gtnh_version"] = self.gtnh_entry.get().strip()
        self._save_wiki_cookie_from_ui()
        self.cfg.save()
        self._log("设置已保存")
        messagebox.showinfo("已保存", "设置已保存，列表即将刷新")
        self.refresh_all()

    def _dirs_ok(self) -> bool:
        if not self.cfg.client_mods_dir and not self.cfg.server_mods_dir:
            messagebox.showerror("错误", "客户端/服务端 mods 目录均未设置，请先到「设置」页配置")
            return False
        return True

    def run(self):
        self.root.mainloop()


def run():
    data_dir = utils.resolve_data_dir()
    try:  # 高分屏 DPI 感知（Win 8.1+；失败不影响运行）
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        app = GuiApp(data_dir)
        app.run()
    except Exception:
        # pyw 无控制台：启动错误写入文件，方便排查
        try:
            import traceback
            err_file = data_dir / "logs" / "gui_error.log"
            err_file.parent.mkdir(parents=True, exist_ok=True)
            with open(err_file, "a", encoding="utf-8") as f:
                f.write(f"[{utils.now_str()}]\n{traceback.format_exc()}\n")
        except OSError:
            pass
        raise


if __name__ == "__main__":
    run()
