"""资源包与光影的四页管理界面；业务行为由 packs 提供。"""
from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from . import packs as packmod, packs_wiki as packwiki, packs_browser, wiki, wiki_browser
from .mod_icons import allowed_icon_url

KIND_NAMES = {'all': '全部', **packwiki.KIND_LABELS}


class PacksGuiMixin:
    def _build_packs_tab(self, tab):
        self._pack_view = None
        self._pack_auto_sync = False
        self._pack_checked_updates = None
        self._pack_check_status = {}
        self._pack_table_rows = {}
        self._pack_action_buttons = []
        top = ttk.Frame(tab)
        top.pack(fill='x', padx=6, pady=4)
        ttk.Label(top, text='类型:').pack(side='left')
        self.pack_kind = tk.StringVar(value='all')
        self.pack_kind_label = tk.StringVar(value='全部')
        combo = ttk.Combobox(top, textvariable=self.pack_kind_label, state='readonly', width=9,
                             values=tuple(KIND_NAMES.values()))
        combo.pack(side='left', padx=(2, 10))
        def choose_kind(_event):
            self.pack_kind.set(next(k for k, v in KIND_NAMES.items() if v == self.pack_kind_label.get()))
            self.refresh_packs()
        combo.bind('<<ComboboxSelected>>', choose_kind)
        ttk.Label(top, text='分类:').pack(side='left')
        self.pack_cat = tk.StringVar()
        self.pack_cat_combo = ttk.Combobox(top, textvariable=self.pack_cat, state='readonly', width=18)
        self.pack_cat_combo.pack(side='left', padx=(2, 10))
        self.pack_cat_combo.bind('<<ComboboxSelected>>', lambda _e: self.refresh_packs())
        ttk.Label(top, text='搜索:').pack(side='left')
        self.pack_search = tk.StringVar()
        self.pack_search.trace_add('write', lambda *_: self._debounce_pack_search())
        self.pack_search_entry = ttk.Entry(top, textvariable=self.pack_search, width=23)
        self.pack_search_entry.pack(side='left', padx=(2, 8))
        self._wrap_bar(top)
        self.pack_dir_label = ttk.Label(tab, style='Muted.TLabel', wraplength=1100)
        self.pack_dir_label.pack(side='bottom', fill='x', padx=8, pady=(0, 4))
        self._auto_wraplength(self.pack_dir_label)
        self.pack_nb = ttk.Notebook(tab)
        self.pack_nb.pack(fill='both', expand=True)
        pages = [ttk.Frame(self.pack_nb) for _ in range(4)]
        for page, label in zip(pages, ('已安装', '可添加', '未受管', '自定义源')):
            self.pack_nb.add(page, text=label)
        self.pack_nb.bind('<<NotebookTabChanged>>', lambda _e: self.refresh_packs())

        bar = ttk.Frame(pages[0])
        bar.pack(fill='x', padx=6, pady=4)
        self._pack_button(bar, '刷新', self.refresh_packs)
        self._pack_button(bar, '检查更新', self.check_pack_updates)
        self.btn_pack_update = self._pack_button(bar, '更新选中', self.update_selected_packs)
        self.btn_pack_update_all = self._pack_button(bar, '全部更新', self.update_all_packs)
        self._pack_button(bar, '启用文件', lambda: self._change_pack_enabled(True))
        self._pack_button(bar, '禁用文件', lambda: self._change_pack_enabled(False))
        self._pack_button(bar, '锁定/解锁', self._toggle_pack_lock)
        self._pack_button(bar, '卸载', self.uninstall_selected_packs)
        self._pack_button(bar, '备份与恢复', self.pack_backup_dialog)
        self._pack_button(bar, '打开目录', self.open_pack_folder)
        self._wrap_bar(bar)
        ttk.Label(pages[0], text='这里显示已安装文件；资源包和光影是否实际启用，请在游戏设置中选择。',
                  style='Muted.TLabel').pack(anchor='w', padx=9, pady=(0, 4))
        self.pack_inst_tree = self._make_pack_tree(pages[0], 'packs-installed',
            ('name', 'kind', 'version', 'time', 'source', 'status', 'file'),
            ('名称', '类型', '本地版本', '本地更新时间', '下载源', '状态', '文件名'),
            (240, 75, 100, 135, 105, 195, 240))
        self.pack_inst_tree.bind('<Double-1>', lambda _e: self.reveal_selected_pack())

        nav = ttk.Frame(pages[1])
        nav.pack(fill='x', padx=6, pady=4)
        self.pack_view_mode = tk.StringVar(value='wiki')
        for value, label in (('wiki', 'Wiki 原网页'), ('offline', '离线目录')):
            ttk.Radiobutton(nav, text=label, value=value, variable=self.pack_view_mode,
                            command=self._show_pack_view).pack(side='left', padx=(0, 8))
        for label, action in (('后退', 'back'), ('返回资源包与光影', 'home'), ('刷新网页', 'reload')):
            self._pack_button(nav, label, lambda a=action: self._navigate_pack_wiki(a))
        self._pack_button(nav, '在浏览器打开', lambda: webbrowser.open(wiki._referer(self.cfg, packwiki.PACKS_PROFILE)))
        self._pack_button(nav, '同步管理目录', self.refresh_packs_wiki, side='right')
        self._wrap_bar(nav)
        self.pack_wiki_status = tk.StringVar(value='保留 Wiki 原站分类与说明，安装和更新由工具处理。')
        status = ttk.Label(pages[1], textvariable=self.pack_wiki_status, style='Muted.TLabel',
                           wraplength=1100)
        status.pack(fill='x', padx=8, pady=(0, 4))
        self._auto_wraplength(status)
        self.pack_content = ttk.Frame(pages[1])
        self.pack_content.pack(fill='both', expand=True)
        self.pack_wiki_frame = ttk.Frame(self.pack_content)
        self.pack_wiki_frame.pack(fill='both', expand=True)
        ttk.Label(self.pack_wiki_frame, text='正在加载 Wiki 原网页…\n网络不可用时可切换「离线目录」。',
                  anchor='center', justify='center').pack(fill='both', expand=True)
        self.pack_wiki_frame.bind('<Map>', lambda _e: self._ensure_pack_view())
        self.pack_wiki_frame.bind('<Configure>', self._resize_pack_view)
        self.pack_offline_frame = ttk.Frame(self.pack_content)
        options = ttk.Frame(self.pack_offline_frame)
        options.pack(fill='x', padx=6, pady=4)
        self.pack_only_installed = tk.BooleanVar(value=False)
        self.pack_only_uninstalled = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text='仅未安装', variable=self.pack_only_uninstalled,
                        command=self.refresh_packs).pack(side='right')
        self.pack_tree = self._make_pack_tree(self.pack_offline_frame, 'packs',
            ('name', 'author', 'cat', 'source', 'installed', 'ver', 'status', 'desc'),
            ('名称', '作者/维护者', '分类', '下载源', '已安装', '本地版本', '状态', '说明'),
            (230, 100, 110, 110, 65, 95, 170, 300))
        bar = ttk.Frame(self.pack_offline_frame)
        bar.pack(fill='x', padx=6, pady=4)
        for label, action in (('详情', self.show_pack_detail), ('安装', self.install_selected_packs),
                              ('打开下载页面', self.open_pack_page), ('重新扫描', self.refresh_packs)):
            self._pack_button(bar, label, action)
        self._wrap_bar(bar)

        ttk.Label(pages[2], text='以下包尚未关联管理目录，可关联已有条目或登记为自定义包。',
                  style='Muted.TLabel').pack(anchor='w', padx=8, pady=5)
        self.pack_um_tree = self._make_pack_tree(pages[2], 'packs-unmanaged',
            ('kind', 'file', 'version', 'time', 'status'),
            ('类型', '文件名', '识别版本', '修改时间', '状态'), (85, 470, 130, 160, 120))
        self.pack_um_tree.bind('<Double-1>', lambda _e: self._selected_unmanaged_packs()
                               and self._reveal(self._selected_unmanaged_packs()[0][1].path))
        bar = ttk.Frame(pages[2])
        bar.pack(fill='x', padx=6, pady=4)
        for label, action in (('关联已有条目', self.associate_selected_pack),
                              ('登记为自定义包', self.register_unmanaged_pack),
                              ('忽略选中', self.ignore_selected_packs),
                              ('恢复忽略', self.restore_ignored_packs), ('重新扫描', self.refresh_packs)):
            self._pack_button(bar, label, action)
        self._wrap_bar(bar)
        self.pack_custom_tree = self._make_pack_tree(pages[3], 'packs-custom',
            ('name', 'author', 'cat', 'source', 'installed', 'ver', 'status', 'desc'),
            ('名称', '作者/维护者', '分类', '下载源', '已安装', '本地版本', '状态', '说明'),
            (230, 100, 110, 110, 65, 95, 170, 300))
        bar = ttk.Frame(pages[3])
        bar.pack(fill='x', padx=6, pady=4)
        for label, action in (('添加', lambda: self._register_one_pack(self._pack_kind() or 'resourcepack', None)),
                              ('设置下载源', self.edit_selected_pack_source),
                              ('移除自定义条目', self.remove_selected_custom_packs),
                              ('详情', self.show_pack_detail), ('安装', self.install_selected_packs)):
            self._pack_button(bar, label, action)
        self._wrap_bar(bar)
        self.pack_rows, self.pack_inst_rows, self.pack_custom_rows, self.pack_um_rows = [], [], [], {}
        self._sync_pack_update_buttons()

    def _pack_button(self, parent, text, command, side='left'):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(side=side, padx=2)
        self._pack_action_buttons.append(button)
        return button

    def _make_pack_tree(self, parent, view_key, cols, heads, widths):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True, padx=6)
        tree = ttk.Treeview(frame, columns=cols, selectmode='extended')
        self._enable_mod_icons(tree)
        for col, label, width in zip(cols, heads, widths):
            tree.heading(col, text=label)
            tree.column(col, width=width, anchor='w')
        self.theme.register_tree(tree, {
            'inst': {'foreground': 'installed'}, 'upd': {'foreground': 'warn'},
            'manual': {'foreground': 'warn'}, 'local': {'foreground': 'disabled'},
            'disabled': {'foreground': 'disabled'}, 'error': {'foreground': 'error'},
            'odd': {'background': 'odd'}})
        tree.pack(side='left', fill='both', expand=True)
        self._attach_scrollbar(tree, frame)
        tree._view_key = view_key
        self._make_sortable(tree)
        self._restore_view(tree)
        tree.bind('<Return>', lambda _e: self.show_pack_detail())
        tree.bind('<Double-1>', lambda _e: self.show_pack_detail())
        tree.bind('<Button-3>', lambda e: self._popup(tree, self._pack_menu, e))
        return tree

    def _debounce_pack_search(self):
        self._debounce('_pack_search_after', self.refresh_packs)

    def _pack_kind(self):
        value = self.pack_kind.get()
        return value if value in packmod.DIR_NAMES else None

    def _filtered_pack_entries(self, entries):
        kind, cat, kw = self._pack_kind(), self.pack_cat.get(), self.pack_search.get().strip().lower()
        return [e for e in entries if (not kind or e.get('kind') == kind)
                and (not cat or e.get('category') == cat)
                and (not kw or kw in ' '.join(str(e.get(k) or '') for k in
                     ('id', 'name_en', 'name_cn', 'author', 'desc', 'category')).lower())]

    def _sorted_pack_entries(self):
        inst = packmod.installed_index(self.packs)
        return [e for e in self._filtered_pack_entries(self.packs.by_kind())
                if not e.get('custom')
                and (not self.pack_only_installed.get() or (e['kind'], e['id']) in inst)
                and (not self.pack_only_uninstalled.get() or (e['kind'], e['id']) not in inst)]

    def _pack_source_text(self, entry):
        if entry.get('local'):
            return '整合包自带'
        return ('自动下载(已绑定)' if entry.get('bound_source') else '自动下载') if packmod.entry_auto_url(entry) else '手动下载'

    def _pack_status_text(self, entry, rec):
        if rec:
            folder = packmod.resolve_pack_dir(self.cfg, entry['kind'])
            if not folder or not folder.is_dir():
                return '目录暂不可用，保留记录'
            if not rec.get('enabled', True):
                return '文件已禁用' + (' · 已锁定' if rec.get('locked') else '')
            if rec.get('locked'):
                return '已锁定更新'
            if rec.get('duplicates'):
                return '多个本地副本，需核对'
            key = entry['kind'], entry['id']
            checked = self._pack_check_status.get(key)
            if checked:
                return checked['detail']
            if key in self._pack_updates:
                return f'可更新 → {self._pack_updates[key]}'
            return '已安装 · 尚未检查更新'
        return '整合包自带，游戏内启用' if entry.get('local') else '未安装'

    def _pack_icon(self, tree, iid, entry, rec):
        folder = packmod.resolve_pack_dir(self.cfg, entry.get('kind'))
        name = rec.get('file_name')
        paths = [folder / name] if folder and name and packmod._safe_component(name) else []
        self._set_mod_icon(tree, iid, entry, paths)

    def _render_pack_table(self, tree, entries, installed, *, installed_view=False):
        keep, scroll = set(tree.selection()), tree.yview()
        self._clear_mod_icon_targets(tree)
        tree.delete(*tree.get_children())
        self._pack_table_rows[tree] = entries
        for i, entry in enumerate(entries):
            rec = installed.get((entry['kind'], entry['id'])) or {}
            status = self._pack_status_text(entry, rec)
            name = entry.get('name_en') or entry['id']
            if entry.get('name_cn'):
                name = f"{entry['name_cn']} ({name})"
            tag = 'upd' if (entry['kind'], entry['id']) in self._pack_updates else 'inst' if rec else 'local' if entry.get('local') else ''
            if rec and not rec.get('enabled', True):
                tag = 'disabled'
            if '失败' in status or '不可用' in status:
                tag = 'error'
            if installed_view:
                folder = packmod.resolve_pack_dir(self.cfg, entry['kind'])
                try:
                    updated = self._fmt_time((folder / rec['file_name']).stat().st_mtime) if folder else '—'
                except OSError:
                    updated = '—'
                values = (name, packwiki.KIND_LABELS[entry['kind']], rec.get('version') or '版本未知',
                          updated, self._pack_source_text(entry), status, rec.get('file_name') or '')
            else:
                if self._pack_kind() is None:
                    name = f"[{packwiki.KIND_LABELS[entry['kind']]}] {name}"
                values = (name, entry.get('author') or '', entry.get('category') or '',
                          self._pack_source_text(entry), '是' if rec else '否', rec.get('version') or '—',
                          status, (entry.get('desc') or '').replace('\n', ' ')[:120])
            tree.insert('', 'end', iid=entry['id'], values=values,
                        tags=tuple(x for x in (tag, 'odd' if i % 2 else '') if x))
            self._pack_icon(tree, entry['id'], entry, rec)
        self._reapply_sort(tree)
        selected = [iid for iid in keep if tree.exists(iid)]
        if selected:
            tree.selection_set(selected)
        if scroll[0] > 0:
            tree.yview_moveto(scroll[0])

    def refresh_packs(self):
        if self.busy:
            self._pending_pack_refresh = True
            return
        if not hasattr(self, 'pack_tree'):
            return
        try:
            packmod.reconcile_installed(self.cfg, self.packs)
        except Exception as exc:
            self._log(f'[警告] 扫描资源包/光影失败：{exc}')
        self.pack_kind_label.set(KIND_NAMES.get(self.pack_kind.get(), '全部'))
        cats = self.packs.categories(self._pack_kind())
        self.pack_cat_combo.configure(values=[''] + cats)
        if self.pack_cat.get() not in cats:
            self.pack_cat.set('')
        inst = packmod.installed_index(self.packs)
        self.pack_rows = self._sorted_pack_entries()
        self.pack_inst_rows = self._filtered_pack_entries(
            [e for e in self.packs.all() if (e.get('kind'), e['id']) in inst])
        self.pack_custom_rows = self._filtered_pack_entries([e for e in self.packs.all() if e.get('custom')])
        self._render_pack_table(self.pack_tree, self.pack_rows, inst)
        self._render_pack_table(self.pack_inst_tree, self.pack_inst_rows, inst, installed_view=True)
        self._render_pack_table(self.pack_custom_tree, self.pack_custom_rows, inst)
        self._clear_mod_icon_targets(self.pack_um_tree)
        self.pack_um_tree.delete(*self.pack_um_tree.get_children())
        self.pack_um_rows = {}
        try:
            unknown = packmod.unmatched_packs(self.cfg, self.packs, self._pack_kind())
        except OSError as exc:
            unknown = []
            self._log(f'[警告] 未受管包扫描失败：{exc}')
        kw = self.pack_search.get().strip().lower()
        for kind, item in unknown:
            if kw and kw not in item.file_name.lower():
                continue
            iid = f'{kind}:{item.file_name}'
            self.pack_um_rows[iid] = kind, item
            self.pack_um_tree.insert('', 'end', iid=iid, values=(packwiki.KIND_LABELS[kind], item.file_name,
                item.version or '版本未知', self._fmt_time(item.mtime), '文件可用' if item.enabled else '文件已禁用'))
            self._set_mod_icon(self.pack_um_tree, iid, {'id': iid}, [item.path])
        self._reapply_sort(self.pack_um_tree)
        kind = self._pack_kind()
        def hint(k):
            folder = packmod.resolve_pack_dir(self.cfg, k)
            return self._short_path(str(folder)) if folder else packmod.folder_hint(self.cfg, k)
        hints = [hint(kind)] if kind else [f'{packwiki.KIND_LABELS[k]}：{hint(k)}' for k in packmod.DIR_NAMES]
        self.pack_dir_label.configure(text='安装目录：' + '　'.join(hints))
        self._set_status(f'资源包与光影：已安装 {len(self.pack_inst_rows)}，目录 {len(self.pack_rows)}，未受管 {len(self.pack_um_rows)}')
        self._sync_pack_update_buttons()
        self._sync_pack_wiki_view()

    def _selected_packs(self):
        trees = (self.pack_inst_tree, self.pack_tree, None, self.pack_custom_tree)
        tree = trees[self.pack_nb.index('current')]
        if tree is None:
            return []
        selected = set(tree.selection())
        return [entry for entry in self._pack_table_rows.get(tree, []) if entry['id'] in selected]

    def _selected_unmanaged_packs(self):
        return [self.pack_um_rows[i] for i in self.pack_um_tree.selection() if i in self.pack_um_rows]

    def _refresh_pack_icons(self):
        inst = packmod.installed_index(self.packs)
        for tree, entries in self._pack_table_rows.items():
            for entry in entries:
                if tree.exists(entry['id']):
                    self._pack_icon(tree, entry['id'], entry, inst.get((entry['kind'], entry['id'])) or {})

    def _show_pack_view(self):
        self.pack_wiki_frame.pack_forget()
        self.pack_offline_frame.pack_forget()
        online = self.pack_view_mode.get() == 'wiki'
        (self.pack_wiki_frame if online else self.pack_offline_frame).pack(fill='both', expand=True)
        if online:
            self._ensure_pack_view()

    def _ensure_pack_view(self):
        if self.pack_view_mode.get() != 'wiki' or not self.pack_wiki_frame.winfo_ismapped():
            return
        if self._pack_view is not None:
            if self._pack_view.proxy == self.cfg.proxy:
                return
            self._close_pack_view()
        view = packs_browser.EmbeddedPacksBrowser(self.cfg,
            lambda kind, value: self.queue.put(('pack_wiki_view', view, kind, value)))
        self._pack_view, self._pack_auto_sync = view, False
        self.pack_wiki_status.set('正在加载资源包与光影 Wiki；如出现人机验证，请在页内完成。')
        try:
            self._sync_pack_wiki_view()
            view.set_color_scheme(self.theme.dark)
            view.start(self.pack_wiki_frame.winfo_id(), self.pack_wiki_frame.winfo_width(),
                       self.pack_wiki_frame.winfo_height())
        except Exception as exc:
            self._on_pack_view_event(view, 'error', str(exc))

    def _close_pack_view(self):
        if getattr(self, '_pack_view', None):
            self._pack_view.close()
            self._pack_view = None

    def _resize_pack_view(self, event):
        if self._pack_view:
            try:
                self._pack_view.resize(event.width, event.height)
            except OSError as exc:
                self._on_pack_view_event(self._pack_view, 'error', str(exc))

    def _navigate_pack_wiki(self, action):
        self.pack_view_mode.set('wiki')
        self._show_pack_view()
        if self._pack_view:
            self._pack_view.navigate(action)

    def _sync_pack_wiki_view(self):
        if getattr(self, '_pack_view', None):
            inst = {key: dict(rec) for key, rec in packmod.installed_index(self.packs).items()}
            for (kind, _), rec in inst.items():
                folder = packmod.resolve_pack_dir(self.cfg, kind)
                rec['directory_unavailable'] = not folder or not folder.is_dir()
            self._pack_view.update(self.packs.by_kind(), inst, self.busy)
            self._schedule_wiki_auto_sync(self._pack_view, self.pack_wiki_frame,
                '_pack_view', '_pack_auto_sync', self.refresh_packs_wiki)

    def _on_pack_view_event(self, view, kind, value):
        if view is not self._pack_view:
            return
        if kind == 'attach':
            try:
                view.attach(value, self.pack_wiki_frame.winfo_width(), self.pack_wiki_frame.winfo_height())
            except OSError as exc:
                self._on_pack_view_event(view, 'error', str(exc))
        elif kind == 'error':
            self._close_pack_view()
            self.pack_wiki_status.set(f'网页加载失败：{value}。已保留离线目录，点「Wiki 原网页」可重试。')
            self.pack_view_mode.set('offline')
            self._show_pack_view()
        elif kind == 'page':
            view.resize(self.pack_wiki_frame.winfo_width(), self.pack_wiki_frame.winfo_height())
            count = value.get('rows', 0)
            self.pack_wiki_status.set(f"Wiki 原网页：{count} 个表格条目，{value.get('matched', 0)} 个已匹配管理目录。"
                                      if count else '正在等待 Wiki 正文；可随时切换离线目录。')
            if count and not self._pack_auto_sync and not self.busy:
                self._pack_auto_sync = True
                self.refresh_packs_wiki()
        elif kind == 'action':
            self._handle_pack_wiki_action(view, value)
        elif kind == 'icons' and wiki_browser.is_catalog_url(value.get('page'), view.page_url):
            self._apply_pack_icons(value.get('icons', []))

    def _apply_pack_icons(self, icons):
        if self.busy:
            self._pending_pack_icons = icons
            return
        changed = False
        for icon in icons:
            entry, url = self.packs.get(icon.get('id')), icon.get('url', '')
            if (entry and not entry.get('custom') and not entry.get('wiki_removed')
                    and allowed_icon_url(url, self.cfg.wiki_url) and entry.get('icon_url') != url):
                entry['icon_url'] = url
                changed = True
        if changed:
            self.packs.save()
            self._refresh_pack_icons()

    def _handle_pack_wiki_action(self, view, event):
        from .gui import _has_grab
        if self.busy or _has_grab(self.root):
            return
        if event.get('action') == 'open':
            # 共用 MOD 桥接的 HTTP(S) 链接校验；不接受文件或命令协议。
            self._handle_wiki_action(view, event)
            return
        if not wiki_browser.is_catalog_url(event.get('page'), view.page_url):
            return
        action = event.get('action')
        if action == 'sync':
            self.refresh_packs_wiki()
            return
        entry = self.packs.get(event.get('id'))
        if not entry or entry.get('custom') or entry.get('wiki_removed'):
            return
        if action == 'install' and not entry.get('local'):
            self._install_packs([entry])
        elif action == 'detail':
            self._pack_detail_dialog(entry)
        elif action == 'manage':
            self.pack_kind.set(entry['kind'])
            self.pack_cat.set('')
            self.pack_search.set('')
            self.pack_nb.select(0)
            self.refresh_packs()
            if self.pack_inst_tree.exists(entry['id']):
                self.pack_inst_tree.selection_set(entry['id'])
                self.pack_inst_tree.see(entry['id'])
                self._pack_detail_dialog(entry)

    def refresh_packs_wiki(self):
        if self.busy:
            return
        self._set_busy(True)
        self._log('正在同步资源包与光影 Wiki 管理目录…')
        view = self._pack_view
        def work():
            if view and view.ready:
                return view.fetch_wiki()
            return packwiki.fetch_packs(self.cfg, interactive=True,
                progress_cb=lambda msg: self.queue.put(('log', msg)))
        def done(res):
            try:
                if res is None:
                    return
                if res.source != 'online':
                    raise ValueError('本次未在线同步，已保留目录与上次在线成功时间')
                self._log(f'Wiki 读取证据：发起 {res.request_count} 个网络请求；通道={res.channel}；'
                          f'响应={res.response_bytes} 字节；内容指纹={res.text_hash[:12]}')
                entries, warnings, changes = packmod.apply_wiki_result(self.packs, res)
                self._pack_auto_sync = True
                # 不整体作废检查结果：下载源有变化的条目会在执行更新时逐项识别并要求重新检查
                for warning in warnings:
                    self._log(f'[警告] {warning}')
                self._log(f'资源包与光影在线同步完成：{len(entries)} 个条目，{len(changes)} 处变化')
                for change in changes[:30]:
                    self._log('  - ' + change)
            except Exception as exc:
                self._log(f'[错误] 资源包与光影同步失败：{exc}')
                messagebox.showerror('刷新失败', str(exc))
            finally:
                self._set_busy(False)
                self.refresh_packs()
        self._run_async(work, done)

    def _drop_pack_checks(self, keys):
        """从本次检查结果中移除指定 (kind, id)，其余结果保留。"""
        snapshot = self._pack_checked_updates
        if snapshot:
            self._pack_checked_updates = {**snapshot, 'plan': [
                p for p in snapshot['plan'] if (p.kind, p.pack_id) not in keys]}
        for key in keys:
            self._pack_updates.pop(key, None)
            self._pack_check_status.pop(key, None)
        self._sync_pack_update_buttons()

    def _invalidate_pack_checks(self):
        self._pack_checked_updates = None
        self._pack_updates = {}
        self._pack_check_status = {}
        self._sync_pack_update_buttons()

    def _sync_pack_update_buttons(self):
        snapshot = getattr(self, '_pack_checked_updates', None)
        enabled = not self.busy and snapshot and any(p.action == 'update' for p in snapshot['plan'])
        for attr in ('btn_pack_update', 'btn_pack_update_all'):
            button = getattr(self, attr, None)
            if button:
                button.configure(state='normal' if enabled else 'disabled')

    def check_pack_updates(self):
        if self.busy:
            return
        self._set_busy(True)
        self._log('正在检查已安装资源包与光影的更新…')
        def work():
            return packmod.check_update_plan(self.cfg, self.packs,
                progress_cb=lambda msg: self.queue.put(('log', msg)))
        def done(snapshot):
            self._set_busy(False)
            if snapshot is None:
                return
            self._pack_checked_updates = snapshot
            plans = snapshot['plan']
            self._pack_updates = {(p.kind, p.pack_id): p.version for p in plans if p.action == 'update'}
            self._pack_check_status = {(p.kind, p.pack_id): {'action': p.action,
                'detail': f'可更新 → {p.version}' if p.action == 'update' else p.detail} for p in plans}
            for plan in plans:
                self._log(f'{plan.name}：{plan.detail}')
            errors = sum(p.action == 'error' for p in plans)
            pending = sum(p.action == 'manual' or p.detail.startswith('无法比较') for p in plans)
            summary = f'检查完成：{len(self._pack_updates)} 个可用更新，{errors} 个查询失败，{pending} 个需人工判断。'
            self._log(summary)
            self.refresh_packs()
            messagebox.showinfo('检查完成', summary)
        self._run_async(work, done)

    def update_selected_packs(self):
        self._use_checked_pack_updates({e['id'] for e in self._selected_packs()})

    def update_all_packs(self):
        self._use_checked_pack_updates()

    def _use_checked_pack_updates(self, selected=None):
        if self.busy:
            return
        snapshot = self._pack_checked_updates
        if not snapshot:
            messagebox.showinfo('请先检查更新', '尚未检查更新，请先点击“检查更新”。')
            return
        plans = deepcopy([p for p in snapshot['plan'] if p.action == 'update'
                          and (selected is None or p.pack_id in selected)])
        plans, changed = packmod.split_checked_updates(self.cfg, self.packs, snapshot, plans)
        if changed:
            # 只移除检查后发生变化的条目，其余检查结果照常可用
            self._drop_pack_checks({(p.kind, p.pack_id) for p in changed})
            snapshot = self._pack_checked_updates
            names = '、'.join(p.name for p in changed)
            self._log(f'检查后文件、锁定或下载源已变化，已移出本次更新：{names}（需重新检查）')
            if not plans:
                messagebox.showinfo('需要重新检查', f'{names} 在检查后发生了变化，请重新检查更新。')
                return
            messagebox.showinfo('部分项目需重新检查',
                                f'{names} 在检查后发生了变化，已跳过；其余项目继续使用本次检查结果。')
        if not plans:
            messagebox.showinfo('没有可更新项目', '本次检查没有符合条件的选中项目，请查看检查结果。')
            return
        text = '\n'.join(f'{packwiki.KIND_LABELS[p.kind]} · {p.name} → {p.version}' for p in plans)
        if not messagebox.askyesno('确认更新计划', '将执行本次检查的更新，旧文件会先备份：\n\n' + text):
            return
        reason = packmod.validate_checked_updates(self.cfg, self.packs, snapshot, plans)
        if reason:
            self._invalidate_pack_checks()
            messagebox.showinfo('需要重新检查', reason)
            return
        # 只消费本次执行的条目，其余检查结果保留给后续「全部更新」
        self._drop_pack_checks({(p.kind, p.pack_id) for p in plans})
        self._run_pack_installs(plans, {}, snapshot=snapshot)

    def _change_pack_enabled(self, enabled):
        self._manage_selected_packs(lambda e: packmod.set_enabled(self.cfg, self.packs, e['kind'], e['id'], enabled))

    def _toggle_pack_lock(self):
        entries = self._selected_packs()
        lock = any(not self.packs.installed(e['kind']).get(e['id'], {}).get('locked') for e in entries)
        self._manage_selected_packs(lambda e: packmod.set_locked(self.packs, e['kind'], e['id'], lock))

    def _manage_selected_packs(self, action):
        if self.busy:
            return
        entries = self._selected_packs()
        if not entries:
            messagebox.showinfo('提示', '请先选择已安装条目')
            return
        errors = []
        for entry in entries:
            try:
                action(entry)
            except Exception as exc:
                errors.append(f"{entry.get('name_en')}：{exc}")
        # 只作废被操作条目的检查结果，其余条目仍可「全部更新」
        self._drop_pack_checks({(e['kind'], e['id']) for e in entries})
        self.refresh_packs()
        if errors:
            messagebox.showwarning('部分操作未完成', '\n'.join(errors))

    def uninstall_selected_packs(self):
        if self.busy:
            return
        entries = self._selected_packs()
        if not entries:
            messagebox.showinfo('提示', '请先选择要卸载的已安装条目')
            return
        files = [name for e in entries for name in
                 [self.packs.installed(e['kind']).get(e['id'], {}).get('file_name')]
                 if name]
        if not files:
            return
        if not messagebox.askyesno('确认卸载', '将备份后卸载以下包及其已匹配副本：\n\n' + '\n'.join(files)):
            return
        self._manage_selected_packs(lambda e: packmod.uninstall_pack(self.cfg, self.packs, e['kind'], e['id']))

    def reveal_selected_pack(self):
        selected = self._selected_packs()
        if selected:
            entry = selected[0]
            rec = self.packs.installed(entry['kind']).get(entry['id']) or {}
            folder = packmod.resolve_pack_dir(self.cfg, entry['kind'])
            name = rec.get('file_name')
            if folder and name and packmod._safe_component(name):
                self._reveal(folder / name)

    def register_unmanaged_pack(self):
        if self.busy:
            return
        chosen = self._selected_unmanaged_packs()
        if not chosen:
            messagebox.showinfo('提示', '请先选择一个未受管的包')
            return
        self._register_one_pack(*chosen[0])

    def associate_selected_pack(self):
        if self.busy:
            return
        chosen = self._selected_unmanaged_packs()
        if not chosen:
            messagebox.showinfo('提示', '请先选择一个未受管的包')
            return
        kind, item = chosen[0]
        entries = [e for e in self.packs.by_kind(kind) if not e.get('local')]
        top = self._dialog('关联已有包条目', '640x200')
        ttk.Label(top, text=item.file_name, wraplength=600).pack(anchor='w', padx=12, pady=10)
        labels = {f"{e.get('name_en') or e['id']} · {e['id']}": e for e in entries}
        value = tk.StringVar()
        ttk.Combobox(top, textvariable=value, values=tuple(labels), state='readonly', width=75).pack(padx=12)
        def confirm():
            entry = labels.get(value.get())
            if entry:
                self.packs.add_alias(entry['id'], packmod.suggest_pack_name(item.file_name))
                top.destroy()
                self._invalidate_pack_checks()
                self.refresh_packs()
        ttk.Button(top, text='关联', command=confirm).pack(pady=12)

    def ignore_selected_packs(self):
        if self.busy:
            return
        for kind, item in self._selected_unmanaged_packs():
            names = self.cfg.data.setdefault('ignored_pack_files', {}).setdefault(kind, [])
            if item.file_name not in names:
                names.append(item.file_name)
        self.cfg.save()
        self.refresh_packs()

    def restore_ignored_packs(self):
        if self.busy:
            return
        kinds = [self._pack_kind()] if self._pack_kind() else list(packmod.DIR_NAMES)
        for kind in kinds:
            self.cfg.data.setdefault('ignored_pack_files', {})[kind] = []
        self.cfg.save()
        self.refresh_packs()

    def edit_selected_pack_source(self):
        entries = self._selected_packs()
        if entries and not self.busy:
            self._pack_source_dialog(entries[0])

    def remove_selected_custom_packs(self):
        if self.busy:
            return
        entries = [e for e in self._selected_packs() if e.get('custom')]
        if entries and messagebox.askyesno('移除自定义条目', '只移除管理条目，保留已安装文件。继续？'):
            for entry in entries:
                self.packs.remove_custom(entry['id'])
            self._invalidate_pack_checks()
            self.refresh_packs()
