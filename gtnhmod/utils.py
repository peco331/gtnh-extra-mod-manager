"""通用工具：数据目录定位、JSON 原子读写、时间戳。"""
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path


def resolve_data_dir() -> Path:
    """确定数据目录：环境变量 GTNHMOD_DATA_DIR > 工具目录 data/ > %APPDATA% 回退。"""
    env = os.environ.get("GTNHMOD_DATA_DIR")
    if env:
        return Path(env)
    local = Path(__file__).resolve().parent.parent / "data"
    try:
        local.mkdir(parents=True, exist_ok=True)
        probe = local / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return local
    except OSError:
        base = Path(os.environ.get("APPDATA") or str(Path.home()))
        fallback = base / "GTNHModManager"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def timestamp_str() -> str:
    """文件名安全的时间戳（备份目录用）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def atomic_write_json(path: Path, data) -> None:
    """原子写入 JSON：先写 .tmp 再 os.replace，防止半写损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default=None):
    """读取 JSON，文件缺失或损坏时返回 default。"""
    if default is None:
        default = {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def backup_file(path: Path) -> Path | None:
    """写前备份：文件改名前留一份 .bak。"""
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + ".bak")
    try:
        shutil.copy2(path, bak)
        return bak
    except OSError:
        return None


def log_file_path(data_dir: Path) -> Path:
    """操作日志文件路径：data/logs/operations.log。"""
    return Path(data_dir) / "logs" / "operations.log"


def append_log(data_dir: Path, msg: str) -> None:
    """追加一条操作日志（超过2MB自动轮转为 operations.log.old）。失败静默。"""
    try:
        p = log_file_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 2 * 1024 * 1024:
            try:
                p.replace(p.with_name("operations.log.old"))
            except OSError:
                pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{now_str()}] {msg}\n")
    except OSError:
        pass
