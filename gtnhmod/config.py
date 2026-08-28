"""配置管理：config.json 读写（客户端/服务端路径、token、代理等）。"""
from pathlib import Path

from . import utils

DEFAULTS = {
    "version": 1,
    "mods_folders": {"client": "", "server": ""},
    "github_token": "",
    "proxy": None,                # {"host","port","user","pass"} 或 null（跟随系统代理）
    "check_interval_hours": 6,    # GitHub 检查结果的新鲜度缓存时长
    "backup_keep": 3,             # 每个mod保留的旧版本备份数
    "wiki_url": "https://gtnh.huijiwiki.com/api.php",
    "wiki_page": "可添加MOD",
    "gtnh_version": "",           # 当前整合包版本（兼容性推荐/拦截用）
    "ignored_files": [],          # 未受管且主动忽略的 jar 文件名
    "core_mod_confirm": True,     # 禁用 mod 前二次确认
    "wiki_cookie": "",            # Cloudflare 反爬：浏览器通过验证后的 Cookie（cf_clearance）
    "wiki_ua": "",                # 与 cookie 配套的浏览器 User-Agent（cf_clearance 与 UA 绑定）
}


class Config:
    """config.json 的封装。路径修改后无需重启，save 即持久化。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "config.json"
        self.data: dict = {}
        self.load()

    def load(self):
        saved = utils.load_json(self.path, None)
        merged = dict(DEFAULTS)
        if saved:
            merged.update(saved)
        # 可变容器必须独立拷贝：dict(DEFAULTS) 是浅拷贝，共享的 dict/list
        # 会被其他 Config 实例的修改污染（set_mods_dir 就地改 mods_folders）
        merged["mods_folders"] = dict(merged.get("mods_folders") or {})
        merged["mods_folders"].setdefault("client", "")
        merged["mods_folders"].setdefault("server", "")
        if not isinstance(merged.get("ignored_files"), list):
            merged["ignored_files"] = []
        else:
            merged["ignored_files"] = list(merged["ignored_files"])
        self.data = merged

    def save(self):
        utils.backup_file(self.path)
        utils.atomic_write_json(self.path, self.data)

    # ---- 便捷访问 ----
    @property
    def client_mods_dir(self) -> Path | None:
        p = self.data["mods_folders"].get("client", "")
        return Path(p) if p else None

    @property
    def server_mods_dir(self) -> Path | None:
        p = self.data["mods_folders"].get("server", "")
        return Path(p) if p else None

    def mods_dir(self, side: str) -> Path | None:
        return self.client_mods_dir if side == "client" else self.server_mods_dir

    def set_mods_dir(self, side: str, path) -> None:
        self.data["mods_folders"][side] = str(path) if path else ""
        self.save()

    @property
    def proxy(self):
        return self.data.get("proxy")

    @property
    def wiki_url(self) -> str:
        return self.data.get("wiki_url") or DEFAULTS["wiki_url"]

    @property
    def wiki_page(self) -> str:
        return self.data.get("wiki_page") or DEFAULTS["wiki_page"]

    @property
    def wiki_cookie(self) -> str:
        return (self.data.get("wiki_cookie") or "").strip()

    @property
    def wiki_ua(self) -> str:
        return (self.data.get("wiki_ua") or "").strip()

    def set_wiki_cookie(self, cookie: str, ua: str) -> None:
        """保存 wiki 反爬 Cookie 与配套 UA（传空串即清除）。"""
        self.data["wiki_cookie"] = (cookie or "").strip()
        self.data["wiki_ua"] = (ua or "").strip()
        self.save()

    @property
    def github_token(self) -> str:
        return self.data.get("github_token") or ""

    @property
    def check_interval_hours(self) -> float:
        try:
            return float(self.data.get("check_interval_hours") or 6)
        except (TypeError, ValueError):
            return 6.0

    @property
    def backup_keep(self) -> int:
        try:
            return int(self.data.get("backup_keep") or 3)
        except (TypeError, ValueError):
            return 3

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backup"
