"""已安装状态（installed.json）。

只存增量状态（锁定/时间戳/远端版本缓存），不存文件路径——
路径会变，每次操作以磁盘扫描（scanner）为准，本文件做增量校正。
按端别分组：installed[side][mod_id] = {...}
"""
from pathlib import Path

from . import utils


class InstalledDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict = {}
        self.load()

    def load(self):
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
        utils.backup_file(self.path)
        utils.atomic_write_json(self.path, {"version": 1, "installed": self.data})

    # ---- 访问 ----
    def get(self, side: str, mod_id: str):
        return self.data.get(side, {}).get(mod_id)

    def set(self, side: str, mod_id: str, *, save: bool = True, **fields) -> None:
        rec = self.data.setdefault(side, {}).setdefault(mod_id, {})
        rec.update(fields)
        if save:
            self.save()

    def remove(self, side: str, mod_id: str, *, save: bool = True) -> None:
        self.data.get(side, {}).pop(mod_id, None)
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
        self.set(side, mod_id, save=save, **fields)

    def all_ids(self, side: str) -> list:
        return list(self.data.get(side, {}).keys())
