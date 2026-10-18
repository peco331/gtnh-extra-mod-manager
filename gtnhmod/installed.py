"""已安装状态（installed.json）。

只存增量状态（锁定/时间戳/远端版本缓存），不存文件路径——
路径会变，每次操作以磁盘扫描（scanner）为准，本文件做增量校正。
按端别分组：installed[side][mod_id] = {...}
"""
from pathlib import Path

from . import utils


def merge_records(records):
    """合并同一条目的历史 ID 状态：锁定从严保留，远端缓存取最近检查结果。"""
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return {}
    ordered = sorted(records, key=lambda r: str(r.get("updated_at") or r.get("install_date") or ""))
    result = {}
    for record in ordered:
        result.update(record)
    result["locked"] = any(r.get("locked") for r in records)
    checked = max(records, key=lambda r: str(r.get("last_checked") or ""))
    for key in ("last_checked", "last_remote_version", "last_remote_date"):
        if key in checked:
            result[key] = checked[key]
    return result


class InstalledDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        # 本进程删除过的 (side, mod_id)：save 合并时不让磁盘上的旧记录复活
        # （GUI 删除 ↔ 计划任务同时保存的场景）
        self._tombstones: set = set()
        # 本进程写过远端检查结果的 (side, mod_id)：保存时以本进程为准，
        # 不靠比较时间字符串（时区或系统时间调整会让刚检查的结果被旧记录覆盖）
        self._checked_here: set = set()
        self.load()

    def load(self):
        self._tombstones = set()  # 重新加载即重置删除标记
        self._checked_here = set()
        data = utils.load_json(self.path, None)  # None=缺失或损坏
        if data is None:
            # 缺失/损坏 → 从 .bak 自动恢复（对齐 mods_db.json 的做法）。
            # installed.json 存着锁定状态，静默清空会让自动更新误伤被锁的mod
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            recovered = utils.load_json(bak, None)
            if recovered:
                self.data = recovered.get("installed") or {}
                utils.atomic_write_json(self.path,
                                        {"version": 1, "installed": self.data})
                utils.append_log(self.path.parent,
                                 "installed.json 缺失或损坏，已从 .bak 自动恢复")
        else:
            self.data = data.get("installed") or {}
        for side in ("client", "server"):
            self.data.setdefault(side, {})

    def save(self):
        """整文件保存。

        先与磁盘合并（整个"读盘合并+写"持跨进程锁，与其他进程串行）：
        - 计划任务在本进程加载后新写入的 last_checked/last_remote_* → 采纳
        - 磁盘上独有的记录（他进程新装/注册的 mod）→ 并入内存
        - 本进程删除过的记录（墓碑）→ 不复活
        """
        with utils.file_lock(self.path):
            utils.backup_file(self.path)
            disk = utils.load_json(self.path, None)
            if isinstance(disk, dict):
                disk_inst = disk.get("installed") or {}
                for side in ("client", "server"):
                    disk_side = disk_inst.get(side) or {}
                    mine = self.data.setdefault(side, {})
                    for mod_id, rec in disk_side.items():
                        if not isinstance(rec, dict):
                            continue
                        local = mine.get(mod_id)
                        if local is None:
                            # 磁盘独有：非本进程删除的记录并入（他进程新装的别丢）
                            if (side, mod_id) not in self._tombstones:
                                mine[mod_id] = rec
                            continue
                        if (side, mod_id) in self._checked_here:
                            continue
                        if str(rec.get("last_checked") or "") > str(local.get("last_checked") or ""):
                            for k in ("last_checked", "last_remote_version", "last_remote_date"):
                                if rec.get(k):
                                    local[k] = rec[k]
            utils.atomic_write_json(self.path, {"version": 1, "installed": self.data})

    # ---- 访问 ----
    def get(self, side: str, mod_id: str):
        return self.data.get(side, {}).get(mod_id)

    def merged_record(self, side: str, mod_ids):
        return merge_records(self.get(side, mid) for mid in mod_ids)

    def remap_ids(self, redirects: dict) -> int:
        """在目录迁移后合并旧 ID，并阻止 save() 从磁盘复活旧记录。"""
        count = 0
        for side in ("client", "server"):
            grouped = {}
            for old_id, canonical in redirects.items():
                if self.get(side, old_id) is not None:
                    grouped.setdefault(canonical, []).append(old_id)
            for canonical, old_ids in grouped.items():
                fields = self.merged_record(side, [canonical, *old_ids])
                self.set(side, canonical, save=False, **fields)
                for old_id in old_ids:
                    self.remove(side, old_id, save=False)
                    count += 1
        if count:
            self.save()
        return count

    def set(self, side: str, mod_id: str, *, save: bool = True, **fields) -> None:
        rec = self.data.setdefault(side, {}).setdefault(mod_id, {})
        rec.update(fields)
        self._tombstones.discard((side, mod_id))  # 删除后又重新写入 → 撤销墓碑
        if save:
            self.save()

    def remove(self, side: str, mod_id: str, *, save: bool = True) -> None:
        self.data.get(side, {}).pop(mod_id, None)
        self._tombstones.add((side, mod_id))
        if save:
            self.save()

    def touch_checked(self, side: str, mod_id: str, remote_version,
                      remote_date=None, *, save: bool = True) -> None:
        """记录一次远端检查结果（用于未改动时免查显示）。

        批量检查时传 save=False，结束后调用方统一 save()，避免逐mod全文件重写。
        """
        fields = {"last_checked": utils.now_str(), "last_remote_version": remote_version}
        if remote_date:
            fields["last_remote_date"] = remote_date
        self._checked_here.add((side, mod_id))
        self.set(side, mod_id, save=save, **fields)

    def all_ids(self, side: str) -> list:
        return list(self.data.get(side, {}).keys())
