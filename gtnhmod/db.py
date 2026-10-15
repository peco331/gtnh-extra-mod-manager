"""mods 数据库（mods_db.json）：wiki 解析结果 + 自定义源，统一条目模型。

条目字段（wiki 与自定义源同构）：
  id, name_en, name_cn, group(星门规则/非星门规则/自定义), category,
  side(client/server/both), side_uncertain,
  source_type(github/curseforge/local_folder/manual),
  source{owner,repo,asset_regex,exclude_regex} 或 {path,name_regex},
  desc, detail(原文), urls{...}, aliases[](用户手动关联的jar前缀), wiki_removed
"""
from copy import deepcopy
from pathlib import Path

from . import utils
from .wiki import github_repo_from_url, make_id, strip_markup, _strip_refs

WIKI_GROUPS = ("星门规则", "非星门规则")


def _name_key(entry):
    name = entry.get("name_en") or entry.get("name_cn") or ""
    return "".join(c for c in strip_markup(_strip_refs(name)).casefold() if c.isalnum())


def _source_keys(entry):
    """只用项目地址证明身份；共用一个仓库的不同名字仍是不同条目。"""
    source, urls = entry.get("source") or {}, entry.get("urls") or {}
    keys = set()
    if entry.get("source_type") == "github" and source.get("owner") and source.get("repo"):
        keys.add((source["owner"].casefold(), source["repo"].removesuffix(".git").casefold()))
    for url in (urls.get("github"), urls.get("preferred")):
        repo = github_repo_from_url(url)
        if repo:
            keys.add(tuple(s.casefold() for s in repo))
    if urls.get("curseforge"):
        keys.add(("curseforge", urls["curseforge"].rstrip("/").casefold()))
    return keys


def _same_identity(left, right):
    key = _name_key(left)
    if not key or key != _name_key(right):
        return False
    # 两个独立的用户绑定不能因同名而丢失其中一个。
    if left.get("source_override") and right.get("source_override"):
        if (left.get("source_type"), left.get("source")) != (right.get("source_type"), right.get("source")):
            return False
    a, b = _source_keys(left), _source_keys(right)
    if a or b:
        if not a.intersection(b):
            return False
        # 同一项目可同时发布不同 MOD，明确的资产/版本过滤也要尊重。
        for field in ("asset_regex", "tag_regex"):
            av, bv = (left.get("source") or {}).get(field), (right.get("source") or {}).get(field)
            if av and bv and av != bv:
                return False
        return True
    # 无项目地址时只认可链接格式导致的名称变化，不猜测一般重命名。
    return any("[[" in (e.get("name_en") or "") for e in (left, right))


def _retain_user_fields(old, fresh):
    fresh["aliases"] = list(dict.fromkeys((old.get("aliases") or []) + (fresh.get("aliases") or [])))
    fresh["legacy_ids"] = list(dict.fromkeys(
        (old.get("legacy_ids") or []) + (fresh.get("legacy_ids") or [])
        + ([fresh["id"]] if fresh["id"] != old["id"] else [])))
    fresh["id"] = old["id"]
    fresh["legacy_ids"] = [mid for mid in fresh["legacy_ids"] if mid != fresh["id"]]
    if old.get("source_override"):
        fresh["source"] = deepcopy(old.get("source") or fresh.get("source") or {})
        fresh["source_type"] = old.get("source_type") or fresh.get("source_type")
        fresh["source_override"] = True
    old_source, new_source = old.get("source") or {}, fresh.get("source") or {}
    if old.get("source_type") == fresh.get("source_type") == "github":
        same_source = all(str(old_source.get(k) or "").casefold() ==
                          str(new_source.get(k) or "").casefold() for k in ("owner", "repo"))
    else:
        same_source = (old.get("source_type"), old_source) == (fresh.get("source_type"), new_source)
    if same_source:
        for field in ("target_profile", "tag_regex"):
            if old_source.get(field):
                new_source[field] = old_source[field]
        if old.get("release_date"):
            fresh["release_date"] = max(old["release_date"], fresh.get("release_date") or "")
    if old.get("name_cn_override"):
        fresh["name_cn"] = old.get("name_cn") or fresh.get("name_cn")
        fresh["name_cn_override"] = True


class ModsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mods: list = []
        self.meta: dict = {}
        self.catalog_dirty = False
        self.catalog_repairs = []
        self.load()

    def load(self):
        data = utils.load_json(self.path, None)
        if not data:
            # 文件缺失/损坏（load_json 显式传 None，失败返回 None）→ 尝试从 .bak 自动恢复
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            recovered = utils.load_json(bak, None)
            if recovered:
                self.mods = [m for m in (recovered.get("mods") or []) if isinstance(m, dict)]
                self.meta = recovered.get("meta") or {}
                utils.atomic_write_json(self.path,
                                        {"version": 1, "meta": self.meta, "mods": self.mods})
                utils.append_log(self.path.parent,
                                 "mods_db.json 缺失或损坏，已从 mods_db.json.bak 自动恢复")
                self.repair_catalog()
                return
            self.mods, self.meta = [], {}
        else:
            self.mods = [m for m in (data.get("mods") or []) if isinstance(m, dict)]
            self.meta = data.get("meta") or {}
        self.repair_catalog()

    def repair_catalog(self):
        """修复旧解析器生成的重复 Wiki 条目；加载时仅改内存，启动维护再备份落盘。"""
        groups = {}
        for entry in self.mods:
            if entry.get("group") in WIKI_GROUPS:
                groups.setdefault(_name_key(entry), []).append(entry)
        for key, rows in groups.items():
            active = [e for e in rows if not e.get("wiki_removed")]
            retired = [e for e in rows if e.get("wiki_removed")]
            # 多个在用条目或历史条目都可能有独立用途，歧义时不自动合并。
            if not key or len(active) != 1 or len(retired) != 1:
                continue
            current, old = active[0], retired[0]
            if not _same_identity(old, current):
                continue
            merged = {**deepcopy(old), **deepcopy(current)}
            _retain_user_fields(old, merged)
            self.mods[self.mods.index(current)] = merged
            self.mods.remove(old)
            self.catalog_repairs.append(f'{old["id"]}: 合并 Wiki 名称格式变化产生的重复条目')
            self.catalog_dirty = True
        for entry in self.mods:
            if entry.get("group") not in WIKI_GROUPS:
                continue
            # 只有一个历史链接条目时仍沿用旧 ID，同时让新解析器的名称 ID 可定位它。
            raw_en, raw_cn = entry.get("name_en") or "", entry.get("name_cn") or ""
            if "[[" in raw_en or "[[" in raw_cn:
                plain_id = make_id(raw_en, raw_cn)
                if plain_id != entry["id"] and plain_id not in (entry.get("legacy_ids") or []):
                    entry.setdefault("legacy_ids", []).append(plain_id)
                    self.catalog_dirty = True
            for field in ("name_en", "name_cn"):
                text = entry.get(field) or ""
                clean = strip_markup(_strip_refs(text))
                if clean != text:
                    entry[field] = clean
                    self.catalog_dirty = True

    def id_redirects(self) -> dict:
        actual = {m["id"] for m in self.mods}
        candidates = {}
        for entry in self.mods:
            for mid in entry.get("legacy_ids") or []:
                if mid not in actual:
                    candidates.setdefault(mid, set()).add(entry["id"])
        return {mid: next(iter(ids)) for mid, ids in candidates.items() if len(ids) == 1}

    def entry_ids(self, mod_id):
        entry = self.get(mod_id)
        if not entry:
            return [mod_id]
        redirects = self.id_redirects()
        return [entry["id"], *(mid for mid in entry.get("legacy_ids") or []
                               if redirects.get(mid) == entry["id"])]

    def save(self, backup: bool = False):
        if backup:
            utils.backup_file(self.path)  # 结构变更前留 .bak（损坏可恢复）
        utils.atomic_write_json(self.path, {"version": 1, "meta": self.meta, "mods": self.mods})
        self.catalog_dirty = False

    # ---- 查询 ----
    def get(self, mod_id: str):
        for m in self.mods:
            if m["id"] == mod_id:
                return m
        canonical = self.id_redirects().get(mod_id)
        if canonical:
            return next(m for m in self.mods if m["id"] == canonical)
        return None

    def all(self) -> list:
        return list(self.mods)

    def wiki_mods(self) -> list:
        # 自定义源也可能没有 wiki_removed 字段，但不应混入 Wiki 目录。
        return [m for m in self.mods
                if m.get("group") in WIKI_GROUPS and not m.get("wiki_removed")]

    def custom_mods(self) -> list:
        return [m for m in self.mods if m.get("group") == "自定义"]

    def by_group_category(self) -> dict:
        """按 (分组, 分类) 聚合，返回 {('星门规则','功能增强'): [...], ...}。"""
        result: dict = {}
        for m in self.mods:
            result.setdefault((m.get("group") or "?", m.get("category") or "?"), []).append(m)
        return result

    # ---- 自定义源 ----
    def add_custom(self, entry: dict) -> str:
        """添加自定义源条目。entry: name_en/name_cn/side/source_type/source/... 返回新id。"""
        eid = make_id(entry.get("name_en") or "", entry.get("name_cn") or "")
        base, n = eid, 2
        while self.get(eid):
            eid = f"{base}-{n}"
            n += 1
        github_url = entry.get("github_url")
        curseforge_url = entry.get("curseforge_url")
        links = []
        if github_url:
            links.append({"url": github_url, "label": "github"})
        if curseforge_url:
            links.append({"url": curseforge_url, "label": "curseforge"})
        full = {
            "id": eid,
            "name_en": entry.get("name_en") or "",
            "name_cn": entry.get("name_cn") or "",
            "group": "自定义",
            "category": entry.get("category") or "自定义",
            "side": entry.get("side") or "both",
            "side_uncertain": bool(entry.get("side_uncertain")),
            "source_type": entry["source_type"],
            "source": entry.get("source") or {},
            "desc": entry.get("desc") or "",
            "detail": entry.get("detail") or "",
            "compat": list(entry.get("compat") or []),
            "urls": {"github": github_url or None, "curseforge": curseforge_url or None,
                     "mcmod": None, "bilibili": None, "other": [],
                     "links": links},
            "aliases": list(entry.get("aliases") or []),
            "wiki_removed": False,
            # 用户手填的中文名/源视为覆盖：若日后wiki收录同名mod，刷新时保留用户字段
            "name_cn_override": bool(entry.get("name_cn")),
            "source_override": True,
        }
        self.mods.append(full)
        self.save(backup=True)
        utils.append_log(self.path.parent, f"添加自定义源 {eid}（类型 {full['source_type']}）")
        return eid

    def remove_custom(self, mod_id: str) -> bool:
        m = self.get(mod_id)
        if not m or m.get("group") != "自定义":
            return False
        self.mods.remove(m)
        self.save(backup=True)
        utils.append_log(self.path.parent,
                         f"删除自定义源 {m.get('name_en') or mod_id}（类型 {m.get('source_type')}）")
        return True

    def update_custom(self, mod_id: str, fields: dict) -> bool:
        """更新自定义条目的字段（side/source/aliases 等）。"""
        return self.update_entry(mod_id, fields)

    def update_entry(self, mod_id: str, fields: dict) -> bool:
        """更新任意条目的字段（不覆盖 id/group/wiki_removed）。"""
        m = self.get(mod_id)
        if not m:
            return False
        for k, v in fields.items():
            if k in ("id", "group", "wiki_removed"):
                continue
            m[k] = v
        self.save(backup=True)
        return True

    def add_alias(self, mod_id: str, alias: str) -> None:
        """记住 jar 文件名前缀与条目的关联（用户手动匹配后调用）。"""
        m = self.get(mod_id)
        if not m or not alias:
            return
        if alias not in m.setdefault("aliases", []):
            m["aliases"].append(alias)
            self.save()

    # ---- wiki 合并 ----
    def merge_wiki(self, fresh: list, *, update_fetched_at: bool = True,
                   preserve_missing: bool = False) -> list:
        """合并新解析的 wiki 条目。保留用户数据（aliases），返回变更日志。"""
        if not fresh:
            # 空结果几乎必然是抓取被反爬拦截或页面结构变更；照常合并会把
            # 全部 wiki 条目误标记为 wiki_removed，可添加列表直接清空
            raise ValueError("wiki 解析结果为空，已取消合并（本地数据未改动）。"
                             "请稍后重试；若反复出现，可能是页面结构变更，需更新解析器")
        fresh = deepcopy(fresh)
        changes = []
        new_mods = []
        used_ids = set()
        for fe in fresh:
            old = self.get(fe["id"])
            if old and old["id"] in used_ids:
                old = None
            if old is None:
                candidates = [m for m in self.mods
                              if m.get("group") in WIKI_GROUPS and m["id"] not in used_ids
                              and _same_identity(m, fe)]
                if len(candidates) == 1:
                    candidate = candidates[0]
                    # 双向唯一，避免把一个旧条目套用到同名的多个新条目。
                    if sum(_same_identity(candidate, e) for e in fresh) == 1:
                        old = candidate
            if old:
                _retain_user_fields(old, fe)
                used_ids.add(old["id"])
                # 同名自定义源与 wiki 新收录条目合并 → 明确提示（用户字段已保留）
                if old.get("group") == "自定义":
                    changes.append(f'自定义源 {old.get("name_en") or old["id"]} 已被 wiki 收录，'
                                   "已合并为 wiki 条目（你的别名/绑定/中文名已保留）")
                for k, label in (("side", "端别"), ("category", "分类"), ("group", "分组")):
                    if old.get(k) != fe.get(k):
                        changes.append(f'{fe["name_en"] or fe["id"]}: {label} {old.get(k)} → {fe.get(k)}')
                if old.get("source_type") != fe.get("source_type") or old.get("source") != fe.get("source"):
                    changes.append(f'{fe["name_en"] or fe["id"]}: 下载源发生变化')
                if any(old.get(k) != fe.get(k)
                       for k in ("name_en", "name_cn", "side_uncertain",
                                 "desc", "detail", "urls", "compat")):
                    changes.append(f'{fe["name_en"] or fe["id"]}: Wiki资料已更新')
            else:
                changes.append(f'新增: {fe["name_en"] or fe["id"]}（{fe["group"]}/{fe["category"]}）')
            new_mods.append(fe)
        fresh_ids = {m["id"] for m in new_mods}
        # 从 wiki 消失的条目保留记录但标记（已安装引用不丢）；自定义源原样保留
        for m in self.mods:
            if m.get("group") in WIKI_GROUPS:
                if m["id"] not in fresh_ids:
                    if not preserve_missing and not m.get("wiki_removed"):
                        m["wiki_removed"] = True
                        changes.append(f'移除: {m.get("name_en") or m["id"]}（wiki已删除，本地保留记录）')
                    new_mods.append(m)
            else:
                if m["id"] in fresh_ids:
                    continue  # 与wiki条目同名的自定义源已在上面合并处理，避免重复id
                new_mods.append(m)
        self.mods = new_mods
        self.repair_catalog()
        # 缓存回退与离线导入不能伪装成已完成线上刷新。调用方只有在
        # 经验证的实时 Wiki 内容到达时才推进这个时间。
        if update_fetched_at:
            self.meta["wiki_fetched_at"] = utils.now_str()
        else:
            self.meta["wiki_imported_at"] = utils.now_str()
        self.save(backup=True)
        return changes
