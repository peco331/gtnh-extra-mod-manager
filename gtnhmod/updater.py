"""编排层：注册表构建、检查更新、安装/更新、启用禁用、未受管管理。

壳（CLI/GUI）只调本模块；所有交互经 UIProtocol（见 ui.py）。
"""
import os
import re
import shutil
from pathlib import Path

from . import SIDES, SIDE_LABELS, utils
from . import downloader, installed as installed_mod, scanner, sources
from . import wiki as wikimod
from .sources import Source, UpdateInfo
from .versions import (VersionParseError, compare, order_key, parse_version,
                       split_mc_mod_version)

IGNORED_HINT = "（已忽略）"


def _log_op(cfg, msg: str) -> None:
    """写操作日志（data/logs/operations.log），失败静默。"""
    utils.append_log(cfg.data_dir, msg)


# ---------- GTNH 版本兼容 ----------

def _opt_parse(s):
    """解析版本；空/不可解析返回 None。"""
    if not s:
        return None
    try:
        return parse_version(s)
    except VersionParseError:
        return None


def _mod_pattern_match(pattern: str, version: str) -> bool:
    """版本模式匹配：'0.2.0pXX' 匹配 '0.2.0p44'；'1.9' 匹配 '1.9'/'1.9.1'。"""
    p = re.sub(r"p\d+|pxx", "p", (pattern or "").strip().lstrip("v").lower())
    v = re.sub(r"p\d+|pxx", "p", (version or "").strip().lstrip("v").lower())
    if not p or not v:
        return False
    return p == v or v.startswith(p + ".") or v.startswith(p + "-")


def match_compat(compat_rules: list, gtnh_version: str, mod_version: str) -> str:
    """判断某 mod 版本是否适配当前 GTNH 版本。

    返回 compatible / incompatible / unknown（无规则或无法解析时）。
    """
    if not gtnh_version or not compat_rules:
        return "unknown"
    g = _opt_parse(gtnh_version)
    v = _opt_parse(mod_version)
    if g is None or v is None:
        return "unknown"
    known, bad = False, False
    for r in compat_rules:
        if r["kind"] == "range":
            if not _mod_pattern_match(r["mod_ver"], mod_version):
                continue
            known = True
            lo, hi = _opt_parse(r.get("min")), _opt_parse(r.get("max"))
            if (lo is not None and compare(g, lo) < 0) or (hi is not None and compare(g, hi) > 0):
                bad = True
        elif r["kind"] == "gtnh_min":
            gv = _opt_parse(r.get("gtnh"))
            if gv is None or compare(g, gv) < 0:
                continue
            known = True
            mm = _opt_parse(r.get("mod_min"))
            if mm is not None and compare(v, mm) < 0:
                bad = True
        elif r["kind"] == "gtnh_max":
            gv = _opt_parse(r.get("gtnh"))
            if gv is None or compare(g, gv) > 0:
                continue
            known = True
            mm = _opt_parse(r.get("mod_max"))
            if mm is not None and compare(v, mm) > 0:
                bad = True
    if bad:
        return "incompatible"
    return "compatible" if known else "unknown"


def list_install_options(entry: dict, cfg, db=None, *, force: bool = False) -> tuple:
    """列出可安装/更新的版本（最新在前），并标注适配性与推荐。

    返回 (options, error)：error 非空时 options 为空（网络/限流等原因），
    调用方应把真实原因展示给用户，而不是把错误当成"无版本可用"。
    传入 db 时会从下载资产文件名学习该mod的真实jar命名（存入aliases），
    并缓存最新版发布时间（供「可添加MOD」按更新时间排序）。
    """
    source = Source.from_entry(entry, cfg)
    try:
        options = source.list_versions(force=force)
    except Exception as e:
        return [], str(e)
    # 从首个有资产的版本学习真实jar命名（供以后扫描精确匹配）
    if db is not None:
        for o in options:
            if o.candidates:
                _learn_asset_name(db, entry, o.candidates[0].file_name)
                break
        # 缓存最新版发布时间（供「可添加MOD」按更新时间排序）
        if options and options[0].published_at:
            _touch_release_date(db, entry, options[0].published_at)
    gtnh = (cfg.data.get("gtnh_version") or "").strip()
    compat_rules = entry.get("compat") or []
    result = []
    for i, o in enumerate(options):
        # 版本自述（GitHub release body）里的兼容说明优先于wiki条目级规则
        body_rules = wikimod.parse_compat(o.body or "") if o.body else []
        if body_rules:
            compat = match_compat(body_rules, gtnh, o.version)
        else:
            compat = match_compat(compat_rules, gtnh, o.version)
        result.append({
            "version": o.version,
            "tag": o.tag or o.version,
            "body": o.body,
            "candidates": o.candidates,
            "compat": compat,
            "recommended": False,
            "latest": i == 0,
            "prerelease": o.prerelease,
            "published_at": o.published_at,
        })
    if not result:
        return [], None
    # 推荐：第一个确认适配的；全部 unknown 时推荐最新
    for r in result:
        if r["compat"] == "compatible":
            r["recommended"] = True
            break
    else:
        result[0]["recommended"] = True
    return result, None


def get_available_versions(entry: dict, cfg, db=None, *, force: bool = False) -> list:
    """兼容包装：只返回版本列表（出错时为空列表）。新代码请用 list_install_options。"""
    return list_install_options(entry, cfg, db, force=force)[0]


def _pick_default_option(options: list, old_version: str | None = None) -> tuple:
    """默认（不指定版本）路径选版：最新正式版优先，跳过不兼容版本。

    旧逻辑用 releases/latest（不含 prerelease）；仓库只有 prerelease 时
    退回全量候选（对齐旧 _check_via_releases 的兜底行为）。
    同版本号的变体构建互为判等（如 v1.85 / v1.85-Multi / v1.85-Multiplayer）：
    排序最高者若与当前已装版本判等，但同组里有严格更新的变体，优先选它。
    返回 (选中option|None, 说明note)；None 表示所有候选均不兼容。
    """
    latest = options[0]
    pool = [o for o in options if not o.get("prerelease")] or options
    chosen = next((o for o in pool if o["compat"] != "incompatible"), None)
    if chosen is None:
        return None, ""
    if old_version:
        try:
            # 同组判等的变体里取"最大"的（order_key 全序），而不是列表顺序第一个
            group = [o for o in pool
                     if o["compat"] != "incompatible"
                     and compare(o["version"], chosen["version"]) == 0]
            if group:
                chosen = max(group, key=lambda o: order_key(o["version"]))
        except VersionParseError:
            pass
    if chosen["version"] != latest["version"]:
        try:
            same_ver = compare(chosen["version"], latest["version"]) == 0
        except VersionParseError:
            same_ver = False
        if latest["compat"] == "incompatible":
            note = (f"最新版 {latest['version']} 按 wiki 兼容表可能与当前 GTNH 不兼容，"
                    f"已选择标注适配的 {chosen['version']}")
        elif same_ver:
            note = f"同版本号存在多个构建，已选择新于当前版本的 {chosen['version']}"
        else:
            note = (f"最新版 {latest['version']} 为 prerelease，"
                    f"已选择最新正式版 {chosen['version']}")
        return chosen, note
    return chosen, ""


def scan_sides(cfg, db) -> dict:
    """扫描两端 mods 目录并匹配 db，返回 {side: [InstalledFile]}。"""
    result = {}
    for side in SIDES:
        folder = cfg.mods_dir(side)
        files = scanner.scan_folder(folder) if folder else []
        scanner.match_all(files, db.all())
        result[side] = files
    return result


def unmatched_files(cfg, db) -> dict:
    """两端未匹配且未忽略的 jar，返回 {side: [InstalledFile]}。"""
    ignored = set(cfg.data.get("ignored_files") or [])
    result = {}
    for side, files in scan_sides(cfg, db).items():
        result[side] = [f for f in files if not f.mod_id and f.file_name not in ignored]
    return result


def reconcile_installed(cfg, db, installed) -> int:
    """用磁盘扫描校正 installed.json（清理手动删除jar留下的残留记录）。

    在程序启动时调用一次。只处理已配置目录的端别——未配置目录时扫描结果
    为空，若照常校正会把该端别的记录全部误删。返回清理的记录数。
    """
    scan = {side: files for side, files in scan_sides(cfg, db).items()
            if cfg.mods_dir(side)}
    if not scan:
        return 0
    before = sum(len(installed.all_ids(side)) for side in scan)
    scanner.reconcile(scan, installed)
    removed = before - sum(len(installed.all_ids(side)) for side in scan)
    if removed:
        _log_op(cfg, f"校正已安装记录：清理 {removed} 条磁盘上已不存在的记录")
    return removed


def startup_maintenance(cfg, db, installed) -> None:
    """程序启动维护：校正已安装记录、清理积压备份、清扫孤儿临时文件。"""
    try:
        reconcile_installed(cfg, db, installed)
        prune_all_backups(cfg)
        n = utils.clean_orphan_tmp(cfg.cache_dir)
        if n:
            _log_op(cfg, f"清扫孤儿下载临时文件 {n} 个")
    except OSError as e:
        _log_op(cfg, f"启动维护失败: {e}")


def build_registry(cfg, db, installed) -> dict:
    """构建已安装注册表：{side: {mod_id: state}}，以磁盘扫描为准。

    已剔除（ignored_files）的 jar 不出现；同一mod多个jar时取版本最高者，
    其余记入 state["duplicates"] 供界面提示。
    """
    ignored = set(cfg.data.get("ignored_files") or [])
    reg = {}
    for side, files in scan_sides(cfg, db).items():
        by_mod: dict = {}
        for f in files:
            if f.file_name in ignored or not f.mod_id:
                continue
            by_mod.setdefault(f.mod_id, []).append(f)
        side_reg = {}
        for mod_id, fs in by_mod.items():
            ordered = _sort_files_by_version(fs)
            f = ordered[0]
            entry = db.get(f.mod_id) or {}
            inst = installed.get(side, f.mod_id) or {}
            latest = inst.get("last_remote_version")
            status = "disabled" if not f.enabled else "installed"
            if f.enabled and latest and f.version:
                try:
                    if compare(latest, f.version) > 0:
                        status = "update_avail"
                        # 最新版存在但按 wiki 兼容规则不适配当前整合包 → 单独状态，
                        # 与"可更新"（全部更新会自动安装的）区分开，列表不再误导
                        compat = match_compat(entry.get("compat") or [],
                                              (cfg.data.get("gtnh_version") or "").strip(),
                                              latest)
                        if compat == "incompatible":
                            status = "update_incompat"
                except VersionParseError:
                    pass
            st = {
                "mod_id": f.mod_id,
                "name_en": entry.get("name_en") or f.mod_id,
                "name_cn": entry.get("name_cn") or "",
                "group": entry.get("group") or "?",
                "category": entry.get("category") or "?",
                "want_side": entry.get("side") or "both",
                "file_name": f.file_name,
                "version": f.version or "未知",
                "enabled": f.enabled,
                "locked": bool(inst.get("locked")),
                "status": status,
                "latest_version": latest,
                # 本地更新时间：jar 文件时间与工具记录时间取较新者
                "install_time": _local_update_str(f.path, inst),
            }
            if len(ordered) > 1:
                st["duplicates"] = [x.file_name for x in ordered[1:]]
            side_reg[f.mod_id] = st
        reg[side] = side_reg
    return reg


def build_merged_registry(cfg, db, installed) -> list:
    """合并注册表：一个 mod 一行（不区分端别重复显示）。

    安装端别分三类：both=双端 / client=仅客户端 / server=仅服务端。
    每行: {mod_id, name_en, name_cn, group, category, want_side,
          install_side, sides:{side: state}, status, locked, latest_version}
    """
    reg = build_registry(cfg, db, installed)
    merged: dict = {}
    for side in SIDES:
        for mod_id, st in reg[side].items():
            m = merged.setdefault(mod_id, {
                "mod_id": mod_id, "name_en": st["name_en"], "name_cn": st["name_cn"],
                "group": st["group"], "category": st["category"],
                "want_side": st["want_side"], "sides": {},
            })
            m["sides"][side] = st
    out = []
    for m in merged.values():
        sides = m["sides"]
        dups = {s: st.get("duplicates") for s, st in sides.items() if st.get("duplicates")}
        if dups:
            m["duplicates"] = dups
        if "client" in sides and "server" in sides:
            m["install_side"] = "both"
        elif "client" in sides:
            m["install_side"] = "client"
        else:
            m["install_side"] = "server"
        statuses = [st["status"] for st in sides.values()]
        if "update_avail" in statuses:
            m["status"] = "update_avail"
        elif "update_incompat" in statuses:
            m["status"] = "update_incompat"
        elif all(s == "disabled" for s in statuses):
            m["status"] = "disabled"
        else:
            m["status"] = "installed"
        m["locked"] = any(st["locked"] for st in sides.values())
        m["latest_version"] = next(
            (st["latest_version"] for st in sides.values() if st["latest_version"]), "")
        # 安装时间取两端最新者（ISO 字符串可直接比较排序）
        m["install_time"] = max(
            (st["install_time"] for st in sides.values() if st["install_time"]), default="")
        out.append(m)
    out.sort(key=lambda m: m["name_en"].lower())
    return out


def _local_update_str(path: Path, inst: dict) -> str:
    """本地更新时间：jar 文件时间与工具记录时间取较新者（ISO 字符串可直接比较）。

    文件时间覆盖手动替换的场景；工具记录（updated_at/install_date）覆盖
    restore/回滚等 copy2 保留旧 mtime 的场景。
    """
    try:
        mtime = utils.fmt_ts(path.stat().st_mtime)
    except OSError:
        mtime = ""
    rec = (inst.get("updated_at") or inst.get("install_date") or "")[:16]
    return max((x for x in (mtime, rec) if x), default="")


def _sort_files_by_version(files: list) -> list:
    """按文件版本降序排列（无法解析版本的排最后，保持原序）。"""
    parseable = [f for f in files if f.version and _opt_parse(f.version)]
    unparse = [f for f in files if not (f.version and _opt_parse(f.version))]
    parseable.sort(key=lambda f: parse_version(f.version).key(), reverse=True)
    return parseable + unparse


def find_installed_files(cfg, db, side: str, mod_id: str) -> list:
    """某端别某 mod 在磁盘上的所有匹配文件（版本降序）。"""
    folder = cfg.mods_dir(side)
    if not folder:
        return []
    files = scanner.scan_folder(folder)
    scanner.match_all(files, db.all())
    hits = [f for f in files if f.mod_id == mod_id]
    return _sort_files_by_version(hits)


def find_installed_file(cfg, db, side: str, mod_id: str):
    """找到某端别某 mod 当前在磁盘上的代表文件（版本最高者）。失败返回 None。"""
    hits = find_installed_files(cfg, db, side, mod_id)
    return hits[0] if hits else None


def backup_and_remove_extra(cfg, db, side: str, mod_id: str, extra) -> bool:
    """把同mod的冗余jar备份后移除（防双jar冲突）。返回是否成功。"""
    bak_dir = cfg.backup_dir / side / mod_id
    bak_dir.mkdir(parents=True, exist_ok=True)
    bak = downloader._unique_backup_path(bak_dir, extra.file_name)
    try:
        shutil.copy2(extra.path, bak)
        extra.path.unlink()
        _log_op(cfg, f"{SIDE_LABELS[side]} 清理重复jar {extra.file_name}（已备份）")
        return True
    except PermissionError:
        _log_op(cfg, f"{SIDE_LABELS[side]} 清理重复jar失败（文件被占用，请关闭游戏/服务端后重试）: {extra.file_name}")
        return False
    except OSError as e:
        _log_op(cfg, f"{SIDE_LABELS[side]} 清理重复jar失败: {extra.file_name}（{e}）")
        return False


def cleanup_duplicates(cfg, db, installed, mod_id: str) -> dict:
    """清理某mod在所有端别的重复jar。返回结果dict。

    保留文件：优先工具记录（installed.json file_name，回滚后的版本意图），
    记录缺失/文件不在磁盘时退回版本最高者。
    """
    cleaned, skipped, kept = [], [], []
    for side in SIDES:
        hits = find_installed_files(cfg, db, side, mod_id)
        if not hits:
            continue
        keep = hits[0]  # 版本最高者
        rec_name = (installed.get(side, mod_id) or {}).get("file_name")
        for h in hits:
            if h.file_name == rec_name:
                keep = h
                break
        kept.append(f"{SIDE_LABELS[side]}: {keep.file_name}")
        for extra in hits:
            if extra is keep:
                continue
            if backup_and_remove_extra(cfg, db, side, mod_id, extra):
                cleaned.append(f"{SIDE_LABELS[side]}: {extra.file_name}")
            else:
                skipped.append(f"{SIDE_LABELS[side]}: {extra.file_name}")
    return {"action": "cleaned" if cleaned else "none",
            "cleaned": cleaned, "skipped": skipped, "kept": kept}


def auto_install_sides(entry: dict, cfg) -> tuple:
    """按mod端别声明自动决定安装到哪些端别。返回 (sides列表, 提示)。

    both → 所有已设置目录的端别；client/server → 对应端别（目录未设置则提示）。
    """
    want = entry.get("side") or "both"
    note = ""
    if want == "both":
        sides = [s for s in SIDES if cfg.mods_dir(s) and cfg.mods_dir(s).is_dir()]
        missing = [SIDE_LABELS[s] for s in SIDES
                   if not (cfg.mods_dir(s) and cfg.mods_dir(s).is_dir())]
        if missing:
            note = f"{'、'.join(missing)}mods目录未设置，已跳过该端别"
    else:
        d = cfg.mods_dir(want)
        if d and d.is_dir():
            sides, note = [want], ""
        else:
            sides, note = [], f"{SIDE_LABELS[want]}mods目录未设置，无法安装"
    return sides, note


def set_name_cn(db, mod_id: str, name_cn: str) -> dict:
    """编辑条目中文名（可新增）。留空则恢复wiki原名。"""
    entry = db.get(mod_id)
    if not entry:
        return {"action": "error", "error": f"未知mod: {mod_id}"}
    name_cn = (name_cn or "").strip()
    db.update_entry(mod_id, {"name_cn": name_cn, "name_cn_override": bool(name_cn)})
    utils.append_log(db.path.parent,
                     f"编辑中文名 {entry.get('name_en') or mod_id} → {name_cn or '（恢复wiki原名）'}")
    return {"action": "saved", "name_cn": name_cn}


def entry_links(entry: dict) -> list:
    """条目的全部下载链接（含标签）。老数据无links字段时从分类url兜底重建。"""
    links = list((entry.get("urls") or {}).get("links") or [])
    if links:
        return links
    urls = entry.get("urls") or {}
    for key, label in (("github", "github"), ("curseforge", "curseforge"),
                       ("mcmod", "mcmod"), ("bilibili", "bilibili")):
        if urls.get(key):
            links.append({"url": urls[key], "label": label})
    return links


def _side_warning(entry: dict, side: str) -> str:
    want = entry.get("side") or "both"
    if want != "both" and want != side:
        return (f"注意：该mod标注为{SIDE_LABELS.get(want, want)}专用，"
                f"当前选择安装到{SIDE_LABELS[side]}")
    return ""


# ---------- 检查更新 ----------

def refresh_release_dates(cfg, db, *, progress_cb=None) -> dict:
    """刷新所有 GitHub 源的最新版发布时间，供可添加列表排序。

    force=False，优先使用 GitHub 缓存，只有缓存过期/不存在时才请求网络。
    返回 {updated, failed} 数量。
    """
    updated = failed = 0
    for entry in db.all():
        if entry.get("source_type") != "github":
            continue
        try:
            info = Source.from_entry(entry, cfg).check(None, force=False)
            if info.published_at and entry.get("release_date") != info.published_at:
                entry["release_date"] = info.published_at
                updated += 1
            if progress_cb:
                progress_cb(entry.get("id"), info.published_at)
        except Exception:
            failed += 1
            if progress_cb:
                progress_cb(entry.get("id"), None)
    if updated:
        db.save()
    return {"updated": updated, "failed": failed}


def check_updates(cfg, db, installed, *, sides=SIDES, force: bool = False,
                  progress_cb=None, only=None) -> list:
    """对已安装（未锁定）mod 逐一查询最新版本。only=指定mod_id集合时只查这些。

    返回 [(side, mod_id, UpdateInfo|None, error_str|None)]。
    """
    results = []
    db_dirty = False
    inst_dirty = False
    ignored = set(cfg.data.get("ignored_files") or [])
    for side in sides:
        folder = cfg.mods_dir(side)
        if not folder:
            continue
        files = scanner.scan_folder(folder)
        scanner.match_all(files, db.all())
        for f in files:
            if not f.mod_id or f.file_name in ignored:
                continue
            if only is not None and f.mod_id not in only:
                continue
            if not f.enabled:
                continue  # 已禁用的不查，省 API 配额
            entry = db.get(f.mod_id)
            if not entry:
                continue
            inst = installed.get(side, f.mod_id) or {}
            if inst.get("locked"):
                continue
            source = Source.from_entry(entry, cfg)
            try:
                info = source.check(f.version, force=force)
                if info.latest_version:
                    # 批量模式：收尾一次性保存（N个mod = N次全文件重写太浪费）
                    installed.touch_checked(side, f.mod_id, info.latest_version,
                                            remote_date=info.published_at, save=False)
                    inst_dirty = True
                if info.candidates:
                    _learn_asset_name(db, entry, info.candidates[0].file_name)
                if info.published_at and entry.get("release_date") != info.published_at:
                    entry["release_date"] = info.published_at
                    db_dirty = True
                results.append((side, f.mod_id, info, None))
            except Exception as e:  # 单个失败不影响整体
                results.append((side, f.mod_id, None, str(e)))
            if progress_cb:
                progress_cb(side, f.mod_id)
    if inst_dirty:
        try:
            installed.save()
        except Exception as e:  # 保存失败不丢检查结果，仅记录（下次会重查）
            _log_op(cfg, f"保存已安装记录失败: {e}")
    if db_dirty:
        try:
            db.save()
        except Exception as e:
            _log_op(cfg, f"保存数据库失败: {e}")
    return results


def bindable_links(entry: dict) -> list:
    """可绑定为下载源的链接（GitHub 仓库页 / CurseForge 页面）。"""
    return [l for l in entry_links(entry)
            if "github.com" in l["url"] or "curseforge.com" in l["url"]]


def summarize_check(results: list, reg: dict) -> dict:
    """检查更新结果计数，按 mod 去重（双端不重复计）：
    {update, uptodate, unknown, manual, error}。

    同一 mod 多端结果不一致时取最有行动价值的状态：
    可更新 > 出错 > 无法判断 > 需手动 > 已最新。
    reg 为 build_registry 结果（判定"可更新"需要当前已装版本号）。
    """
    per_mod: dict = {}
    for side, mod_id, info, err in results:
        st = per_mod.setdefault(mod_id, set())
        if err:
            st.add("error")
        elif not info or not info.latest_version:
            st.add("manual")
        else:
            cur = (reg.get(side, {}).get(mod_id) or {}).get("version")
            s = version_status(cur, info.latest_version)
            st.add(s if s in ("update", "uptodate") else "unknown")
    counts = {"update": 0, "uptodate": 0, "unknown": 0, "manual": 0, "error": 0}
    for st in per_mod.values():
        for key in ("update", "error", "unknown", "manual", "uptodate"):
            if key in st:
                counts[key] += 1
                break
    return counts


def version_status(current, latest) -> str:
    """比较已装版本与远端版本：update=可更新 / uptodate=已最新 / unknown=无法判断。"""
    if not current or not latest:
        return "unknown"
    try:
        return "update" if compare(latest, current) > 0 else "uptodate"
    except VersionParseError:
        return "unknown"


# ---------- 安装 / 更新 ----------

def install_mod(cfg, db, installed, mod_id: str, side: str, *,
                version: str = None, progress_cb=None) -> dict:
    """安装 mod 到指定端别。version 指定时安装该版本（否则最新兼容版）。

    返回结果 dict（action: installed/manual/skipped_incompatible/error）。
    """
    entry = db.get(mod_id)
    if not entry:
        return {"action": "error", "error": f"未知mod: {mod_id}"}
    folder = cfg.mods_dir(side)
    if not folder or not folder.is_dir():
        return {"action": "error",
                "error": f"{SIDE_LABELS[side]}mods目录未设置或不存在，请先在设置中配置"}
    warn = _side_warning(entry, side)
    name = entry.get("name_en") or mod_id
    gtnh = (cfg.data.get("gtnh_version") or "").strip()
    options, err = list_install_options(entry, cfg, db, force=True)
    if err:
        _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {name} 失败: {err}")
        return {"action": "error", "error": f"查询版本列表失败: {err}"}
    if version is not None:
        opt = next((o for o in options if o["version"] == version), None)
        if not opt:
            _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {name} 失败: 版本 {version} 不可用")
            return {"action": "error", "error": f"版本 {version} 不可用（列表可能已过期，请重试）"}
        if not opt["candidates"]:
            return {"action": "manual", "note": f"版本 {version} 无自动下载资产，需手动下载",
                    "entry": entry, "warning": warn}
        cand = opt["candidates"][0]
        info = UpdateInfo(opt["version"], opt["candidates"], opt["body"],
                          utils.now_str(), f"已选版本 {version}",
                          opt.get("published_at"))
    else:
        if not options:
            # 无任何版本：取源的说明（手动源/CurseForge 的引导文案）
            try:
                info0 = Source.from_entry(entry, cfg).check(None, force=True)
            except Exception:
                info0 = None
            note = (info0.note if info0 and info0.note else "") or "该mod无自动下载渠道"
            return {"action": "manual", "note": note, "entry": entry, "warning": warn}
        chosen, note = _pick_default_option(options)
        if chosen is None:
            msg = (f"现有版本按 wiki 兼容表可能与 GTNH {gtnh} 均不兼容，未自动安装"
                   f"（最新版 {options[0]['version']}），可在版本列表中手动选择")
            _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {name}: {msg}")
            return {"action": "skipped_incompatible", "note": msg,
                    "entry": entry, "warning": warn}
        if not chosen["candidates"]:
            return {"action": "manual", "note": note or "该mod无自动下载渠道",
                    "entry": entry, "warning": warn}
        cand = chosen["candidates"][0]
        info = UpdateInfo(chosen["version"], chosen["candidates"], chosen["body"],
                          utils.now_str(), note, chosen.get("published_at"))
    old = find_installed_file(cfg, db, side, mod_id)
    try:
        dest, unremoved = downloader.update_with_backup(
            cand, folder, cfg.backup_dir / side / mod_id,
            old_file=old.path if old else None, backup_keep=cfg.backup_keep,
            progress_cb=progress_cb, proxy=cfg.proxy, dl_cache_dir=cfg.cache_dir)
    except downloader.FileBusyError as e:
        _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {entry.get('name_en') or mod_id} 失败: {e}")
        return {"action": "error", "error": str(e)}
    except Exception as e:
        _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {entry.get('name_en') or mod_id} 失败: {e}")
        return {"action": "error", "error": f"下载/安装失败: {e}"}
    ver = split_mc_mod_version(dest.stem)[2] or info.latest_version or "?"
    installed.set(side, mod_id, file_name=dest.name, parsed_version=ver,
                  install_date=utils.now_str(), updated_at=utils.now_str(),
                  last_remote_version=info.latest_version, last_checked=utils.now_str())
    _learn_asset_name(db, entry, dest.name)
    _log_op(cfg, f"{SIDE_LABELS[side]} 安装 {entry.get('name_en') or mod_id} v{ver}（{dest.name}）")
    leftover = _cleanup_extras(cfg, db, side, mod_id, dest, unremoved)
    result = {"action": "installed", "file": dest.name, "version": ver,
              "warning": warn, "note": info.note, "body": info.release_body}
    if leftover:
        result["leftover"] = leftover
    return result


def current_source_url(entry: dict) -> str:
    """条目当前绑定的下载源链接（github仓库页/curseforge页）；无则空串。

    返回绑定时的原始链接（urls["github"]，可能是 releases 等子路径），而非从
    owner/repo 重建的仓库根——否则绑定子路径后，"← 当前绑定"标记会对不上。
    旧数据 urls 里没有时回退到重建。
    """
    src = entry.get("source") or {}
    urls = entry.get("urls") or {}
    if entry.get("source_type") == "github":
        if urls.get("github"):
            return urls["github"]
        if src.get("owner"):
            return f"https://github.com/{src['owner']}/{src['repo']}"
        return ""
    if entry.get("source_type") == "curseforge":
        return urls.get("curseforge") or ""
    return ""


def bind_source(db, mod_id: str, url: str) -> dict:
    """把某mod的下载源绑定到指定链接（GitHub仓库/CurseForge页面）。

    绑定记录 source_override 标记，刷新wiki数据后依然保留。
    返回 {action: bound/error, ...}。
    """
    entry = db.get(mod_id)
    if not entry:
        return {"action": "error", "error": f"未知mod: {mod_id}"}
    url = (url or "").strip().rstrip("/")
    fields = {"source_override": True, "urls": dict(entry.get("urls") or {})}
    if "github.com" in url:
        repo = wikimod.github_repo_from_url(url)
        if not repo:
            return {"action": "error", "error": "无法从该链接解析出 GitHub 仓库（owner/repo）"}
        old_src = entry.get("source") or {}
        # 重绑同一仓库时保留用户配置的 tag 过滤（如只取带 GTNH 的版本）
        tag_regex = (old_src.get("tag_regex") or ""
                     if old_src.get("owner") == repo[0] and old_src.get("repo") == repo[1]
                     else "")
        fields["source_type"] = "github"
        fields["source"] = {"owner": repo[0], "repo": repo[1], "asset_regex": "",
                            "exclude_regex": wikimod.DEFAULT_EXCLUDE_REGEX,
                            "tag_regex": tag_regex}
        fields["urls"]["github"] = url
    elif "curseforge.com" in url:
        fields["source_type"] = "curseforge"
        fields["source"] = {}
        fields["urls"]["curseforge"] = url
    else:
        return {"action": "error", "error": "仅支持绑定 GitHub 仓库或 CurseForge 页面链接"}
    # 绑定的链接始终进入链接列表（否则选择对话框里看不到/标不住当前绑定）
    fields["urls"]["links"] = list((entry.get("urls") or {}).get("links") or [])
    if not any(l.get("url", "").rstrip("/") == url for l in fields["urls"]["links"]):
        fields["urls"]["links"].append({"url": url, "label": fields["source_type"]})
    db.update_entry(mod_id, fields)
    entry = db.get(mod_id)
    utils.append_log(db.path.parent,
                     f"绑定下载源 {entry.get('name_en') or mod_id} → {url}")
    return {"action": "bound", "url": url, "source_type": fields["source_type"]}


def _learn_asset_name(db, entry: dict, file_name: str) -> None:
    """从下载资产文件名学习该mod的真实jar命名（存入aliases，供扫描匹配）。

    例如资产 "GTNHModify_CutCorners-v1.3.17+2.9.0-beta-1.jar"
    → 记住名字段 "GTNHModify_CutCorners"。
    """
    if not file_name:
        return
    stem = file_name[:-4] if file_name.lower().endswith(".jar") else file_name
    name, mc, ver = split_mc_mod_version(stem)
    if name and len(scanner.normalize_name(name)) >= scanner.MIN_FUZZY_LEN:
        db.add_alias(entry["id"], name)


def _touch_release_date(db, entry: dict, published_at) -> None:
    """把最新版发布时间缓存进条目（供「可添加MOD」按更新时间排序）。"""
    if not published_at or entry.get("release_date") == published_at:
        return
    entry["release_date"] = published_at
    db.save()


def _cleanup_extras(cfg, db, side: str, mod_id: str, dest: Path,
                    failed: list = None) -> list:
    """新文件就位后，把同mod的其他jar备份并移除（防双jar冲突）。

    failed 传入已知未能移除的文件名（重试成功的会从中移除）。
    返回最终未能移除的文件名列表（文件被占用等）。
    """
    failed = list(failed or [])
    for extra in find_installed_files(cfg, db, side, mod_id):
        if extra.path.resolve() == dest.resolve():
            continue
        if backup_and_remove_extra(cfg, db, side, mod_id, extra):
            if extra.file_name in failed:
                failed.remove(extra.file_name)
        elif extra.file_name not in failed:
            failed.append(extra.file_name)
    return failed


def update_mod(cfg, db, installed, mod_id: str, side: str, *,
               version: str = None, progress_cb=None, prefetched=None) -> dict:
    """更新已安装 mod。version 指定时更新到该版本（否则最新兼容版）。

    prefetched: (options, err) 预取的版本列表——update_all 逐mod复用一次查询，
    双端不再各查一遍。返回结果 dict（action: updated/uptodate/manual/
    skipped_incompatible/error）。
    """
    entry = db.get(mod_id)
    if not entry:
        return {"action": "error", "error": f"未知mod: {mod_id}"}
    warn = _side_warning(entry, side)
    folder = cfg.mods_dir(side)
    if not folder or not folder.is_dir():
        return {"action": "error",
                "error": f"{SIDE_LABELS[side]}mods目录未设置或不存在"}
    old = find_installed_file(cfg, db, side, mod_id)
    if not old:
        return {"action": "error", "error": f"{SIDE_LABELS[side]}未找到已安装文件"}
    gtnh = (cfg.data.get("gtnh_version") or "").strip()
    if prefetched is not None:
        options, err = prefetched
    else:
        options, err = list_install_options(entry, cfg, db, force=True)
    if err:
        _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id} 失败: {err}")
        return {"action": "error", "error": f"查询版本列表失败: {err}", "warning": warn}
    if version is not None:
        if old.version and version == old.version:
            return {"action": "uptodate", "version": old.version, "note": "已是指定版本"}
        opt = next((o for o in options if o["version"] == version), None)
        if not opt:
            _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id} 失败: 版本 {version} 不可用")
            return {"action": "error", "error": f"版本 {version} 不可用（列表可能已过期，请重试）",
                    "warning": warn}
        if not opt["candidates"]:
            return {"action": "manual", "note": f"版本 {version} 无自动下载资产，需手动下载",
                    "entry": entry, "warning": warn}
        info = UpdateInfo(opt["version"], opt["candidates"], opt["body"],
                          utils.now_str(), f"已选版本 {version}",
                          opt.get("published_at"))
    else:
        if not options:
            try:
                info0 = Source.from_entry(entry, cfg).check(None, force=True)
            except Exception:
                info0 = None
            note = (info0.note if info0 and info0.note else "") or "该mod无自动下载渠道"
            return {"action": "manual", "note": note, "entry": entry, "warning": warn}
        latest = options[0]
        chosen, note = _pick_default_option(options, old_version=old.version)
        if chosen is None:
            msg = (f"现有版本按 wiki 兼容表可能与 GTNH {gtnh} 均不兼容，未自动更新"
                   f"（最新版 {latest['version']}），可在版本列表中手动选择")
            _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id}: {msg}")
            return {"action": "skipped_incompatible", "note": msg, "entry": entry,
                    "warning": warn}
        if old.version:
            try:
                if compare(chosen["version"], old.version) <= 0:
                    if compare(latest["version"], old.version) > 0:
                        # 有更新的版本但 wiki 兼容表未标注适配——如实说明，而不是"已是最新"
                        note = (note or f"最新版 {latest['version']} 按 wiki 兼容表"
                                f"可能与当前 GTNH {gtnh} 不兼容，保持 v{old.version}；"
                                f"需要的话在版本选择器中手动安装")
                    return {"action": "uptodate", "version": old.version,
                            "note": note or "已是最新版本"}
            except VersionParseError:
                # 版本格式无法比较（如 dev-build 类 tag）：不盲目重装，交用户判断
                return {"action": "manual",
                        "note": (f"版本格式无法比较（已装 {old.version}，最新 "
                                 f"{latest['version']}），请手动判断"),
                        "entry": entry, "warning": warn}
        if not chosen["candidates"]:
            return {"action": "manual", "note": note or "该版本无自动下载资产，需手动下载",
                    "entry": entry, "warning": warn}
        info = UpdateInfo(chosen["version"], chosen["candidates"], chosen["body"],
                          utils.now_str(), note, chosen.get("published_at"))
    cand = info.candidates[0]
    try:
        dest, unremoved = downloader.update_with_backup(
            cand, folder, cfg.backup_dir / side / mod_id,
            old_file=old.path, backup_keep=cfg.backup_keep,
            progress_cb=progress_cb, proxy=cfg.proxy, dl_cache_dir=cfg.cache_dir)
    except downloader.FileBusyError as e:
        _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id} 失败: {e}")
        return {"action": "error", "error": str(e)}
    except Exception as e:
        _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id} 失败: {e}")
        return {"action": "error", "error": f"下载/更新失败: {e}", "warning": warn}
    ver = split_mc_mod_version(dest.stem)[2] or info.latest_version or "?"
    installed.set(side, mod_id, file_name=dest.name, parsed_version=ver,
                  updated_at=utils.now_str(),
                  last_remote_version=info.latest_version, last_checked=utils.now_str())
    _learn_asset_name(db, entry, dest.name)
    _log_op(cfg, f"{SIDE_LABELS[side]} 更新 {entry.get('name_en') or mod_id}: "
                 f"v{old.version or '?'} → v{ver}（{dest.name}）")
    leftover = _cleanup_extras(cfg, db, side, mod_id, dest, unremoved)
    result = {"action": "updated", "from": old.version or "?", "to": ver,
              "file": dest.name, "note": info.note, "body": info.release_body,
              "warning": warn}
    if leftover:
        result["leftover"] = leftover
    return result


def update_all(cfg, db, installed, *, sides=SIDES, progress_cb=None,
               registry=None) -> list:
    """逐mod更新所有可更新的 mod；单个失败不中断。返回结果列表。

    每个 mod 只查询一次最新版（双端复用同一份版本列表，请求量减半），
    progress_cb(done, total, name) 按 mod 计数（双端不重复计数）。
    registry 可传入已构建好的 build_registry 结果，避免重复扫描磁盘。
    """
    reg = registry if registry is not None else build_registry(cfg, db, installed)
    order: list = []  # [mod_id, name, [sides]]——跨端去重，保持端别顺序
    for side in sides:
        for mod_id, st in reg.get(side, {}).items():
            if not st["enabled"] or st["locked"]:
                continue
            hit = next((x for x in order if x[0] == mod_id), None)
            if hit is None:
                order.append([mod_id, st["name_en"], [side]])
            else:
                hit[2].append(side)
    results = []
    total = len(order)
    for done, (mod_id, name, mod_sides) in enumerate(order, 1):
        if progress_cb:
            progress_cb(done, total, name)
        options, err = list_install_options(db.get(mod_id) or {}, cfg, db, force=True)
        for side in mod_sides:
            try:
                r = update_mod(cfg, db, installed, mod_id, side, prefetched=(options, err))
            except Exception as e:  # 单个失败不中断
                r = {"action": "error", "error": str(e)}
            r["side"], r["mod_id"], r["name"] = side, mod_id, name
            results.append(r)
    return results


# ---------- 启用/禁用 / 锁定 ----------

def _toggle_path(path: Path, enable: bool) -> Path:
    name = path.name
    if enable and name.lower().endswith(".jar.disabled"):
        return path.with_name(name[:-len(".disabled")])
    if not enable and name.lower().endswith(".jar"):
        return path.with_name(name + ".disabled")
    return path


def set_enabled(cfg, db, installed, mod_id: str, side: str, enabled: bool) -> dict:
    """启用/禁用（.jar ↔ .jar.disabled 重命名）。两端独立。"""
    f = find_installed_file(cfg, db, side, mod_id)
    if not f:
        return {"action": "error", "error": f"{SIDE_LABELS[side]}未找到该mod的文件"}
    if f.enabled == enabled:
        return {"action": "unchanged", "file": f.file_name}
    new_path = _toggle_path(f.path, enabled)
    try:
        os.rename(f.path, new_path)
    except PermissionError:
        return {"action": "error", "error": f"文件被占用，请先关闭游戏/服务端: {f.file_name}"}
    installed.set(side, mod_id, file_name=new_path.name, enabled=enabled)
    entry = db.get(mod_id) or {}
    name = entry.get("name_en") or mod_id
    _log_op(cfg, f"{SIDE_LABELS[side]} {'启用' if enabled else '禁用'} {name}（{new_path.name}）")
    return {"action": "enabled" if enabled else "disabled", "file": new_path.name}


def set_lock(installed, mod_id: str, side: str, locked: bool) -> None:
    """直接设置某端别锁定状态。"""
    installed.set(side, mod_id, locked=locked)


def exclude_installed(cfg, db, installed, mod_id: str) -> list:
    """把已安装 mod 从受管列表剔除（其 jar 文件名加入忽略列表，两端都剔除）。

    可随时在「恢复已排除文件」中恢复显示。返回被剔除的文件名列表。
    """
    names = []
    for side in SIDES:
        for f in find_installed_files(cfg, db, side, mod_id):
            ignore_unmanaged(cfg, f.file_name)
            installed.remove(side, mod_id)
            names.append(f.file_name)
    entry = db.get(mod_id) or {}
    _log_op(cfg, f"从列表剔除 {entry.get('name_en') or mod_id}: {', '.join(names) or '无文件'}")
    return names


# ---------- 删除 ----------

def delete_mod(cfg, db, installed, mod_id: str, *, sides: tuple = SIDES) -> dict:
    """删除mod：jar 移入备份目录并加 .deleted 后缀（可恢复），不直接物理删除。

    返回 {action: deleted/error, deleted: [...], error: str}。
    """
    entry = db.get(mod_id) or {}
    name = entry.get("name_en") or mod_id
    deleted, errors = [], []
    for side in sides:
        side_err = len(errors)  # 本端别处理前的错误数（错误跨端共享时判定会误伤）
        files = find_installed_files(cfg, db, side, mod_id)
        if not files:
            continue
        for f in files:
            dest_dir = cfg.backup_dir / side / mod_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{utils.timestamp_str()}_{f.file_name}.deleted"
            try:
                shutil.move(str(f.path), str(dest))
                deleted.append(f"{SIDE_LABELS[side]}: {f.file_name}")
                _log_op(cfg, f"{SIDE_LABELS[side]} 删除 {name}（{f.file_name} → 备份目录 .deleted）")
            except PermissionError:
                errors.append(f"{SIDE_LABELS[side]} 文件被占用，请先关闭游戏/服务端: {f.file_name}")
            except OSError as e:
                errors.append(f"{SIDE_LABELS[side]} 删除失败: {e}")
        if len(errors) == side_err:
            installed.remove(side, mod_id)
    if not deleted and not errors:
        return {"action": "error", "error": "未找到该mod的文件"}
    return {"action": "deleted" if deleted else "error",
            "deleted": deleted, "error": "；".join(errors)}


# ---------- 备份 ----------

def prune_all_backups(cfg) -> int:
    """按每mod保留数清理存量超限备份（含 .deleted），返回删除的文件数。

    修复前的 prune 按新文件名匹配旧备份，永远清不到，备份目录会无限
    增长；启动时调用一次把历史积压清掉（超出保留数的旧备份被删除）。
    """
    base = cfg.backup_dir
    if not base.is_dir():
        return 0
    removed = 0
    for side in SIDES:
        side_dir = base / side
        if not side_dir.is_dir():
            continue
        for mod_dir in side_dir.iterdir():
            if mod_dir.is_dir():
                removed += downloader.prune_backups(mod_dir, cfg.backup_keep)
    if removed:
        _log_op(cfg, f"按每mod保留数 {cfg.backup_keep} 清理存量备份：删除 {removed} 个文件")
    return removed


def list_backups(cfg) -> dict:
    """列出备份：{side: {mod_id: [Path,...]}}（每个 mod 内最新在前）。"""
    out = {}
    base = cfg.backup_dir
    if not base.is_dir():
        return out
    for side in SIDES:
        side_dir = base / side
        if not side_dir.is_dir():
            continue
        for mod_dir in sorted(side_dir.iterdir()):
            if not mod_dir.is_dir():
                continue
            # backup_files 已按 mtime 排序并对消失的文件容错
            files = list(reversed(downloader.backup_files(mod_dir)))
            if files:
                out.setdefault(side, {})[mod_dir.name] = files
    return out


def restore_backup(cfg, db, installed, side: str, backup_path: Path) -> dict:
    """恢复某个备份 jar 到 mods 目录（当前文件先另存备份）。"""
    folder = cfg.mods_dir(side)
    if not folder or not folder.is_dir():
        return {"action": "error", "error": f"{SIDE_LABELS[side]}mods目录未设置或不存在"}
    mod_id = backup_path.parent.name
    name = re.sub(r"^\d{8}_\d{6}(?:_\d+)?_", "", backup_path.name)  # 时间戳[_序号]_原文件名
    if name.endswith(".deleted"):
        name = name[:-len(".deleted")]  # 删除操作移入的备份，恢复时还原原名
    old = find_installed_file(cfg, db, side, mod_id)
    cand = sources.DownloadCandidate(str(backup_path), name)
    try:
        dest, unremoved = downloader.update_with_backup(
            cand, folder, cfg.backup_dir / side / mod_id,
            old_file=old.path if old else None, backup_keep=cfg.backup_keep,
            dl_cache_dir=cfg.cache_dir)
    except downloader.FileBusyError as e:
        return {"action": "error", "error": str(e)}
    except Exception as e:
        return {"action": "error", "error": f"恢复失败: {e}"}
    ver = split_mc_mod_version(dest.stem)[2] or ""
    installed.set(side, mod_id, file_name=dest.name, parsed_version=ver,
                  updated_at=utils.now_str())
    entry = db.get(mod_id) or {}
    _log_op(cfg, f"{SIDE_LABELS[side]} 恢复备份 {entry.get('name_en') or mod_id} → {dest.name}")
    result = {"action": "restored", "file": dest.name}
    # 回滚后同样清理旧版本残留（占用失败会报告，防静默双jar）
    leftover = _cleanup_extras(cfg, db, side, mod_id, dest, unremoved)
    if leftover:
        result["leftover"] = leftover
    return result


def rollback_mod(cfg, db, installed, mod_id: str, *, sides: tuple = SIDES) -> list:
    """一键回滚到更新前版本，双端版本保持一致。

    取所有端别备份中最新的一份作为目标版本，在已安装该mod的每个端别上恢复
    同一份备份（本端无该备份时直接复用），保证双端同版本。
    返回 [{side, action(restored/nobackup/error), ...}]。
    """
    # 最新备份取文件创建时间：同秒内多次备份时，文件名字典序
    # （_1_ 序号 < 字母）不能反映先后；copy2 保留源 mtime 也不可用
    def backup_key(p):
        try:
            return (p.stat().st_ctime_ns, p.name)
        except OSError:
            return (0, p.name)

    all_jars = []
    for side in sides:
        bak_dir = cfg.backup_dir / side / mod_id
        if bak_dir.is_dir():
            # 只回滚普通备份；.deleted（用户主动删除的）不参与
            all_jars += [p for p in downloader.backup_files(bak_dir)
                         if p.is_file() and not p.name.endswith(".deleted")]
    if not all_jars:
        return [{"side": side, "action": "nobackup"} for side in sides]
    src = max(all_jars, key=backup_key)
    results = []
    for side in sides:
        if not find_installed_file(cfg, db, side, mod_id):
            continue  # 该端别未安装，跳过（只回滚已安装的端别）
        r = restore_backup(cfg, db, installed, side, src)
        r["side"], r["backup_file"] = side, src.name
        results.append(r)
    return results


# ---------- 未受管 ----------

def register_unmanaged(cfg, db, side: str, file_name: str, *,
                       name_cn: str = "", side_override: str = "both",
                       source_type: str = "manual", source: dict = None) -> str:
    """把未受管 jar 注册为自定义条目（默认 manual 无上游源）。返回新 mod_id。"""
    name_part, mc, ver = split_mc_mod_version(file_name[:-4])
    entry = {
        "name_en": name_part or file_name,
        "name_cn": name_cn,
        "side": side_override,
        "source_type": source_type,
        "source": source or {},
        "category": "自定义",
    }
    eid = db.add_custom(entry)
    if name_part:
        db.add_alias(eid, name_part)
    _log_op(cfg, f"注册未受管文件 {file_name} 为自定义mod {eid}")
    return eid


def associate_unmanaged(db, mod_id: str, alias: str) -> None:
    """把未匹配的 jar 文件名前缀关联到已有条目（记住映射）。"""
    db.add_alias(mod_id, alias)


def ignore_unmanaged(cfg, file_name: str) -> None:
    """忽略某个未受管文件（不再提示）。"""
    ignored = list(cfg.data.get("ignored_files") or [])
    if file_name not in ignored:
        ignored.append(file_name)
        cfg.data["ignored_files"] = ignored
        cfg.save()


def unignore(cfg, file_name: str) -> None:
    ignored = list(cfg.data.get("ignored_files") or [])
    if file_name in ignored:
        ignored.remove(file_name)
        cfg.data["ignored_files"] = ignored
        cfg.save()
