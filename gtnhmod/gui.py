"""Tkinter GUI 壳。

线程模型：所有网络/文件操作在后台线程执行，经 queue.Queue 把日志与
"刷新"事件推回主线程（root.after 轮询），避免 UI 冻结。Tk 控件只在主线程操作。

已安装页：一个 mod 一行，安装端别分三类（双端/仅客户端/仅服务端）。
"""
import os
import queue
import subprocess
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import SIDES, SIDE_LABELS, __version__, updater, utils
from . import wiki as wikimod
from .config import Config
from .db import ModsDB
from .installed import InstalledDB

STATUS_CN = {"installed": "已安装", "update_avail": "可更新", "disabled": "已禁用"}
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
        self.queue: queue.Queue = queue.Queue()
        self.busy = False

        self.root = tk.Tk()
        self.root.title(f"GTNH 额外MOD管理工具 v{__version__}")
        self.root.geometry("1120x720")
        self.root.minsize(980, 640)
        self._build_ui()
        self.root.after(100, self._poll_queue)
        # 启动时刷新全部页签（否则自定义源/未受管页首次是空的）
        self.refresh_installed()
        self.refresh_addable()
        self.refresh_unmanaged()
        self.refresh_custom()
        # 切换页签时自动刷新对应页
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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
        self.nb.add(tab2, text="可添加MOD列表")
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
        main_pane.add(bottom, stretch="never", minsize=110)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", side="top", padx=6, pady=(4, 0))
        log_bar = ttk.Frame(bottom)
        log_bar.pack(fill="x", padx=6)
        ttk.Label(log_bar, text="操作日志:").pack(side="left")
        ttk.Button(log_bar, text="清空", command=self.clear_log).pack(side="left", padx=4)
        ttk.Button(log_bar, text="打开日志文件", command=self._open_log_file).pack(side="left", padx=2)
        log_frame = ttk.Frame(bottom)
        log_frame.pack(fill="both", expand=True, padx=6)
        self.log = tk.Text(log_frame, height=8, font=("Consolas", 9),
                           state="disabled", wrap="word")
        self.log.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.log, log_frame)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, font=FONT,
                  anchor="w").pack(fill="x", padx=8, pady=(2, 4))
        self._log("就绪。首次使用请先到「设置」配置两端 mods 目录；mod 数据请点「可添加列表」页的刷新按钮获取。")

    def _attach_scrollbar(self, widget, parent):
        sb = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
        sb.pack(side="right", fill="y")
        widget.configure(yscrollcommand=sb.set)
        return sb

    # ---- 表头点击排序（通用，所有列表） ----
    def _make_sortable(self, tree, desc_first_cols=()):
        """表头点击按该列排序；再次点击反向。desc_first_cols 里的列首次点击为降序。"""

        def handler(col):
            if getattr(tree, "_sort_col", None) == col:
                reverse = not getattr(tree, "_sort_rev", False)
            else:
                reverse = col in desc_first_cols
            tree._sort_col, tree._sort_rev = col, reverse
            self._sort_tree(tree, col, reverse)

        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col: handler(c))

    def _sort_tree(self, tree, col, reverse):
        def key(iid):
            v = tree.set(iid, col)
            try:
                return (0, float(v))
            except ValueError:
                return (1, v.lower())
        iids = list(tree.get_children(""))
        iids.sort(key=key, reverse=reverse)
        for i, iid in enumerate(iids):
            tree.move(iid, "", i)

    def _reapply_sort(self, tree):
        if hasattr(tree, "_sort_col"):
            self._sort_tree(tree, tree._sort_col, tree._sort_rev)

    def _popup(self, tree, build, event):
        """右键菜单：自动选中目标行后弹出。"""
        iid = tree.identify_row(event.y)
        if iid and iid not in tree.selection():
            tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        build(menu, iid)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
        ttk.Label(bar, text="搜索:").pack(side="left", padx=(16, 2))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self.refresh_installed())
        ttk.Entry(bar, textvariable=self.search_var, width=22).pack(side="left")
        self.only_update = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="仅可更新", variable=self.only_update,
                        command=self.refresh_installed).pack(side="left", padx=(8, 0))

        cols = ("name", "cn", "side", "client", "server", "latest", "status")
        heads = ("名称", "中文名", "安装端别", "客户端", "服务端", "最新版本", "状态")
        widths = (230, 110, 80, 130, 130, 90, 70)
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6)
        self.inst_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                      selectmode="extended")
        for c, h, w in zip(cols, heads, widths):
            self.inst_tree.heading(c, text=h)
            self.inst_tree.column(c, width=w, anchor="w")
        self.inst_tree.tag_configure("upd", foreground=STATUS_COLORS["update_avail"])
        self.inst_tree.tag_configure("dis", foreground=STATUS_COLORS["disabled"])
        self.inst_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.inst_tree, tree_frame)
        self._make_sortable(self.inst_tree, desc_first_cols=("ver", "latest"))
        self.inst_tree.bind("<Double-1>", self._on_inst_double)
        self.inst_tree.bind("<Button-3>", lambda e: self._popup(self.inst_tree, self._inst_menu, e))

        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=4)
        self.btn_check = ttk.Button(btns, text="检查更新", command=self.check_updates)
        self.btn_update = ttk.Button(btns, text="更新选中", command=self.update_selected)
        self.btn_update_all = ttk.Button(btns, text="全部更新", command=self.update_all)
        self.btn_toggle = ttk.Button(btns, text="启用/禁用", command=self.toggle_selected)
        self.btn_lock = ttk.Button(btns, text="锁定/解锁", command=self.lock_selected)
        self.btn_open = ttk.Button(btns, text="打开下载页", command=self.open_link_selected)
        self.btn_exclude = ttk.Button(btns, text="从列表剔除", command=self.exclude_selected)
        self.btn_delete = ttk.Button(btns, text="删除mod", command=self.delete_selected)
        # 打开mods目录（下拉选择端别）
        self.btn_dirs = ttk.Menubutton(btns, text="打开mods目录")
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
        self.btn_dirs["menu"] = dir_menu
        for b in (self.btn_check, self.btn_update, self.btn_update_all, self.btn_toggle,
                  self.btn_lock, self.btn_open, self.btn_delete, self.btn_exclude, self.btn_dirs):
            b.pack(side="left", padx=2)
        self.busy_buttons = (self.btn_check, self.btn_update, self.btn_update_all,
                             self.btn_toggle, self.btn_lock, self.btn_open,
                             self.btn_delete, self.btn_exclude)

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
        self.add_search.trace_add("write", lambda *a: self.refresh_addable())
        ttk.Entry(bar, textvariable=self.add_search, width=22).pack(side="left", padx=(2, 8))
        self.btn_refresh_wiki = ttk.Button(bar, text="刷新Wiki数据", command=self.refresh_wiki)
        self.btn_refresh_wiki.pack(side="right")

        cols = ("name", "cn", "side", "cat", "installed", "desc")
        heads = ("名称", "中文名", "端别", "分类", "已安装", "简介")
        tree_frame = ttk.Frame(tab)
        tree_frame.pack(fill="both", expand=True, padx=6)
        self.add_tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                     selectmode="extended")
        for c, h, w in zip(cols, heads, (250, 110, 70, 90, 80, 380)):
            self.add_tree.heading(c, text=h)
            self.add_tree.column(c, width=w, anchor="w")
        self.add_tree.tag_configure("inst", foreground=STATUS_COLORS["update_avail"])
        self.add_tree.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(self.add_tree, tree_frame)
        self._make_sortable(self.add_tree)
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
        self.um_search.trace_add("write", lambda *a: self.refresh_unmanaged())
        ttk.Entry(bar, textvariable=self.um_search, width=26).pack(side="left", padx=(2, 0))
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
        self._make_sortable(self.um_tree, desc_first_cols=("mtime", "size"))
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
        self._make_sortable(self.cust_tree)
        self.cust_tree.bind("<Button-3>", lambda e: self._popup(self.cust_tree, self._cust_menu, e))
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=6, pady=4)
        ttk.Button(btns, text="添加", command=self.add_custom_dialog).pack(side="left", padx=2)
        ttk.Button(btns, text="编辑端别", command=self.edit_custom_side).pack(side="left", padx=2)
        ttk.Button(btns, text="删除", command=self.remove_custom).pack(side="left", padx=2)

    def _build_settings_tab(self, tab):
        f = ttk.Frame(tab)
        f.pack(fill="x", padx=12, pady=12)

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
        ttk.Label(f, text="GitHub Token（可选，提高API限额）").grid(row=2, column=0, sticky="w", pady=6)
        self.token_entry = ttk.Entry(f, width=60, show="*")
        self.token_entry.grid(row=2, column=1, sticky="we", padx=6)
        ttk.Label(f, text="代理地址（host:port，留空跟随系统）").grid(row=3, column=0, sticky="w", pady=6)
        self.proxy_entry = ttk.Entry(f, width=60)
        self.proxy_entry.grid(row=3, column=1, sticky="we", padx=6)
        ttk.Label(f, text="检查结果缓存（小时）").grid(row=4, column=0, sticky="w", pady=6)
        self.interval_entry = ttk.Entry(f, width=10)
        self.interval_entry.grid(row=4, column=1, sticky="w", padx=6)
        ttk.Label(f, text="每mod备份保留数").grid(row=5, column=0, sticky="w", pady=6)
        self.backup_entry = ttk.Entry(f, width=10)
        self.backup_entry.grid(row=5, column=1, sticky="w", padx=6)
        ttk.Label(f, text="GTNH整合包版本（预留）").grid(row=6, column=0, sticky="w", pady=6)
        self.gtnh_entry = ttk.Entry(f, width=10)
        self.gtnh_entry.grid(row=6, column=1, sticky="w", padx=6)
        ttk.Button(f, text="保存设置", command=self.save_settings).grid(row=7, column=1, sticky="w", pady=10)

        ttk.Button(f, text="打开操作日志", command=self._open_log_file).grid(row=7, column=1, padx=(110, 0), sticky="w", pady=10)
        f.columnconfigure(1, weight=1)

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
                elif kind == "_done":
                    if payload[0]:
                        payload[0](payload[1])
                elif kind == "_error":
                    msg = payload[1]
                    if payload[0]:
                        payload[0](None)
                    messagebox.showerror("操作失败", msg)
                    self._log(f"[错误] {msg}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def refresh_all(self):
        self.refresh_installed()
        self.refresh_addable()
        self.refresh_unmanaged()
        self.refresh_custom()

    # ---------- 已安装页 ----------
    def _inst_rows(self):
        """合并注册表 + 端别过滤 + 搜索 + 仅可更新过滤。"""
        merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
        filt = self.side_filter.get()
        kw = self.search_var.get().strip().lower()
        only_upd = self.only_update.get()
        rows = []
        for m in merged:
            if filt != "all" and m["install_side"] != filt:
                continue
            if only_upd and m["status"] != "update_avail":
                continue
            if kw and kw not in m["name_en"].lower() and kw not in (m["name_cn"] or "").lower():
                continue
            rows.append(m)
        return rows

    def refresh_installed(self):
        if not hasattr(self, "inst_tree"):
            return
        self.inst_tree.delete(*self.inst_tree.get_children())
        self.inst_rows = {}
        for m in self._inst_rows():
            lock = "🔒" if m["locked"] else ""
            dup = " ⚠重复jar" if m.get("duplicates") else ""
            tag = "upd" if m["status"] == "update_avail" else ("dis" if m["status"] == "disabled" else "")
            self.inst_tree.insert(
                "", "end", iid=m["mod_id"], tags=(tag,) if tag else (),
                values=(m["name_en"] + lock + dup, m["name_cn"],
                        INSTALL_SIDE_CN.get(m["install_side"], m["install_side"]),
                        side_state_text(m["sides"].get("client")),
                        side_state_text(m["sides"].get("server")),
                        m["latest_version"], STATUS_CN.get(m["status"], m["status"])))
            self.inst_rows[m["mod_id"]] = m
        # 底部状态栏
        merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
        client = self.cfg.client_mods_dir or "未设置"
        server = self.cfg.server_mods_dir or "未设置"
        self.status_var.set(f"客户端: {client}   服务端: {server}   "
                            f"已装受管mod: {len(merged)} 个"
                            f"（显示 {len(self.inst_rows)} 个）")
        self._reapply_sort(self.inst_tree)

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
        n_upd = 0
        reg = updater.build_registry(self.cfg, self.db, self.installed)
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
                n_upd += 1
                self._log(f"{SIDE_LABELS[side]} {name}: v{cur} → v{info.latest_version} 可更新")
            elif st == "uptodate":
                self._log(f"{SIDE_LABELS[side]} {name}: v{cur} 已最新")
            else:
                self._log(f"{SIDE_LABELS[side]} {name}: 当前v{cur}，最新v{info.latest_version}（请手动判断）")
            if info.candidates is None and info.latest_version:
                self._log(f"  （{name} 无自动下载资产，需手动下载）")
        self._log(f"检查完成：发现 {n_upd} 个可更新")
        self.refresh_installed()

    def update_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要更新的mod（Ctrl可多选）")
            return
        if self.busy:
            return
        if not messagebox.askyesno("确认", f"更新选中的 {len(sel)} 个mod（所有已装端别）？\n"
                                    "将逐个弹出版本选择（可取消跳过），旧版本自动备份到 data/backup。"):
            return
        self._start_update_flow(list(sel))

    def _start_update_flow(self, mods):
        """逐个处理：弹版本选择器 → 更新 → 下一个（单个mod失败不影响其他）。"""
        self._pending_updates = list(mods)
        self._update_results = []
        self._set_busy(True)
        self._process_next_update()

    def _process_next_update(self):
        if not self._pending_updates:
            self._finish_updates()
            return
        m = self._pending_updates.pop(0)
        entry = self.db.get(m["mod_id"]) or {}
        self._log(f"正在获取 {m['name_en']} 的可用版本列表...")
        self._run_async(
            lambda: updater.get_available_versions(entry, self.cfg, self.db, force=True),
            on_done=lambda opts: self._on_versions_for_update(opts, m))

    def _on_versions_for_update(self, options, m):
        if options is None:
            self._log(f"[错误] {m['name_en']}: 获取版本列表失败，已跳过")
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
        self._run_update_one(m, ver)

    def _run_update_one(self, m, ver):
        self._set_busy(True)

        def job():
            out = []
            for side in m["sides"]:
                try:
                    r = updater.update_mod(self.cfg, self.db, self.installed,
                                           m["mod_id"], side, version=ver)
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
        if not messagebox.askyesno("确认", "更新所有已安装且未锁定、启用的mod？\n旧版本会自动备份。"):
            return
        self._set_busy(True)
        self._run_async(
            lambda: updater.update_all(self.cfg, self.db, self.installed),
            on_done=lambda rs: self._on_update_done(rs or []))

    def _on_update_done(self, results):
        self._set_busy(False)
        n_ok = 0
        for r in results:
            name = r.get("name") or r.get("mod_id", "?")
            label = SIDE_LABELS.get(r.get("side", "?"), r.get("side", "?"))
            if r["action"] == "updated":
                n_ok += 1
                self._log(f"已更新 {label} {name}: v{r['from']} → v{r['to']}")
            elif r["action"] == "uptodate":
                self._log(f"{label} {name}: 已是最新")
            elif r["action"] == "manual":
                self._log(f"{label} {name}: {r.get('note') or '需手动下载'}")
            else:
                self._log(f"[错误] {label} {name}: {r.get('error')}")
        self._log(f"完成：成功更新 {n_ok} 个")
        self.refresh_all()

    def toggle_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要切换的mod")
            return
        for m in sel:
            self._toggle_mod(m)
        self.refresh_installed()

    def _toggle_mod(self, m):
        # 全部已装端别启用 → 全部禁用；否则全部启用（同步两端）
        want = not all(st["enabled"] for st in m["sides"].values())
        if not want and self.cfg.data.get("core_mod_confirm") \
                and not messagebox.askyesno("确认", f"禁用 {m['name_en']}？\n（影响端别：{INSTALL_SIDE_CN[m['install_side']]}）"):
            return
        for side in m["sides"]:
            r = updater.set_enabled(self.cfg, self.db, self.installed, m["mod_id"], side, want)
            if r["action"] in ("enabled", "disabled"):
                self._log(f"已{'启用' if want else '禁用'} {SIDE_LABELS[side]} {m['name_en']}")
            elif r["action"] != "unchanged":
                self._log(f"[错误] {SIDE_LABELS[side]} {m['name_en']}: {r.get('error')}")

    def lock_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中mod")
            return
        for m in sel:
            self._lock_mod(m)
        self.refresh_installed()

    def _lock_mod(self, m):
        new = not m["locked"]
        for side in m["sides"]:
            updater.set_lock(self.installed, m["mod_id"], side, new)
        self._log(f"{m['name_en']}: 已{'锁定' if new else '解锁'}（两端同步）")

    def exclude_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要剔除的mod")
            return
        names = "、".join(m["name_en"] for m in sel)
        if not messagebox.askyesno("确认", f"从受管列表剔除 {names}？\n"
                                    "（不会删除文件；可在「未受管MOD」页的“恢复已排除文件”中恢复显示）"):
            return
        for m in sel:
            self._exclude_mod(m)
        self.refresh_all()

    def _exclude_mod(self, m):
        files = updater.exclude_installed(self.cfg, self.db, self.installed, m["mod_id"])
        self._log(f"已剔除 {m['name_en']}: {', '.join(files) or '无文件'}")

    def delete_selected(self):
        sel = self._selected_inst()
        if not sel:
            messagebox.showinfo("提示", "请先选中要删除的mod")
            return
        for m in sel:
            self._delete_mod(m)
        self.refresh_all()

    def _delete_mod(self, m):
        if not messagebox.askyesno("确认删除", f"删除 {m['name_en']}？\n"
                                   f"（影响端别：{INSTALL_SIDE_CN[m['install_side']]}；\n"
                                   "jar 会移入 data/backup 备份目录并加 .deleted 后缀，可手动恢复）"):
            return
        r = updater.delete_mod(self.cfg, self.db, self.installed, m["mod_id"])
        if r["action"] == "deleted":
            for d in r["deleted"]:
                self._log(f"已删除 {d}")
        if r.get("error"):
            self._log(f"[错误] 删除 {m['name_en']}: {r['error']}")

    def open_link_selected(self):
        sel = self._selected_inst()
        for m in sel:
            self._open_link_mod(m)

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
        # 双击 → 在资源管理器中定位文件
        iid = self.inst_tree.identify_row(event.y)
        m = self.inst_rows.get(iid)
        if m:
            side = "client" if "client" in m["sides"] else next(iter(m["sides"]))
            self.reveal_mod(m, side)

    # ---------- 右键菜单 ----------
    def _inst_menu(self, menu, iid):
        m = self.inst_rows.get(iid)
        if not m:
            menu.add_command(label="（未选中mod）", state="disabled")
            return
        menu.add_command(label=f"检查更新: {m['name_en']}", command=lambda: self._check_single(m))
        menu.add_command(label="更新...", command=lambda: self._start_single_update(m))
        menu.add_command(label="启用/禁用", command=lambda: (self._toggle_mod(m), self.refresh_installed()))
        menu.add_command(label="锁定/解锁", command=lambda: (self._lock_mod(m), self.refresh_installed()))
        menu.add_command(label="打开下载页", command=lambda: self._open_link_mod(m))
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
        menu.add_command(label="删除mod...", command=lambda: (self._delete_mod(m), self.refresh_all()))
        menu.add_command(label="从列表剔除", command=lambda: (self._exclude_mod(m), self.refresh_all()))
        if m.get("duplicates"):
            menu.add_separator()
            menu.add_command(label=f"清理重复jar（{sum(len(v) for v in m['duplicates'].values())}个）",
                             command=lambda: self._cleanup_dups(m))

    def _cleanup_dups(self, m):
        if not messagebox.askyesno("确认", f"清理 {m['name_en']} 的重复jar？\n"
                                   "（保留版本最高者，其余备份到 data/backup 后移除）"):
            return
        r = updater.cleanup_duplicates(self.cfg, self.db, self.installed, m["mod_id"])
        if r["action"] == "cleaned":
            for c in r["cleaned"]:
                self._log(f"已清理重复jar {c}")
        else:
            self._log(f"{m['name_en']} 没有需要清理的重复jar")
        self.refresh_all()

    def _start_single_update(self, m):
        if self.busy:
            return
        if not messagebox.askyesno("确认", f"更新 {m['name_en']}（所有已装端别）？"):
            return
        self._start_update_flow([m])

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
        top = tk.Toplevel(self.root)
        top.title(f"编辑中文名 - {e['name_en']}")
        top.geometry("420x150")
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
            for e in sel:
                sides, note = updater.auto_install_sides(e, self.cfg)
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
                else:
                    self._log(f"[错误] {label} {r['name']}: {r.get('error')}")
            self._log(f"批量安装完成：成功 {n_ok} 个")
            self.refresh_all()
        self._run_async(job, on_done=done)

    def _bind_source_dialog(self, e):
        """选择该mod的一个下载链接绑定为下载源（检查更新/下载用它）。"""
        cand = [l for l in updater.entry_links(e)
                if "github.com" in l["url"] or "curseforge.com" in l["url"]]
        if not cand:
            if messagebox.askyesno("链接列表缺失",
                                   "该mod的下载链接列表不完整（可能是旧版工具保存的数据）。\n"
                                   "是否现在刷新Wiki数据以获取完整链接？"):
                self.refresh_wiki()
            return
        cur = updater.current_source_url(e)
        top = tk.Toplevel(self.root)
        top.title(f"选择下载源 - {e['name_en']}")
        top.geometry("640x360")
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
        menu.add_command(label="编辑中文名...", command=lambda: self._edit_name_cn(e))
        menu.add_command(label="编辑端别", command=self.edit_custom_side)
        menu.add_command(label="删除", command=self.remove_custom)

    # ---------- 可添加页 ----------
    def _installed_marks(self) -> dict:
        """{mod_id: install_side}，用于标记可添加列表中已安装的mod。"""
        marks = {}
        for m in updater.build_merged_registry(self.cfg, self.db, self.installed):
            marks[m["mod_id"]] = m["install_side"]
        return marks

    def refresh_addable(self):
        if not hasattr(self, "add_tree"):
            return
        entries = self.db.wiki_mods()
        groups = {}
        for e in entries:
            groups.setdefault(f"{e['group']}/{e['category']}", []).append(e)
        cats = sorted(groups, key=lambda k: (k.split("/")[0] != "星门规则", k))
        self._addable_cats = cats
        if not self.cat_var.get() or self.cat_var.get() not in cats:
            if cats:
                self.cat_var.set(cats[0])
        self.cat_combo["values"] = cats
        self.add_tree.delete(*self.add_tree.get_children())
        cur = self.cat_var.get()
        kw = self.add_search.get().strip().lower()
        marks = self._installed_marks()
        self.add_rows = {}
        for e in sorted(groups.get(cur, []), key=lambda x: x["id"]):
            if kw and kw not in e["name_en"].lower() and kw not in (e["name_cn"] or "").lower():
                continue
            if self.only_uninstalled.get() and e["id"] in marks:
                continue
            side_txt = SIDE_LABELS.get(e["side"], e["side"]) + ("?" if e["side_uncertain"] else "")
            installed_txt = INSTALL_SIDE_CN.get(marks.get(e["id"]), "")
            tag = "inst" if e["id"] in marks else ""
            name_txt = ("✓ " + e["name_en"]) if e["id"] in marks else e["name_en"]
            self.add_tree.insert("", "end", iid=e["id"], tags=(tag,) if tag else (),
                                 values=(name_txt, e["name_cn"], side_txt,
                                         e["category"], installed_txt, e["desc"][:60]))
            self.add_rows[e["id"]] = e
        self._reapply_sort(self.add_tree)

    def _selected_addable(self):
        sel = self.add_tree.selection()
        return [self.add_rows[i] for i in sel if i in self.add_rows]

    def show_addable_detail(self):
        sel = self._selected_addable()
        if not sel:
            return
        e = sel[0]
        marks = self._installed_marks()
        installed_txt = INSTALL_SIDE_CN.get(marks.get(e["id"]), "未安装")
        top = tk.Toplevel(self.root)
        top.title(f"{e['name_en']} {e['name_cn']}")
        top.geometry("680x460")
        src = updater.current_source_url(e)
        text = (f"名称: {e['name_en']} {e['name_cn']}\n"
                f"分类: {e['group']} / {e['category']}\n"
                f"端别: {SIDE_LABELS.get(e['side'], e['side'])}"
                + ("（wiki标注不确定）" if e["side_uncertain"] else "") + "\n"
                f"已安装: {installed_txt}\n"
                f"下载源: {src or '（默认第一个GitHub链接）'}\n\n"
                + (e["desc"] + "\n\n" if e["desc"] else "")
                + "详细信息:\n" + e["detail"])
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        t = tk.Text(frame, wrap="word", font=FONT)
        t.insert("1.0", text)
        t.configure(state="disabled")
        t.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(t, frame)

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
            lambda: updater.get_available_versions(e, self.cfg, self.db, force=True),
            on_done=lambda opts: self._on_versions_for_install(opts, e, sides, note))

    def _on_versions_for_install(self, options, e, sides, note):
        self._set_busy(False)
        if options is None:
            return
        if not options:
            # manual/curseforge 源无版本列表 → 直接原流程
            self._run_install(e, sides, None, note)
            return
        ver = self._version_picker(options, current=None,
                                   title=f"选择要安装的版本 - {e['name_en']}")
        if ver is None:
            return
        self._run_install(e, sides, ver, note)

    def _run_install(self, e, sides, ver, note=""):
        self._set_busy(True)
        self._log(f"开始安装 {e['name_en']}（{ver or '最新'}）→ "
                  + "、".join(SIDE_LABELS[s] for s in sides) + "...")
        if note:
            self._log(f"  {note}")

        def job():
            out = []
            for side in sides:
                r = updater.install_mod(self.cfg, self.db, self.installed, e["id"], side,
                                        version=ver)
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
            else:
                self._log(f"[错误] {label} {r['name']}: {r.get('error')}")
                if r.get("warning"):
                    self._log(f"  [端别提示] {r['warning']}")
        self.refresh_all()

    def _version_picker(self, options, current=None, title="选择版本"):
        """模态版本选择对话框。返回版本字符串或 None（取消）。"""
        top = tk.Toplevel(self.root)
        top.title(title)
        top.geometry("560x440")
        gtnh = self.cfg.data.get("gtnh_version") or ""
        ttk.Label(top, text=f"你的整合包版本: {gtnh or '（未设置，设置页可填写以获得推荐标记）'}",
                  font=FONT).pack(anchor="w", padx=10, pady=(10, 2))
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        lb = tk.Listbox(frame, width=64, height=12, font=FONT)
        lb.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(lb, frame)
        for o in options:
            marks = []
            if o["recommended"]:
                marks.append("推荐")
            if o["latest"]:
                marks.append("最新")
            if o["compat"] == "incompatible":
                marks.append("不适配当前GTNH")
            if current and o["version"] == current:
                marks.append("已安装")
            tag = f"  [{'/'.join(marks)}]" if marks else ""
            lb.insert("end", f"{o['version']}{tag}")
        # 选中版本时显示更新日志
        detail = tk.Text(top, height=6, wrap="word", font=FONT, state="disabled")
        detail.pack(fill="x", padx=10, pady=4)

        def on_sel(event):
            idx = lb.curselection()
            if not idx:
                return
            o = options[idx[0]]
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("1.0", o.get("body") or "（该版本无更新日志）")
            detail.configure(state="disabled")
        lb.bind("<<ListboxSelect>>", on_sel)
        result = {"version": None}

        def ok():
            idx = lb.curselection()
            if idx:
                result["version"] = options[idx[0]]["version"]
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
        for e in sel:
            url = (e["urls"] or {}).get("github") or (e["urls"] or {}).get("curseforge")
            if url:
                webbrowser.open(url)
                return

    def refresh_wiki(self):
        if self.busy:
            return
        self._set_busy(True)
        self._log("正在抓取 wiki 数据...")
        self._run_async(
            lambda: (wikimod.fetch_and_parse(self.cfg), ),
            on_done=lambda r: self._on_wiki_done(r))

    def _on_wiki_done(self, r):
        self._set_busy(False)
        if r is None:
            return
        mods, warnings = r[0]
        for w in warnings:
            self._log(f"[警告] {w}")
        changes = self.db.merge_wiki(mods)
        self._log(f"wiki 数据已更新，共 {len(self.db.wiki_mods())} 个mod，{len(changes)} 处变化")
        for c in changes[:30]:
            self._log(f"  - {c}")
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
        self._reapply_sort(self.um_tree)

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
        entries = sorted(self.db.all(), key=lambda e: e["id"])
        top = tk.Toplevel(self.root)
        top.title("选择要关联的mod")
        frame = ttk.Frame(top)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        lb = tk.Listbox(frame, width=60, height=16, font=FONT)
        lb.pack(side="left", fill="both", expand=True)
        self._attach_scrollbar(lb, frame)
        for e in entries:
            lb.insert("end", f"{e['name_en'] or e['id']}（{e['name_cn']}）")

        def ok():
            idx = lb.curselection()
            if not idx:
                return
            e = entries[idx[0]]
            for side, f in sel:
                updater.associate_unmanaged(self.db, e["id"], f.name_part or f.file_name)
            self._log(f"已关联到 {e['name_en'] or e['id']}，重新扫描后生效")
            top.destroy()
            self.refresh_unmanaged()
        ttk.Button(top, text="确认关联", command=ok).pack(pady=4)

    def register_unmanaged(self):
        sel = self._selected_um()
        if not sel:
            messagebox.showinfo("提示", "请先选中文件")
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
        top = tk.Toplevel(self.root)
        top.title("恢复已排除文件")
        top.geometry("560x320")
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
                       else src.get("path", "") if e["source_type"] == "local_folder" else "")
            self.cust_tree.insert("", "end", iid=e["id"],
                                  values=(e["name_en"] or e["id"], e["name_cn"],
                                          SIDE_LABELS.get(e["side"], e["side"]),
                                          e["source_type"], src_txt))
            self.cust_rows.append(e)
        self._reapply_sort(self.cust_tree)

    def add_custom_dialog(self):
        top = tk.Toplevel(self.root)
        top.title("添加自定义源")
        top.geometry("460x330")
        f = ttk.Frame(top)
        f.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(f, text="类型:").grid(row=0, column=0, sticky="w", pady=4)
        kind = tk.StringVar(value="github")
        ttk.Combobox(f, textvariable=kind, state="readonly", width=22,
                     values=("github", "local_folder", "manual")).grid(row=0, column=1, sticky="w")
        ttk.Label(f, text="英文名(匹配jar用):").grid(row=1, column=0, sticky="w", pady=4)
        en = ttk.Entry(f, width=30)
        en.grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="中文名(可选):").grid(row=2, column=0, sticky="w", pady=4)
        cn = ttk.Entry(f, width=30)
        cn.grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="端别:").grid(row=3, column=0, sticky="w", pady=4)
        side = tk.StringVar(value="both")
        ttk.Combobox(f, textvariable=side, state="readonly", width=22,
                     values=("client", "server", "both")).grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="GitHub owner/repo 或 本地目录:").grid(row=4, column=0, sticky="w", pady=4)
        src = ttk.Entry(f, width=40)
        src.grid(row=4, column=1, sticky="w")

        def ok():
            k = kind.get()
            name_en = en.get().strip()
            if not name_en:
                messagebox.showwarning("提示", "英文名不能为空", parent=top)
                return
            if k == "github":
                parts = src.get().strip().replace("https://github.com/", "").split("/")
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    messagebox.showwarning("提示", "GitHub源需填 owner/repo", parent=top)
                    return
                entry = {"name_en": name_en, "name_cn": cn.get().strip(), "side": side.get(),
                         "source_type": "github",
                         "source": {"owner": parts[0], "repo": parts[1], "asset_regex": "",
                                    "exclude_regex": wikimod.DEFAULT_EXCLUDE_REGEX}}
            elif k == "local_folder":
                p = src.get().strip()
                if not p:
                    messagebox.showwarning("提示", "请填写本地目录路径", parent=top)
                    return
                entry = {"name_en": name_en, "name_cn": cn.get().strip(), "side": side.get(),
                         "source_type": "local_folder",
                         "source": {"path": p, "name_regex": ""}}
            else:
                entry = {"name_en": name_en, "name_cn": cn.get().strip(), "side": side.get(),
                         "source_type": "manual"}
            eid = self.db.add_custom(entry)
            self._log(f"已添加自定义源: {eid}")
            top.destroy()
            self.refresh_custom()
        ttk.Button(f, text="添加", command=ok).grid(row=5, column=1, sticky="w", pady=10)

    def edit_custom_side(self):
        sel = self.cust_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中条目")
            return
        e = self.db.get(sel[0])
        if not e:
            return
        top = tk.Toplevel(self.root)
        top.title(f"编辑端别: {e['name_en']}")
        side = tk.StringVar(value=e.get("side") or "both")
        ttk.Radiobutton(top, text="客户端", variable=side, value="client").pack(anchor="w", padx=20)
        ttk.Radiobutton(top, text="服务端", variable=side, value="server").pack(anchor="w", padx=20)
        ttk.Radiobutton(top, text="双端", variable=side, value="both").pack(anchor="w", padx=20)

        def ok():
            self.db.update_custom(e["id"], {"side": side.get()})
            top.destroy()
            self.refresh_custom()
        ttk.Button(top, text="保存", command=ok).pack(pady=8)

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
    def save_settings(self):
        self.cfg.set_mods_dir("client", self.client_entry.get().strip())
        self.cfg.set_mods_dir("server", self.server_entry.get().strip())
        self.cfg.data["github_token"] = self.token_entry.get().strip()
        p = self.proxy_entry.get().strip()
        if p:
            if ":" in p:
                host, _, port = p.partition(":")
                self.cfg.data["proxy"] = {"host": host, "port": int(port or 8080)}
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
