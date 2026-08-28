"""mods 数据库（mods_db.json）：wiki 解析结果 + 自定义源，统一条目模型。

条目字段（wiki 与自定义源同构）：
  id, name_en, name_cn, group(星门规则/非星门规则/自定义), category,
  side(client/server/both), side_uncertain,
  source_type(github/curseforge/local_folder/manual),
  source{owner,repo,asset_regex,exclude_regex} 或 {path,name_regex},
  desc, detail(原文), urls{...}, aliases[](用户手动关联的jar前缀), wiki_removed
"""
from pathlib import Path

from . import utils
from .wiki import make_id

WIKI_GROUPS = ("星门规则", "非星门规则")


class ModsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.mods: list = []
        self.meta: dict = {}
        self.load()

    def load(self):
        data = utils.load_json(self.path, None)
        if not data:
            # 文件缺失/损坏（load_json 失败返回 {}）→ 尝试从 .bak 自动恢复
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            recovered = utils.load_json(bak, None)
            if recovered:
                self.mods = [m for m in (recovered.get("mods") or []) if isinstance(m, dict)]
                self.meta = recovered.get("meta") or {}
                utils.atomic_write_json(self.path,
                                        {"version": 1, "meta": self.meta, "mods": self.mods})
                utils.append_log(self.path.parent,
                                 "mods_db.json 缺失或损坏，已从 mods_db.json.bak 自动恢复")
                return
            self.mods, self.meta = [], {}
        else:
            self.mods = [m for m in (data.get("mods") or []) if isinstance(m, dict)]
            self.meta = data.get("meta") or {}

    def save(self, backup: bool = False):
        if backup:
            utils.backup_file(self.path)  # 结构变更前留 .bak（损坏可恢复）
        utils.atomic_write_json(self.path, {"version": 1, "meta": self.meta, "mods": self.mods})

    # ---- 查询 ----
    def get(self, mod_id: str):
        for m in self.mods:
            if m["id"] == mod_id:
                return m
        return None

    def all(self) -> list:
        return list(self.mods)

    def wiki_mods(self) -> list:
        return [m for m in self.mods if not m.get("wiki_removed")]

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
    def merge_wiki(self, fresh: list) -> list:
        """合并新解析的 wiki 条目。保留用户数据（aliases），返回变更日志。"""
        if not fresh:
            # 空结果几乎必然是抓取被反爬拦截或页面结构变更；照常合并会把
            # 全部 wiki 条目误标记为 wiki_removed，可添加列表直接清空
            raise ValueError("wiki 解析结果为空，已取消合并（本地数据未改动）。"
                             "请稍后重试；若反复出现，可能是页面结构变更，需更新解析器")
        changes = []
        old_by_id = {m["id"]: m for m in self.mods}
        fresh_ids = {m["id"] for m in fresh}
        new_mods = []
        for fe in fresh:
            old = old_by_id.get(fe["id"])
            if old:
                # 用户手动关联的别名保留（可能包含 wiki 上没有的拼写）
                merged_aliases = list(dict.fromkeys(
                    (old.get("aliases") or []) + (fe.get("aliases") or [])))
                fe["aliases"] = merged_aliases
                # 用户手动绑定的下载源在刷新后保留
                if old.get("source_override"):
                    fe["source"] = old.get("source") or fe.get("source")
                    fe["source_type"] = old.get("source_type") or fe.get("source_type")
                    fe["source_override"] = True
                # 用户配置的版本过滤（如 GitHub 只取带 GTNH 的版本）在刷新后保留
                old_tag_regex = (old.get("source") or {}).get("tag_regex")
                if old_tag_regex:
                    fe.setdefault("source", {})["tag_regex"] = old_tag_regex
                # 用户编辑过的中文名在刷新后保留
                if old.get("name_cn_override"):
                    fe["name_cn"] = old.get("name_cn") or fe.get("name_cn")
                    fe["name_cn_override"] = True
                # 下载页最新版发布时间不是wiki字段，刷新wiki时必须保留缓存
                if old.get("release_date"):
                    fe["release_date"] = old["release_date"]
                # 同名自定义源与 wiki 新收录条目合并 → 明确提示（用户字段已保留）
                if old.get("group") == "自定义":
                    changes.append(f'自定义源 {old.get("name_en") or old["id"]} 已被 wiki 收录，'
                                   "已合并为 wiki 条目（你的别名/绑定/中文名已保留）")
                for k, label in (("side", "端别"), ("category", "分类"), ("group", "分组")):
                    if old.get(k) != fe.get(k):
                        changes.append(f'{fe["name_en"] or fe["id"]}: {label} {old.get(k)} → {fe.get(k)}')
                if old.get("source_type") != fe.get("source_type") or old.get("source") != fe.get("source"):
                    changes.append(f'{fe["name_en"] or fe["id"]}: 下载源发生变化')
            else:
                changes.append(f'新增: {fe["name_en"] or fe["id"]}（{fe["group"]}/{fe["category"]}）')
            new_mods.append(fe)
        # 从 wiki 消失的条目保留记录但标记（已安装引用不丢）；自定义源原样保留
        for m in self.mods:
            if m.get("group") in WIKI_GROUPS:
                if m["id"] not in fresh_ids:
                    if not m.get("wiki_removed"):
                        m["wiki_removed"] = True
                        changes.append(f'移除: {m.get("name_en") or m["id"]}（wiki已删除，本地保留记录）')
                    new_mods.append(m)
            else:
                if m["id"] in fresh_ids:
                    continue  # 与wiki条目同名的自定义源已在上面合并处理，避免重复id
                new_mods.append(m)
        self.mods = new_mods
        self.meta["wiki_fetched_at"] = utils.now_str()
        self.save(backup=True)
        return changes
