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
from functools import cmp_to_key

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
    cleaned = _clean(raw)
    if not re.match(r"\d", cleaned):
        # 版本必须以数字开头：否则 "p3" 这类补丁名会被 GTNH 补丁规则
        # 误解析成"版本3"，排序时反而压过 v1.7.49
        raise VersionParseError(f"无法解析版本: {raw!r}")
    tokens = re.findall(r"\d+|[a-z]+", cleaned)
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


def _tie_key(s: str) -> tuple:
    """tie-break 排序键：数字段按数值、字母段按字典序（可跨类型比较）。"""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t)
                 for t in re.findall(r"\d+|[a-z]+", _clean(s)))


def order_key(s: str) -> tuple:
    """全序排序键：结构化键为主，同结构变体（v1.85 / v1.85-Multi / …Multiplayer）
    按 _tie_key 严格分先后。

    compare() 刻意把"多出未知后缀"判等（2.0.0-GTNH == 2.0.0），不可传递、
    不能用作排序比较器；排序展示请用本键。
    """
    v = parse_version(s)
    return (v.parts, RELEASE_RANK if v.pre_kind is None else v.pre_kind,
            v.pre_nums, _tie_key(v.raw))


def _tie_break(ra: str, rb: str) -> int:
    """结构化键相等时按原文打破平局。

    只在两侧"同一位置都有但值不同"的 token 上分先后（如 1.2.3s 与
    1.2.3t 两个构建、+build.5 与 +build.9）；一侧多出的未知后缀
    （2.0.0-GTNH vs 2.0.0、1.0.0 vs 1.0.0.0）维持判等，与旧语义一致。
    """
    ta, tb = _tie_key(ra), _tie_key(rb)
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else None
        y = tb[i] if i < len(tb) else None
        if x is None or y is None:
            continue
        if x != y:
            return -1 if x < y else 1
    return 0


def compare(a, b) -> int:
    """比较两个版本（str 或 Version），返回 -1/0/1。

    结构化键相等但原文不同时（未知字母 token 被忽略），见 _tie_break。
    """
    va = a if isinstance(a, Version) else parse_version(a)
    vb = b if isinstance(b, Version) else parse_version(b)
    n = max(len(va.parts), len(vb.parts))
    ka = (va.parts + (0,) * (n - len(va.parts)),) + va.key()[1:]
    kb = (vb.parts + (0,) * (n - len(vb.parts)),) + vb.key()[1:]
    if ka != kb:
        return -1 if ka < kb else 1
    if va.raw != vb.raw:
        return _tie_break(va.raw, vb.raw)
    return 0


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
    "advanced_memory_card-1.0.1-1.7.10-GTNH"（版本段在MC锚点前，name-modver-mcver 命名）
        → ("advanced_memory_card", "1.7.10", "1.0.1-1.7.10-GTNH")
    """
    name, mc, ver = _split_mc_mod_version_raw(stem)
    if ver:
        # 版本段只保留 ASCII 版本字符："dualhotbar-1.7.10-1.6[双层-超长快捷栏]"
        # 的版本是 1.6，方括号中文备注不是版本的一部分
        m = re.match(r"[A-Za-z0-9_.+-]+", ver)
        ver = (m.group(0).rstrip("-_.+") if m else "") or ver
    return (name, mc, ver)


def _split_mc_mod_version_raw(stem: str):
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
            # 通用锚点要求 MC 段之后是 ASCII 版本段："dualhotbar-1.61-超长快捷栏"
            # 的 1.61 不是 MC 版本，后面跟着的中文描述也不是版本号
            if MC_VERSION_RE.match(p) and parts[i + 1].isascii():
                mc_idx = i
                break
    if mc_idx is not None:
        mc = parts[mc_idx]
        if mc_idx == 0:
            # "MC1.7.10-FoamFix-0.10.2"：MC段在最前，名字取下一段
            name = parts[1] if len(parts) > 1 else None
            ver = "-".join(parts[2:]) or None
        elif re.match(r"^[vV]?\d", parts[mc_idx - 1]):
            # "advanced_memory_card-1.0.1-1.7.10-GTNH"：MC 锚点前一段像版本
            # → name-modver-mcver 命名，版本段在 MC 之前，整体保留为版本
            name = "-".join(parts[:mc_idx - 1]) or None
            ver = "-".join(parts[mc_idx - 1:]) or None
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
            # 版本段只认 ASCII（\w 会把中文算进去，导致"dualhotbar-1.61-超长快捷栏"
            # 整段被当成版本号）。ASCII 判定用显式字符集（含下划线），不用 \w。
            if re.match(r"^[vV]?[0-9][\w.+-]*(-[\w.+-]+)*$", suffix) and suffix.isascii():
                return ("-".join(parts[:i]) or None, None, suffix)
            if not suffix.isascii():
                # 版本段后跟着中文描述（"1.61-超长快捷栏"）→ 截取开头的 ASCII 版本
                m = re.match(r"^[vV]?[0-9][A-Za-z0-9_.+-]*", suffix)
                if m:
                    return ("-".join(parts[:i]) or None, None, m.group(0).rstrip("-_.+"))
    return (stem or None, None, None)
