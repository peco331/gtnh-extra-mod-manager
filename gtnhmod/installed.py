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
        data = utils.load_json(self.path, None)
        if data:
            self.data = data.get("installed") or {}
        else:
            self.data = {}
        for side in ("client", "server"):
            self.data.setdefault(side, {})

    def save(self):
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

    def touch_checked(self, side: str, mod_id: str, remote_version, remote_date=None) -> None:
        """记录一次远端检查结果（用于未改动时免查显示）。"""
        fields = {"last_checked": utils.now_str(), "last_remote_version": remote_version}
        if remote_date:
            fields["last_remote_date"] = remote_date
        self.set(side, mod_id, **fields)

    def all_ids(self, side: str) -> list:
        return list(self.data.get(side, {}).keys())
