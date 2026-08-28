"""下载、jar 校验、旧版本备份与替换。"""
import os
import shutil
import zipfile
from pathlib import Path

from . import net, utils

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class VerifyError(Exception):
    """下载内容不是有效 jar。"""


class FileBusyError(Exception):
    """目标文件被占用（游戏/服务端运行中）。"""


def atomic_replace(src: Path, dst: Path) -> None:
    """把 src 原子替换为 dst 的内容（同盘 os.replace；跨盘先复制再同盘替换）。

    跨盘时 os.replace 会抛 WinError 17（无法将文件移到不同的磁盘驱动器）。
    """
    try:
        os.replace(src, dst)
    except OSError as e:
        # WinError 17 或跨设备错误 → 复制到目标盘 .part 后同盘原子替换
        if e.errno not in (17, 18) and not getattr(e, "winerror", None) == 17:
            raise
        part = dst.with_name(dst.name + ".part")
        try:
            shutil.copy2(src, part)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            raise
        try:
            os.replace(part, dst)
        finally:
            try:
                src.unlink()
            except OSError:
                pass


def verify_jar(path: Path) -> None:
    """校验下载产物：非空、zip 魔数、不是 HTML 错误页、zip 结构完整。失败抛 VerifyError。"""
    try:
        size = path.stat().st_size
    except OSError:
        raise VerifyError(f"文件不存在: {path.name}")
    if size == 0:
        raise VerifyError(f"下载的文件为空: {path.name}")
    with open(path, "rb") as f:
        head = f.read(512)
    low = head[:300].lower()
    if b"<!doctype" in low or b"<html" in low:
        raise VerifyError(f"下载到的是错误页面而非jar: {path.name}")
    if not head.startswith(ZIP_MAGICS):
        raise VerifyError(f"文件不是有效的 jar/zip: {path.name}")
    # 魔数只覆盖前512字节，被截断的 zip 也能通过——完整校验中央目录与 CRC
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
    except (zipfile.BadZipFile, OSError) as e:
        raise VerifyError(f"jar 压缩结构损坏（可能下载不完整）: {path.name}（{e}）")
    if bad is not None:
        raise VerifyError(f"jar 内部文件损坏: {path.name}（{bad}）")


def _unique_backup_path(backup_dir: Path, file_name: str) -> Path:
    """时间戳_文件名，同一秒冲突时加序号（防覆盖）。"""
    bak = backup_dir / f"{utils.timestamp_str()}_{file_name}"
    k = 1
    while bak.exists():
        bak = backup_dir / f"{utils.timestamp_str()}_{k}_{file_name}"
        k += 1
    return bak


def _backup_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def backup_files(backup_dir: Path) -> list:
    """备份目录下的全部备份文件（jar 与 .deleted），按 mtime 旧→新排序。"""
    try:
        files = list(backup_dir.glob("*.jar")) + list(backup_dir.glob("*.jar.deleted"))
        return sorted(files, key=lambda p: (_backup_mtime(p), p.name))
    except OSError:
        return []


def prune_backups(backup_dir: Path, keep: int) -> int:
    """清理超出保留数的旧备份，返回删除的文件数。

    按整个备份目录清理而非按文件名匹配：备份保存的是旧 jar 的文件名，
    更新后文件名随版本号变化，按新文件名 glob 永远匹配不到旧备份，
    会导致备份无限累积。
    普通备份与 .deleted（删除mod移入）分成两个池各自保留 keep 份：
    删除的 jar 不会因同 mod 之后几次更新而被挤掉，"删除可恢复"才成立。
    """
    files = backup_files(backup_dir)
    pools = (
        [p for p in files if not p.name.endswith(".deleted")],
        [p for p in files if p.name.endswith(".deleted")],
    )
    removed = 0
    for pool in pools:
        excess = pool[:max(0, len(pool) - max(0, keep))]
        for p in excess:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def update_with_backup(cand, dest_dir: Path, backup_dir: Path, *,
                       old_file: Path = None, backup_keep: int = 3,
                       progress_cb=None, proxy=None, dl_cache_dir: Path = None) -> tuple:
    """下载候选并替换安装（旧文件先备份，再移除）。

    - cand: DownloadCandidate
    - old_file: 当前已安装的旧 jar（文件名可能与新资产不同，需移除避免双jar冲突）
    返回 (新文件 Path, 未能移除的旧文件名列表[文件被占用等])。
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(dl_cache_dir) if dl_cache_dir else dest_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / (".dl_" + cand.file_name)
    net.download(cand.url, tmp, progress_cb=progress_cb, proxy=proxy)
    try:
        verify_jar(tmp)
    except VerifyError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    target = dest_dir / cand.file_name
    # 备份旧文件（若与新文件同名，replace 前先留档）
    victims = []
    if old_file is not None and Path(old_file).exists():
        victims.append(Path(old_file))
    elif old_file is None and target.exists():
        victims.append(target)
    if victims:
        backup_dir.mkdir(parents=True, exist_ok=True)
    for v in victims:
        bak = _unique_backup_path(backup_dir, v.name)
        shutil.copy2(v, bak)
    try:
        atomic_replace(tmp, target)
    except PermissionError:
        raise FileBusyError(f"文件被占用，请先关闭游戏/服务端: {target.name}")
    # 新文件就位后移除旧文件（已备份）；占用失败时报告给调用方，避免静默双jar
    failed = []
    for v in victims:
        if v.resolve() != target.resolve():
            try:
                v.unlink()
            except OSError:
                failed.append(v.name)
    prune_backups(backup_dir, backup_keep)
    return target, failed
