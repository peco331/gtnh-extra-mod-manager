"""版本解析与比较、mod 文件名三段切分（名字|MC版本|mod版本）。

设计要点：
- GTNH 生态版本形态多：5.09.44.03、0.2.0p05（p=补丁，比 0.2.0 新）、
  2.0.0-GTNH、v0.8.0、1.0.0-beta1、dev-build 等。
- 排序键 (数字段, 预发布等级, 预发布序号)：正式版等级 99 最大；
  数字段逐位数值比较；未知字母（gtnh/build 等）忽略；
  "p" 后跟数字视为补丁段并入数字部分（GTNH 惯例，比基础版新）。
- 无法解析（无任何数字）→ VersionParseError，由调用层标记"未知版本"交用户判断。
"""
import re
from dataclasses import dataclass

# 常见 MC 版本锚点表（文件名/标签切分用，GTNH 以 1.7.10 为主）
KNOWN_MC_VERSIONS = (
    "1.20.1", "1.18.2", "1.16.5", "1.12.2", "1.11.2", "1.10.2",
    "1.9.4", "1.8.9", "1.7.10", "1.6.4",
)
_MC_SET = set(KNOWN_MC_VERSIONS)
MC_VERSION_RE = re.compile(r"^(MC)?(1\.\d{1,2}(?:\.\d{1,2})?)$", re.I)

# 预发布标记 → 等级（越小越"早"；正式版等级记为 99，最大）
PRERELEASE_RANK = {
    "dev": 0, "snapshot": 0, "snap": 0, "nightly": 0, "prealpha": 0,
    "alpha": 1, "a": 1,
    "beta": 2, "b": 2, "prebeta": 2,
    "rc": 3, "cr": 3, "pre": 4, "preview": 4,
}
RELEASE_RANK = 99


class VersionParseError(ValueError):
    """版本字符串无法解析。"""


@dataclass(frozen=True)
class Version:
    parts: tuple            # 数字段
    pre_kind: int | None    # None=正式版；否则为预发布等级
    pre_nums: tuple         # 预发布序号（"rc1"→(1,)，"alpha.2"→(2,)）
    raw: str

    def __str__(self):
        return self.raw

    def key(self):
        return (self.parts, RELEASE_RANK if self.pre_kind is None else self.pre_kind, self.pre_nums)


def _clean(s: str) -> str:
    """去 v 前缀、去已知 MC 版本前缀段（标签场景：1.7.10-0.8.0 → 0.8.0）。"""
    s = s.strip().lower()
    s = re.sub(r"^v(?=\d)", "", s)
    # 仅剥离"已知MC版本 + 分隔符"前缀，避免误伤 1.2.3-rc1 这类版本号
    for mc in KNOWN_MC_VERSIONS:
        if s.startswith(mc) and len(s) > len(mc) and s[len(mc)] in "-_.":
            s = s[len(mc) + 1:]
            break
    m = re.match(r"^mc(1\.\d{1,2}(?:\.\d{1,2})?)[\-_.]", s)
    if m and m.group(1) in _MC_SET:
        s = s[m.end():]
    return s


def parse_version(s) -> Version:
    """解析版本字符串。失败抛 VersionParseError。"""
    if s is None:
        raise VersionParseError("版本为空")
    raw = str(s).strip()
    if not raw:
        raise VersionParseError("版本为空")
    tokens = re.findall(r"\d+|[a-z]+", _clean(raw))
    if not any(t.isdigit() for t in tokens):
        raise VersionParseError(f"无法解析版本: {raw!r}")

    parts: list = []
    pre_kind = None
    pre_nums: list = []
    seen_pre = False  # 已进入预发布阶段（其后的数字算预发布序号）
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.isdigit():
            if seen_pre:
                pre_nums.append(int(t))
            else:
                parts.append(int(t))
        elif t == "p" and i + 1 < len(tokens) and tokens[i + 1].isdigit() and not seen_pre:
            # GTNH 补丁版本：0.2.0p05 → (0,2,0,5)，比 0.2.0 新
            i += 1
            parts.append(int(tokens[i]))
        else:
            rank = PRERELEASE_RANK.get(t)
            if rank is not None:
                seen_pre = True
                pre_kind = rank if pre_kind is None else max(pre_kind, rank)
            # 未知字母（gtnh/build 等）忽略
        i += 1
    return Version(tuple(parts), pre_kind, tuple(pre_nums), raw)


def compare(a, b) -> int:
    """比较两个版本（str 或 Version），返回 -1/0/1。"""
    va = a if isinstance(a, Version) else parse_version(a)
    vb = b if isinstance(b, Version) else parse_version(b)
    n = max(len(va.parts), len(vb.parts))
    ka = (va.parts + (0,) * (n - len(va.parts)),) + va.key()[1:]
    kb = (vb.parts + (0,) * (n - len(vb.parts)),) + vb.key()[1:]
    return -1 if ka < kb else (1 if ka > kb else 0)


def max_version(values) -> str | None:
    """取版本字符串列表中最新者；全部无法解析时返回 None。"""
    best = None
    for v in values:
        try:
            ver = parse_version(v)
        except VersionParseError:
            continue
        if best is None or compare(ver, best) > 0:
            best = ver
    return best.raw if best else None


def split_mc_mod_version(stem: str):
    """把 jar 文件名主干切成 (名字, MC版本|None, mod版本|None)。

    "GardenOfGlass-1.7.10-1.9.5" → ("GardenOfGlass", "1.7.10", "1.9.5")
    "NeverEnoughCharacters-Rework-1.7.10-2.0.0" → 名字含连字符也切得开（1.7.10 锚点）
    "FoamFix-0.10.2"（无MC段）→ ("FoamFix", None, "0.10.2")
    "MC1.7.10-FoamFix-0.10.2"（MC段在最前）→ ("FoamFix", "MC1.7.10", "0.10.2")
    """
    stem = (stem or "").strip()
    parts = stem.split("-")
    # 找 MC 锚点：先精确匹配已知 MC 版本表，其次通用模式；不能是最后一段
    mc_idx = None
    for i, p in enumerate(parts[:-1]):
        m = MC_VERSION_RE.match(p)
        if m and m.group(2) in _MC_SET:
            mc_idx = i
            break
    if mc_idx is None:
        for i, p in enumerate(parts[:-1]):
            if MC_VERSION_RE.match(p):
                mc_idx = i
                break
    if mc_idx is not None:
        mc = parts[mc_idx]
        if mc_idx == 0:
            # "MC1.7.10-FoamFix-0.10.2"：MC段在最前，名字取下一段
            name = parts[1] if len(parts) > 1 else None
            ver = "-".join(parts[2:]) or None
        else:
            name = "-".join(parts[:mc_idx]) or None
            ver = "-".join(parts[mc_idx + 1:]) or None
        return (name, mc, ver)
    # 无 MC 段：找第一个以版本号开头的段，其后整体像版本 → 视为 mod 版本。
    # 支持 "GTNHModify_CutCorners-v1.3.17+2.9.0-beta-1"（版本含 + 与多级后缀）
    for i, p in enumerate(parts):
        if i == 0:
            continue
        if re.match(r"^[vV]?\d", p):
            suffix = "-".join(parts[i:])
            if re.match(r"^[vV]?\d[\w.+]*(-[A-Za-z0-9.+]+)*$", suffix):
                return ("-".join(parts[:i]) or None, None, suffix)
    return (stem or None, None, None)
