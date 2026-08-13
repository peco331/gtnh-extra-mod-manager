"""mods 目录扫描、jar 识别、与数据库条目匹配。

匹配规则（normalize 后三档）：
  1. exact：文件名名字段 == 条目 name_en/name_cn/aliases
  2. prefix：文件名名字段以条目 name_en 开头（取最长前缀，更具体者胜）
  3. abbrev：条目 name_en 以文件名名字段开头（缩写，如 NEC → NeverEnoughCharacters，取最短条目名）
歧义/零命中返回 None，由 UI 弹候选让用户选择，选择结果写入条目 aliases 持久化。
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

from .versions import split_mc_mod_version

JAR_SUFFIXES = (".jar", ".jar.disabled")


@dataclass
class InstalledFile:
    path: Path
    file_name: str
    enabled: bool            # False = 文件名以 .jar.disabled 结尾
    name_part: str | None    # 切分出的名字段
    mc_version: str | None
    version: str | None
    mod_id: str | None = None
    match_quality: str = "none"   # exact|prefix|abbrev|none


def normalize_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _word_tokens(s: str) -> list:
    """按分隔符拆词，保留大小写（NeverEnoughCharacters-Rework → [NeverEnoughCharacters, Rework]）。"""
    return re.findall(r"[A-Za-z0-9]+", s or "")


def _word_initials(word: str) -> str:
    """词的大写首字母串：NeverEnoughCharacters → NEC。"""
    return "".join(c for c in word if c.isupper())


def _token_abbrev_match(f_tokens: list, e_tokens: list) -> bool:
    """词级缩写匹配：NEC-Rework → NeverEnoughCharacters-Rework。

    要求词数相同且至少2词；每个词相等、或缩写（f 为 e 的小写前缀）、
    或 f 等于 e 的大写首字母串（NEC 型）。"""
    if len(f_tokens) != len(e_tokens) or len(f_tokens) < 2:
        return False
    for ft, et in zip(f_tokens, e_tokens):
        ft_l, et_l = ft.lower(), et.lower()
        if ft_l == et_l:
            continue
        if len(ft_l) >= 2 and len(et_l) >= 4 and et_l.startswith(ft_l):
            continue
        ini = _word_initials(et).lower()
        if len(ini) >= 2 and ft_l == ini:
            continue
        return False
    return True


def scan_folder(folder: Path) -> list:
    """扫描 mods 目录，返回 InstalledFile 列表（仅 .jar 与 .jar.disabled）。"""
    out = []
    if not folder or not folder.is_dir():
        return out
    try:
        files = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for p in files:
        name = p.name
        if not p.is_file():
            continue
        if name.lower().endswith(".jar"):
            enabled, stem = True, name[:-4]
        elif name.lower().endswith(".jar.disabled"):
            enabled, stem = False, name[:-13]
        else:
            continue
        name_part, mc, ver = split_mc_mod_version(stem)
        out.append(InstalledFile(p, name, enabled, name_part, mc, ver))
    return out


MIN_FUZZY_LEN = 4  # 前缀/缩写匹配的最短名字长度（防中文名残片如"nei"误配）


def match_db(f: InstalledFile, entries: list) -> tuple[str | None, str]:
    """把扫描到的文件匹配到 db 条目。返回 (mod_id|None, quality)。

    规则：
    - exact：与 name_en/name_cn/aliases 归一化全等（中文名可参与）
    - prefix/abbrev：只用 name_en 与 aliases（中文名残片不可靠），且长度≥4
    - 词级缩写：NEC-Rework → NeverEnoughCharacters-Rework
    """
    if not f.name_part:
        return None, "none"
    norm = normalize_name(f.name_part)
    if not norm:
        return None, "none"
    exact, prefix, abbrev = [], [], []
    for e in entries:
        # 只用英文名与别名参与匹配：中文名混有拉丁残片（如"NEI 拼音搜索"→"nei"），
        # 会与整合包自带 NEI* mod 全等/前缀误配
        en = normalize_name(e.get("name_en") or "")
        aliases = [normalize_name(a) for a in (e.get("aliases") or [])]
        if norm in ([en] if en else []) + aliases:
            exact.append(e["id"])
            continue
        for nc in ([en] if en else []) + aliases:
            if not nc or len(nc) < MIN_FUZZY_LEN:
                continue
            if norm.startswith(nc):
                prefix.append((len(nc), e["id"]))
            elif len(norm) >= MIN_FUZZY_LEN and nc.startswith(norm):
                abbrev.append((len(nc), e["id"]))
    if exact:
        return exact[0], "exact"
    if prefix:
        # 最长前缀最具体：foamfixanimations → FoamFix-Animations 而非 FoamFix
        return max(prefix)[1], "prefix"
    if abbrev:
        # 最短条目名最可能是缩写目标
        return min(abbrev)[1], "abbrev"
    # 词级缩写：NEC-Rework → NeverEnoughCharacters-Rework
    f_tokens = _word_tokens(f.name_part)
    token_cands = []
    for e in entries:
        if _token_abbrev_match(f_tokens, _word_tokens(e.get("name_en") or "")):
            token_cands.append((len(e.get("name_en") or ""), e["id"]))
    if token_cands:
        return min(token_cands)[1], "abbrev"
    return None, "none"


def match_all(files: list, entries: list) -> None:
    """就地填充每个文件的 mod_id / match_quality。"""
    for f in files:
        f.mod_id, f.match_quality = match_db(f, entries)


def reconcile(scan_by_side: dict, installed: "InstalledDB") -> dict:
    """用磁盘扫描结果校正 installed.json。

    返回 {side: {mod_id: InstalledFile}}（当前磁盘上真实存在的、已匹配的文件）。
    installed.json 中文件已不存在的记录会被清除。
    """
    live: dict = {}
    for side, files in scan_by_side.items():
        side_live = {}
        for f in files:
            if not f.mod_id:
                continue
            side_live[f.mod_id] = f
            inst = installed.get(side, f.mod_id) or {}
            installed.set(side, f.mod_id, save=False,
                          file_name=f.file_name,
                          parsed_version=f.version or "",
                          enabled=f.enabled,
                          install_date=inst.get("install_date") or None,
                          locked=bool(inst.get("locked")))
        # 清理已不存在于磁盘的记录
        for mod_id in installed.all_ids(side):
            if mod_id not in side_live:
                installed.remove(side, mod_id, save=False)
        live[side] = side_live
    installed.save()
    return live
