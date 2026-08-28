"""通用工具：数据目录定位、JSON 原子读写、时间戳。"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


def clean_orphan_tmp(root: Path, keep_seconds: float = 86400) -> int:
    """清扫下载中断留下的孤儿临时文件（.part / .dl_*），返回删除数。

    只清超过 keep_seconds 的（避免误删正在进行中的下载）；进程被杀时
    这些文件不会自清理，会永久残留。
    """
    if not root.is_dir():
        return 0
    removed = 0
    now = time.time()
    try:
        candidates = list(root.rglob("*.part")) + list(root.rglob(".dl_*"))
    except OSError:
        return 0
    for p in candidates:
        if not p.is_file():
            continue
        try:
            if now - p.stat().st_mtime > keep_seconds:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def resolve_data_dir() -> Path:
    """确定数据目录：环境变量 GTNHMOD_DATA_DIR > 工具目录 data/ > %APPDATA% 回退。

    PyInstaller 打包（frozen）时 __file__ 位于每次运行都新建的临时解压目录，
    数据必须放 exe 旁边，否则退出即丢。
    """
    env = os.environ.get("GTNHMOD_DATA_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        local = Path(sys.executable).resolve().parent / "data"
    else:
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


def fmt_ts(ts: float) -> str:
    """Unix 时间戳 → "YYYY-MM-DD HH:MM"（列表显示/排序用，ISO 字符串可直接按时间排序）。"""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return ""


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
    """读取 JSON，文件缺失或损坏时返回 default。

    default 显式传 None 时失败返回 None（调用方可借此区分"损坏"与"空对象"）；
    不传 default 时失败返回 {}。
    """
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
