"""CLI 交互菜单壳。

用法：
  py -m gtnhmod cli           交互菜单
  py -m gtnhmod cli --check   非交互：检查更新（供计划任务）
  py -m gtnhmod cli --update-all  非交互：更新全部可更新的mod
"""
import os
import sys
import webbrowser
from pathlib import Path

from . import SIDES, SIDE_LABELS, __version__, updater, utils
from . import wiki as wikimod
from .config import Config
from .db import ModsDB
from .installed import InstalledDB
from .ui import ConsoleUI
from . import cookies

def pad(s, width: int) -> str:
    """按显示宽度补齐（中文按2列宽）。"""
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))
    return str(s) + " " * max(0, width - w)


class CliApp:
    def __init__(self, data_dir: Path):
        self.cfg = Config(data_dir)
        self.db = ModsDB(data_dir / "mods_db.json")
        self.installed = InstalledDB(data_dir / "installed.json")
        # 启动维护：校正已安装记录、清理积压备份、清扫孤儿临时文件
        updater.startup_maintenance(self.cfg, self.db, self.installed)
        self.ui = ConsoleUI()

    # ---------- 菜单入口 ----------
    def first_run_setup(self):
        """首次运行：引导设置 mods 目录。"""
        if self.cfg.client_mods_dir or self.cfg.server_mods_dir:
            return
        self.ui.info("欢迎使用 GTNH 额外MOD管理工具（首次运行）")
        self.ui.info("请先设置客户端与服务端的 mods 文件夹（可只设置一个，以后可在菜单 10 修改）")
        for side in SIDES:
            p = self.ui.input_text(f"{SIDE_LABELS[side]} mods 文件夹路径", "")
            if p:
                self.cfg.set_mods_dir(side, p)
        if not self.db.mods:
            self.ui.info("尚未有 wiki 数据，进入后请先执行「刷新Wiki数据」")

    def main_menu(self):
        self.first_run_setup()
        menu = [
            "刷新Wiki数据", "可添加MOD列表", "已安装MOD", "检查更新", "更新MOD",
            "启用/禁用MOD", "未受管MOD", "自定义源管理", "备份管理", "设置", "退出",
        ]
        while True:
            print()
            print(f"===== GTNH 额外MOD管理工具 v{__version__} =====")
            if self.cfg.client_mods_dir:
                print(f"客户端: {self.cfg.client_mods_dir}")
            if self.cfg.server_mods_dir:
                print(f"服务端: {self.cfg.server_mods_dir}")
            idx = self.ui.choose("请选择功能:", menu, allow_cancel=False)
            if idx is None:
                return  # EOF/中断 → 退出
            if idx == 10:
                return
            try:
                (self.do_refresh_wiki, self.do_list_addable, self.do_installed,
                 self.do_check, self.do_update_menu, self.do_toggle_menu,
                 self.do_unmanaged, self.do_custom_sources, self.do_backups,
                 self.do_settings)[idx]()
            except KeyboardInterrupt:
                print()
                continue
            except Exception as e:
                self.ui.error(f"操作失败: {e}")

    # ---------- 1 刷新wiki ----------
    def do_refresh_wiki(self):
        self.ui.info("正在抓取 wiki 数据（gtnh.huijiwiki.com）...")
        try:
            mods, warnings = wikimod.fetch_and_parse(self.cfg)
        except Exception as e:
            self.ui.error(f"抓取失败: {e}")
            return
        for w in warnings:  # 合并前打印（缓存/限流提示是合并失败时的重要上下文）
            self.ui.warn(w)
        try:
            changes = self.db.merge_wiki(mods)
        except Exception as e:
            self.ui.error(f"合并失败: {e}")
            return
        if not changes:
            self.ui.ok("wiki 数据已是最新，无变化")
            return
        self.ui.info(f"wiki 数据已更新（共 {len(self.db.wiki_mods())} 个mod）：")
        for c in changes:
            self.ui.info(f"  - {c}")

    # ---------- 2 可添加列表 ----------
    def do_list_addable(self):
        entries = self.db.wiki_mods()
        if not entries:
            self.ui.warn("wiki 数据为空，请先执行「刷新Wiki数据」")
            return
        groups = self.db.by_group_category()
        keys = sorted(groups, key=lambda k: (k[0] != "星门规则", k))
        options = [f"{g}/{c}（{len(groups[k])}个）" for k in keys for g, c in [k]]
        self.ui.info("按网站分类浏览（星门规则/非星门规则）：")
        idx = self.ui.choose("选择分类:", options)
        if idx is None:
            return
        g, c = keys[idx]
        entries = sorted(groups[(g, c)], key=lambda e: e["id"])
        self._pick_mod(entries, f"{g}/{c}")

    def _pick_mod(self, entries, title):
        while True:
            print(f"--- {title} ---")
            marks = self._installed_marks()
            lines = []
            for e in entries:
                side_txt = SIDE_LABELS.get(e["side"], e["side"]) + ("?" if e["side_uncertain"] else "")
                name = e["name_en"] or e["name_cn"] or e["id"]
                cn = f"（{e['name_cn']}）" if e["name_cn"] else ""
                mark = f"[已装:{marks.get(e['id'])}] " if e["id"] in marks else ""
                lines.append(f"{mark}{pad(name, 34)} {pad(side_txt, 5)} {e['desc'][:36]}")
            idx = self.ui.choose("选择mod查看/操作（[已装]标记=已安装）:", lines)
            if idx is None:
                return
            self._mod_detail(entries[idx])

    def _installed_marks(self) -> dict:
        """{mod_id: 安装端别中文}，用于标记已安装。"""
        marks = {}
        for m in updater.build_merged_registry(self.cfg, self.db, self.installed):
            marks[m["mod_id"]] = {"both": "双端", "client": "客户端", "server": "服务端"}[m["install_side"]]
        return marks

    def _mod_detail(self, entry):
        while True:
            print(f"\n=== {entry['name_en']} {entry['name_cn']} ===")
            print(f"分组/分类: {entry['group']} / {entry['category']}")
            print(f"端别: {SIDE_LABELS.get(entry['side'], entry['side'])}"
                  + ("（wiki标注不确定）" if entry["side_uncertain"] else ""))
            mark = self._installed_marks().get(entry["id"])
            print(f"已安装: {mark if mark else '未安装'}")
            if entry["desc"]:
                print(f"简介: {entry['desc']}")
            if entry["detail"]:
                print("详细信息:")
                for line in entry["detail"].split("\n"):
                    print(f"  {line}")
            links = []
            for key, label in (("github", "GitHub"), ("curseforge", "CurseForge"),
                               ("mcmod", "mcmod"), ("bilibili", "bilibili")):
                if entry["urls"].get(key):
                    links.append((label, entry["urls"][key]))
            actions = ["安装（自动选择端别）", "打开下载页面", "绑定下载源", "编辑中文名"]
            idx = self.ui.choose("操作:", actions)
            if idx is None:
                return
            if idx == 0:
                self._install_to(entry)
            elif idx == 1:
                if not links:
                    self.ui.warn("该mod没有可用下载链接")
                for label, url in links:
                    self.ui.info(f"  打开 {label}: {url}")
                    webbrowser.open(url)
                    break
            elif idx == 2:
                self._bind_source_flow(entry)
            elif idx == 3:
                cur = entry.get("name_cn") or "（无）"
                v = self.ui.input_text(f"中文名（当前: {cur}，留空=恢复wiki原名）", "")
                r = updater.set_name_cn(self.db, entry["id"], v)
                if r["action"] == "saved":
                    self.ui.ok(f"中文名已更新为: {r['name_cn'] or '（恢复wiki原名）'}")
                else:
                    self.ui.error(r.get("error") or "保存失败")

    def _bind_source_flow(self, entry):
        """列出该mod的全部下载链接，选择其一绑定为下载源（检查更新/下载用它）。"""
        cand = updater.bindable_links(entry)
        if not cand:
            self.ui.warn("该mod的下载链接列表不完整（可能是旧版工具保存的数据），"
                         "请先执行菜单1「刷新Wiki数据」")
            return
        cur = updater.current_source_url(entry)
        lines = []
        for l in cand:
            mark = " ← 当前绑定" if l["url"] == cur else ""
            lines.append(f"{l['label']} - {l['url']}{mark}")
        sel = self.ui.choose("选择要绑定的下载源（检查更新/自动下载将使用它）:", lines)
        if sel is None:
            return
        r = updater.bind_source(self.db, entry["id"], cand[sel]["url"])
        if r["action"] == "bound":
            self.ui.ok(f"已绑定下载源: {cand[sel]['label']} - {cand[sel]['url']}")
        else:
            self.ui.error(r.get("error") or "绑定失败")

    def _pick_version_from(self, options, current=None, title="选择版本"):
        """版本选择器（[推荐]标记适配整合包版本的版本）。返回版本字符串或 None。"""
        gtnh = self.cfg.data.get("gtnh_version") or ""
        lines = []
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
            lines.append(f"{o['version']}{tag}")
        head = title + (f"（你的整合包版本: {gtnh}）" if gtnh
                        else "（未设置整合包版本，菜单10设置后可获得推荐标记）")
        idx = self.ui.choose(head, lines)
        if idx is None:
            return None
        return options[idx]["version"]

    def _install_to(self, entry):
        """安装：按mod端别声明自动选择端别。"""
        sides, note = updater.auto_install_sides(entry, self.cfg)
        if not sides:
            self.ui.error(note or "两端mods目录均未设置，请先在菜单10设置")
            return
        if note:
            self.ui.warn(note)
        side_txt = "、".join(SIDE_LABELS[s] for s in sides)
        # 版本选择（有版本列表时；manual/curseforge 源无列表走原流程）
        self.ui.info("正在获取可用版本列表...")
        options, verr = updater.list_install_options(entry, self.cfg, self.db, force=True)
        if verr:
            self.ui.error(f"获取版本列表失败: {verr}")
            return
        version = None
        if options:
            version = self._pick_version_from(options, None,
                                              f"选择要安装的版本（{entry['name_en'] or entry['id']}）")
            if version is None:
                return
        if not self.ui.confirm(f"安装 {entry['name_en'] or entry['id']}"
                               + (f" v{version}" if version else "（最新）")
                               + f" 到 {side_txt}（按端别标注自动判断）?"):
            return
        self.ui.info("正在下载...")
        for side in sides:
            r = updater.install_mod(self.cfg, self.db, self.installed, entry["id"], side,
                                    version=version, prefetched=options)
            self._print_install_result(r, side)
            if r["action"] in ("installed",):
                if r.get("body"):
                    self.ui.info(f"更新日志:\n{r['body'][:600]}")

    def _print_install_result(self, r, side):
        if r.get("warning"):
            self.ui.warn(r["warning"])
        if r["action"] == "installed":
            self.ui.ok(f"已安装到{SIDE_LABELS[side]}: {r['file']} (版本 {r['version']})")
        elif r["action"] == "manual":
            self.ui.warn(f"{SIDE_LABELS[side]}: " + (r.get("note") or "该mod无自动下载渠道"))
            entry = r.get("entry") or {}
            url = (entry.get("urls") or {}).get("curseforge") or (entry.get("urls") or {}).get("github")
            if url and self.ui.confirm("打开浏览器手动下载?"):
                webbrowser.open(url)
        elif r["action"] == "skipped_incompatible":
            self.ui.warn(f"{SIDE_LABELS[side]}: " + (r.get("note") or "版本与当前GTNH不兼容"))
        else:
            self.ui.error(f"{SIDE_LABELS[side]}: " + (r.get("error") or "安装失败"))

    # ---------- 3 已安装 ----------
    def do_installed(self):
        while True:
            merged = updater.build_merged_registry(self.cfg, self.db, self.installed)
            if not merged:
                self.ui.info("（无已受管mod）")
                return
            print(f"\n--- 已安装MOD（共 {len(merged)} 个，一个mod一行） ---")
            lines = []
            for m in merged:
                lock = "🔒" if m["locked"] else ""
                dup = "⚠重复" if m.get("duplicates") else ""
                cs = self._side_state_text(m["sides"].get("client"))
                ss = self._side_state_text(m["sides"].get("server"))
                side_l = {"both": "双端", "client": "仅客户端", "server": "仅服务端"}[m["install_side"]]
                upd = (f" →最新{m['latest_version']}"
                       if m["status"] == "update_avail" and m["latest_version"] else "")
                lines.append(f"{pad(m['name_en'] + lock + dup, 34)} {pad(side_l, 6)} "
                             f"客户端:{cs}  服务端:{ss}{upd}")
            idx = self.ui.choose("选择编号进行操作:", lines)
            if idx is None:
                return
            self._installed_actions(merged[idx])

    def _side_state_text(self, st) -> str:
        if st is None:
            return "—"
        if not st["enabled"]:
            return f"v{st['version']}✗禁用"
        if st["status"] == "update_avail":
            return f"v{st['version']}↑可更新"
        return f"v{st['version']}✓"

    def _installed_actions(self, m):
        actions = ["更新（所有已装端别）", "启用/禁用（选端别）", "锁定/解锁",
                   "从列表剔除", "删除mod", "打开下载页面"]
        has_dup = bool(m.get("duplicates"))
        if has_dup:
            actions.append("清理重复jar（保留最近一次安装/回滚的版本）")
        a = self.ui.choose(f"操作 {m['name_en']}（{m['name_cn']}）:", actions)
        if a is None:
            return
        if a == 0:
            entry = self.db.get(m["mod_id"]) or {}
            self.ui.info("正在获取可用版本列表...")
            options, verr = updater.list_install_options(entry, self.cfg, self.db, force=True)
            if verr:
                self.ui.error(f"获取版本列表失败: {verr}")
                return
            version = None
            if options:
                cur = next(iter(m["sides"].values()))
                version = self._pick_version_from(options, cur["version"],
                                                  f"选择要更新到的版本（{m['name_en']}）")
                if version is None:
                    return
            for side in m["sides"]:
                self.ui.info(f"更新 {SIDE_LABELS[side]} {m['name_en']} 到 "
                             + (f"v{version}" if version else "最新") + "...")
                r = updater.update_mod(self.cfg, self.db, self.installed, m["mod_id"], side,
                                       version=version, prefetched=options)
                self._print_update_result(r, side, m["name_en"])
        elif a == 1:
            side_opts = [s for s in SIDES if s in m["sides"]]
            if not side_opts:
                return
            s = self.ui.choose("选择端别:", [SIDE_LABELS[x] for x in side_opts])
            if s is None:
                return
            side = side_opts[s]
            st = m["sides"][side]
            want = not st["enabled"]
            if not want and self.cfg.data.get("core_mod_confirm") \
                    and not self.ui.confirm(f"确认禁用 {SIDE_LABELS[side]} {m['name_en']}?"):
                return
            r = updater.set_enabled(self.cfg, self.db, self.installed, m["mod_id"], side, want)
            if r["action"] in ("enabled", "disabled"):
                self.ui.ok(f"已{'启用' if want else '禁用'}: {r['file']}")
            elif r["action"] == "unchanged":
                self.ui.info("状态未变化")
            else:
                self.ui.error(r.get("error") or "操作失败")
        elif a == 2:
            new = not m["locked"]
            for side in m["sides"]:
                updater.set_lock(self.installed, m["mod_id"], side, new)
            self.ui.ok(f"{m['name_en']}: 已{'锁定' if new else '解锁'}（所有已装端别）")
        elif a == 3:
            if self.ui.confirm(f"把 {m['name_en']} 从受管列表剔除？\n"
                               "（不会删除文件；可在菜单10「恢复已排除文件」中恢复显示）"):
                names = updater.exclude_installed(self.cfg, self.db, self.installed, m["mod_id"])
                self.ui.ok(f"已剔除: {', '.join(names) or '无文件'}")
        elif a == 4:
            if self.ui.confirm(f"删除 {m['name_en']}？\n"
                               "（jar 会移入 data/backup 备份目录并加 .deleted 后缀，可手动恢复；"
                               "不影响其他mod）"):
                r = updater.delete_mod(self.cfg, self.db, self.installed, m["mod_id"])
                if r["action"] == "deleted":
                    self.ui.ok("已删除: " + "；".join(r["deleted"]))
                else:
                    self.ui.error(r.get("error") or "删除失败")
        elif a == 5:
            entry = self.db.get(m["mod_id"]) or {}
            url = (entry.get("urls") or {}).get("github") or (entry.get("urls") or {}).get("curseforge")
            if url:
                webbrowser.open(url)
                self.ui.info(f"已打开 {m['name_en']} 下载页")
        elif a == 6 and has_dup:
            if self.ui.confirm(f"清理 {m['name_en']} 的重复jar？"
                               "（保留最近一次安装/回滚的版本，其余备份到 data/backup 后移除）"):
                r = updater.cleanup_duplicates(self.cfg, self.db, self.installed, m["mod_id"])
                if r["action"] == "cleaned":
                    for c in r["cleaned"]:
                        self.ui.ok(f"已清理重复jar {c}")
                else:
                    self.ui.info("没有需要清理的重复jar")

    # ---------- 4 检查更新 ----------
    def do_check(self, sides=SIDES):
        if not self._check_dirs():
            return
        self.ui.info("正在检查更新（仅检查已安装且未锁定的mod）...")
        results = updater.check_updates(self.cfg, self.db, self.installed, sides=sides,
                                        progress_cb=lambda s, m: print(
                                            f"  已检查 {SIDE_LABELS[s]}: {m}", end="\r"))
        print()
        self._print_check_results(results)

    def _print_check_results(self, results):
        reg = updater.build_registry(self.cfg, self.db, self.installed)
        counts = updater.summarize_check(results, reg)
        for side, mod_id, info, err in sorted(results, key=lambda r: r[1]):
            entry = self.db.get(mod_id) or {}
            name = entry.get("name_en") or mod_id
            if err:
                self.ui.error(f"  {SIDE_LABELS[side]} {name}: {err}")
                continue
            if not info or not info.latest_version:
                self.ui.info(f"  {SIDE_LABELS[side]} {name}: {info.note if info else '无信息'}")
                continue
            st = reg[side].get(mod_id) or {}
            cur = st.get("version")
            status = updater.version_status(cur, info.latest_version)
            if status == "update":
                self.ui.ok(f"  {SIDE_LABELS[side]} {name}: v{cur} → v{info.latest_version} 可更新")
            elif status == "uptodate":
                self.ui.info(f"  {SIDE_LABELS[side]} {name}: v{cur} 已是最新")
            else:
                self.ui.warn(f"  {SIDE_LABELS[side]} {name}: 当前v{cur}，最新v{info.latest_version}（版本格式无法比较，请手动判断）")
            if info.candidates is None and info.latest_version:
                self.ui.warn(f"      （无自动下载资产，需手动下载）")
        print()
        self.ui.info(f"可更新 {counts['update']} 个 / 已最新 {counts['uptodate']} 个 "
                     f"/ 需手动 {counts['manual']} 个 / 出错 {counts['error']} 个")

    # ---------- 5 更新 ----------
    def do_update_menu(self):
        if not self._check_dirs():
            return
        reg = updater.build_registry(self.cfg, self.db, self.installed)
        targets = []
        for side in SIDES:
            for mod_id, st in reg[side].items():
                if st["enabled"] and not st["locked"]:
                    targets.append((side, st))
        if not targets:
            self.ui.info("没有可更新的mod（或全部已禁用/锁定）")
            return
        options = [f"{SIDE_LABELS[s]} {st['name_en']} v{st['version']}"
                   + (f" → {st['latest_version']}" if st["status"] == "update_avail" else "")
                   for s, st in targets]
        options.append("== 全部更新 ==")
        idx = self.ui.choose("选择要更新的mod（可更新的会高亮在菜单4查看）:", options)
        if idx is None:
            return
        if idx == len(targets):
            if not self.ui.confirm(f"确认更新全部 {len(targets)} 个mod?"):
                return
            results = updater.update_all(self.cfg, self.db, self.installed)
            n_ok = 0
            for r in results:
                if r["action"] == "updated":
                    n_ok += 1
                    self.ui.ok(f"  已更新 {SIDE_LABELS[r['side']]} {r['name']}: {r['from']} → {r['to']}")
                elif r["action"] in ("uptodate",):
                    self.ui.info(f"  {SIDE_LABELS[r['side']]} {r['name']}: 已是最新")
                elif r["action"] == "manual":
                    self.ui.warn(f"  {SIDE_LABELS[r['side']]} {r['name']}: {r.get('note')}")
                elif r["action"] == "skipped_incompatible":
                    self.ui.warn(f"  {SIDE_LABELS[r['side']]} {r['name']}: {r.get('note')}")
                else:
                    self.ui.error(f"  {SIDE_LABELS[r['side']]} {r['name']}: {r.get('error')}")
            self.ui.info(f"完成：成功更新 {n_ok} 个")
            return
        side, st = targets[idx]
        entry = self.db.get(st["mod_id"]) or {}
        self.ui.info("正在获取可用版本列表...")
        options, verr = updater.list_install_options(entry, self.cfg, self.db, force=True)
        if verr:
            self.ui.error(f"获取版本列表失败: {verr}")
            return
        version = None
        if options:
            version = self._pick_version_from(options, st["version"],
                                              f"选择要更新到的版本（{st['name_en']}）")
            if version is None:
                return
        self.ui.info(f"更新 {SIDE_LABELS[side]} {st['name_en']} 到 "
                     + (f"v{version}" if version else "最新") + "...")
        r = updater.update_mod(self.cfg, self.db, self.installed, st["mod_id"], side,
                               version=version, prefetched=options)
        self._print_update_result(r, side, st["name_en"])

    def _print_update_result(self, r, side, name):
        if r.get("warning"):
            self.ui.warn(r["warning"])
        if r["action"] == "updated":
            self.ui.ok(f"已更新 {SIDE_LABELS[side]} {name}: v{r['from']} → v{r['to']}（{r['file']}）")
            if r.get("body"):
                self.ui.info(f"更新日志:\n{r['body'][:600]}")
        elif r["action"] == "uptodate":
            self.ui.info(f"{name} 已是最新版本")
        elif r["action"] == "manual":
            self.ui.warn(r.get("note") or "该mod无自动下载渠道，请手动下载后放入mods目录")
            entry = r.get("entry") or {}
            urls = entry.get("urls") or {}
            if urls.get("curseforge") or urls.get("github"):
                if self.ui.confirm("打开下载页面?"):
                    webbrowser.open(urls["curseforge"] or urls["github"])
        elif r["action"] == "skipped_incompatible":
            self.ui.warn(r.get("note") or "版本与当前GTNH不兼容，未更新")
        else:
            self.ui.error(r.get("error") or "更新失败")

    # ---------- 6 启用/禁用 ----------
    def do_toggle_menu(self):
        if not self._check_dirs():
            return
        reg = updater.build_registry(self.cfg, self.db, self.installed)
        targets = []
        for side in SIDES:
            for mod_id, st in reg[side].items():
                targets.append((side, st))
        if not targets:
            self.ui.info("没有已受管的mod")
            return
        options = [f"{SIDE_LABELS[s]} {st['name_en']} v{st['version']}"
                   + ("  [已禁用]" if not st["enabled"] else "")
                   for s, st in targets]
        idx = self.ui.choose("选择要切换启用状态的mod:", options)
        if idx is None:
            return
        side, st = targets[idx]
        want = not st["enabled"]
        if not want and self.cfg.data.get("core_mod_confirm") \
                and not self.ui.confirm(f"确认禁用 {SIDE_LABELS[side]} {st['name_en']}?（游戏/服务端需重启生效）"):
            return
        r = updater.set_enabled(self.cfg, self.db, self.installed, st["mod_id"], side, want)
        if r["action"] in ("enabled", "disabled"):
            self.ui.ok(f"已{'启用' if want else '禁用'}: {r['file']}")
        elif r["action"] == "unchanged":
            self.ui.info("状态未变化")
        else:
            self.ui.error(r.get("error") or "操作失败")

    # ---------- 7 未受管 ----------
    def do_unmanaged(self):
        um = updater.unmatched_files(self.cfg, self.db)
        total = sum(len(v) for v in um.values())
        if not total:
            self.ui.info("没有未受管的jar（GTNH核心mod等不在可添加列表中的jar会出现在这里）")
            return
        for side in SIDES:
            for f in um[side]:
                print(f"\n{SIDE_LABELS[side]}: {f.file_name}"
                      + (f"（识别版本 {f.version}）" if f.version else ""))
                actions = ["关联到已有条目（记入别名）", "注册为新自定义mod（无上游）",
                           "忽略此文件", "跳过"]
                idx = self.ui.choose("如何处理?", actions)
                if idx == 0:
                    entries = sorted(self.db.all(), key=lambda e: e["id"])
                    names = [f"{e['name_en'] or e['id']}（{e['name_cn']}）" for e in entries]
                    e_idx = self.ui.choose("选择要关联的mod:", names)
                    if e_idx is not None:
                        updater.associate_unmanaged(self.db, entries[e_idx]["id"],
                                                    f.name_part or f.file_name)
                        self.ui.ok(f"已关联到 {entries[e_idx]['name_en'] or entries[e_idx]['id']}，"
                                   "下次扫描生效")
                elif idx == 1:
                    if self.ui.confirm(f"把 {f.file_name} 注册为自定义mod（端别可稍后修改）?"):
                        eid = updater.register_unmanaged(self.cfg, self.db, side, f.file_name)
                        self.ui.ok(f"已注册: {eid}（在菜单8可编辑端别/源）")
                elif idx == 2:
                    updater.ignore_unmanaged(self.cfg, f.file_name)
                    self.ui.info(f"已忽略 {f.file_name}（可在菜单10取消忽略）")

    # ---------- 8 自定义源 ----------
    def do_custom_sources(self):
        while True:
            customs = self.db.custom_mods()
            options = [f"{e['name_en'] or e['id']}（{e['name_cn']}）"
                       f" 端别:{SIDE_LABELS.get(e['side'], e['side'])} 源:{e['source_type']}"
                       for e in customs]
            options.append("+ 添加自定义源")
            idx = self.ui.choose(f"自定义源（共{len(customs)}个）:", options)
            if idx is None:
                return
            if idx == len(customs):
                self._add_custom_source()
                continue
            e = customs[idx]
            actions = ["编辑端别", "编辑GitHub源", "删除"]
            a = self.ui.choose(f"操作 {e['name_en'] or e['id']}:", actions)
            if a == 0:
                s = self.ui.choose("选择端别:", ["客户端", "服务端", "双端"])
                if s is not None:
                    self.db.update_custom(e["id"], {"side": ("client", "server", "both")[s]})
                    self.ui.ok("已更新端别")
            elif a == 1:
                owner = self.ui.input_text("GitHub owner", (e.get("source") or {}).get("owner", ""))
                repo = self.ui.input_text("GitHub repo", (e.get("source") or {}).get("repo", ""))
                if owner and repo:
                    self.db.update_custom(e["id"], {
                        "source_type": "github",
                        "urls": {"github": f"https://github.com/{owner}/{repo}",
                                 "curseforge": None, "mcmod": None,
                                 "bilibili": None, "other": []},
                        "source": {"owner": owner, "repo": repo, "asset_regex": "",
                                   "exclude_regex": wikimod.DEFAULT_EXCLUDE_REGEX}})
                    self.ui.ok("已更新GitHub源")
            elif a == 2:
                if self.ui.confirm(f"确认删除自定义源 {e['name_en'] or e['id']}?（不影响已装文件）"):
                    self.db.remove_custom(e["id"])
                    self.ui.ok("已删除")

    def _add_custom_source(self):
        kind = self.ui.choose("自定义源类型:", [
            "GitHub仓库（自动检查Release更新）",
            "本地文件夹目录（目录内jar作为版本来源，适合朋友分享的mod）",
            "手动维护（仅登记名字，替换文件后重扫识别版本）"])
        if kind is None:
            return
        name_en = self.ui.input_text("英文名（用于匹配jar文件名）", "")
        if not name_en:
            self.ui.error("英文名不能为空")
            return
        name_cn = self.ui.input_text("中文名（可选）", "")
        s = self.ui.choose("端别:", ["客户端", "服务端", "双端"])
        side = ("client", "server", "both")[s] if s is not None else "both"
        if kind == 0:
            owner = self.ui.input_text("GitHub owner", "")
            repo = self.ui.input_text("GitHub repo", "")
            if not owner or not repo:
                self.ui.error("owner/repo 不能为空")
                return
            entry = {"name_en": name_en, "name_cn": name_cn, "side": side,
                     "source_type": "github",
                     "github_url": f"https://github.com/{owner}/{repo}",
                     "source": {"owner": owner, "repo": repo, "asset_regex": "",
                                "exclude_regex": wikimod.DEFAULT_EXCLUDE_REGEX}}
        elif kind == 1:
            path = self.ui.input_text("本地目录路径", "")
            regex = self.ui.input_text("文件名匹配正则（可选，如 ^MyMod）", "")
            if not path:
                self.ui.error("目录不能为空")
                return
            entry = {"name_en": name_en, "name_cn": name_cn, "side": side,
                     "source_type": "local_folder",
                     "source": {"path": path, "name_regex": regex}}
        else:
            entry = {"name_en": name_en, "name_cn": name_cn, "side": side,
                     "source_type": "manual"}
        eid = self.db.add_custom(entry)
        self.ui.ok(f"已添加: {eid}")

    # ---------- 9 备份 ----------
    def do_backups(self):
        backups = updater.list_backups(self.cfg)
        if not backups:
            self.ui.info("暂无备份（更新mod时自动生成旧版本备份）")
            return
        options = []
        flat = []
        for side in SIDES:
            for mod_id, files in backups.get(side, {}).items():
                for f in files:
                    options.append(f"{SIDE_LABELS[side]} {mod_id}: {f.name}")
                    flat.append((side, mod_id, f))
        idx = self.ui.choose("选择备份（恢复该版本）:", options)
        if idx is None:
            return
        side, mod_id, f = flat[idx]
        if not self.ui.confirm(f"恢复 {SIDE_LABELS[side]} {mod_id} 到 {f.name}?（当前版本会另存备份）"):
            return
        r = updater.restore_backup(self.cfg, self.db, self.installed, side, f)
        if r["action"] == "restored":
            self.ui.ok(f"已恢复: {r['file']}")
        else:
            self.ui.error(r.get("error") or "恢复失败")

    # ---------- 10 设置 ----------
    def do_settings(self):
        while True:
            options = [
                f"客户端mods目录: {self.cfg.client_mods_dir or '（未设置）'}",
                f"服务端mods目录: {self.cfg.server_mods_dir or '（未设置）'}",
                f"GitHub Token: {'已配置' if self.cfg.github_token else '（未配置，匿名限额60次/时）'}",
                f"代理: {self.cfg.proxy or '（跟随系统）'}",
                f"检查结果缓存时长: {self.cfg.check_interval_hours} 小时",
                f"每mod保留备份数: {self.cfg.backup_keep}",
                f"GTNH整合包版本（兼容推荐用）: {self.cfg.data.get('gtnh_version') or '（未设置）'}",
                f"Wiki反爬Cookie: {'已配置' if self.cfg.wiki_cookie else '（未配置，站点开启Cloudflare验证时需要）'}",
                "恢复已排除/忽略的文件（重新显示）",
                "打开操作日志文件",
            ]
            idx = self.ui.choose("设置项:", options)
            if idx is None:
                return
            if idx == 0:
                p = self.ui.input_text("客户端mods目录（留空=不管理）",
                                       str(self.cfg.client_mods_dir or ""))
                self.cfg.set_mods_dir("client", p)
                self.ui.ok("已保存")
            elif idx == 1:
                p = self.ui.input_text("服务端mods目录（留空=不管理）",
                                       str(self.cfg.server_mods_dir or ""))
                self.cfg.set_mods_dir("server", p)
                self.ui.ok("已保存")
            elif idx == 2:
                t = self.ui.input_text("GitHub Token（https://github.com/settings/tokens 生成，"
                                       "留空=匿名）", self.cfg.github_token)
                self.cfg.data["github_token"] = t
                self.cfg.save()
                self.ui.ok("已保存")
            elif idx == 3:
                host = self.ui.input_text("代理地址（如 127.0.0.1，留空=跟随系统）",
                                          (self.cfg.proxy or {}).get("host", ""))
                if host:
                    port = self.ui.input_text("端口", "7890")
                    try:
                        self.cfg.data["proxy"] = {"host": host, "port": int(port)}
                    except ValueError:
                        self.ui.error("端口必须是数字，代理未保存")
                        continue
                else:
                    self.cfg.data["proxy"] = None
                self.cfg.save()
                self.ui.ok("已保存")
            elif idx == 4:
                h = self.ui.input_text("缓存时长（小时）", str(self.cfg.check_interval_hours))
                try:
                    self.cfg.data["check_interval_hours"] = float(h)
                    self.cfg.save()
                    self.ui.ok("已保存")
                except ValueError:
                    self.ui.error("无效数字")
            elif idx == 5:
                k = self.ui.input_text("备份保留数", str(self.cfg.backup_keep))
                try:
                    self.cfg.data["backup_keep"] = int(k)
                    self.cfg.save()
                    self.ui.ok("已保存")
                except ValueError:
                    self.ui.error("无效数字")
            elif idx == 6:
                v = self.ui.input_text("GTNH整合包版本（如 2.9.0）",
                                       self.cfg.data.get("gtnh_version", ""))
                self.cfg.data["gtnh_version"] = v
                self.cfg.save()
                self.ui.ok("已保存")
            elif idx == 7:
                self.do_wiki_cookie()
            elif idx == 8:
                ignored = self.cfg.data.get("ignored_files") or []
                if not ignored:
                    self.ui.info("没有被剔除/忽略的文件")
                    continue
                sel = self.ui.choose("选择要恢复显示的文件:", ignored)
                if sel is not None:
                    updater.unignore(self.cfg, ignored[sel])
                    self.ui.ok("已取消忽略")
            elif idx == 9:
                log_path = utils.log_file_path(self.cfg.data_dir)
                if log_path.exists():
                    os.startfile(log_path)  # 用默认编辑器打开
                    self.ui.ok(f"已打开日志: {log_path}")
                else:
                    self.ui.info("暂无操作日志（执行过安装/更新/开关等操作后生成）")

    def do_wiki_cookie(self):
        """wiki 站点开启 Cloudflare 人机验证时，导入浏览器验证后的 Cookie。"""
        while True:
            cur = self.cfg.wiki_cookie
            options = [
                f"设置/更新 Cookie（当前: {'已配置' if cur else '未配置'}）",
                "测试抓取",
                "清除",
                "返回",
            ]
            idx = self.ui.choose("Wiki反爬Cookie（cf_clearance 与浏览器 UA/出口IP 绑定）:", options)
            if idx in (None, 3):
                return
            if idx == 0:
                self.ui.info("获取：浏览器打开 wiki 通过验证 → F12 → Network → 刷新 → 第一个文档请求 →"
                             " Request Headers")
                cookie = self.ui.input_text("粘贴 Cookie 整行（留空取消）", "")
                if not cookie:
                    continue
                ua = self.ui.input_text("粘贴同一请求的 User-Agent 整行", "")
                parsed_cookie, parsed_ua = cookies.parse_paste(cookie)
                if parsed_cookie:
                    cookie = parsed_cookie  # 连头名/整段 cURL 粘贴时自动提取
                if parsed_ua and not ua:
                    ua = parsed_ua
                self.cfg.set_wiki_cookie(cookie, ua)
                self.ui.ok(f"已保存（解析出 {len(cookie.split(';'))} 个 cookie"
                           f"{'，UA 已自动提取' if parsed_ua and not ua else ''}）")
            elif idx == 1:
                self.ui.info("正在测试 wiki 抓取...")
                try:
                    mods, warnings = wikimod.fetch_and_parse(self.cfg)
                except Exception as e:
                    self.ui.error(f"抓取失败: {e}")
                    continue
                for w in warnings:
                    self.ui.warn(w)
                if mods:
                    self.ui.ok(f"成功：解析到 {len(mods)} 个mod")
                else:
                    self.ui.warn("抓取到内容但解析不到mod（Cookie 可能已过期或页面结构变更）")
            elif idx == 2:
                self.cfg.set_wiki_cookie("", "")
                self.ui.ok("已清除")

    def _check_dirs(self) -> bool:
        if not self.cfg.client_mods_dir and not self.cfg.server_mods_dir:
            self.ui.error("客户端/服务端 mods 目录均未设置，请先到菜单10设置")
            return False
        return True


def run(argv=None):
    argv = list(argv or sys.argv[1:])
    data_dir = utils.resolve_data_dir()
    app = CliApp(data_dir)
    if "--check" in argv:
        return run_check(app)
    if "--update-all" in argv:
        return run_update_all(app)
    app.main_menu()


def run_check(app) -> int:
    """非交互：检查更新（供计划任务）。出错时返回非0，任务计划才能感知失败。"""
    print("GTNH mod 检查更新...")
    try:
        results = updater.check_updates(app.cfg, app.db, app.installed)
    except Exception as e:
        print(f"检查失败: {e}")
        return 1
    app._print_check_results(results)
    n_err = sum(1 for r in results if r[3])
    return 1 if n_err else 0


def run_update_all(app) -> int:
    """非交互：更新全部。有失败项时返回非0。"""
    print("GTNH mod 全部更新...")
    try:
        results = updater.update_all(app.cfg, app.db, app.installed)
    except Exception as e:
        print(f"更新失败: {e}")
        return 1
    n_ok = n_fail = 0
    for r in results:
        if r["action"] == "updated":
            n_ok += 1
            print(f"已更新 {SIDE_LABELS[r['side']]} {r['name']}: {r['from']} -> {r['to']}")
        elif r["action"] == "manual":
            print(f"需手动 {SIDE_LABELS[r['side']]} {r['name']}: {r.get('note')}")
        elif r["action"] == "skipped_incompatible":
            print(f"跳过 {SIDE_LABELS[r['side']]} {r['name']}: {r.get('note')}")
        elif r["action"] != "uptodate":
            n_fail += 1
            print(f"失败 {SIDE_LABELS[r['side']]} {r['name']}: {r.get('error')}")
    print(f"完成：成功更新 {n_ok} 个")
    return 1 if n_fail else 0


if __name__ == "__main__":
    run()
