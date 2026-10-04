"""资源包 / 光影包管理：目录识别、扫描、下载源解析、安装与更新。

与 mod 的差异（决定了本模块的形态）：

- **安装目标不同**：mod 是 ``mods/`` 下的 jar；资源包是 ``resourcepacks/`` 下的
  zip/文件夹，光影包是 ``shaderpacks/`` 下的 zip/文件夹。因此目录识别、扫描与
  安装流程都独立于 scanner/updater。
- **上游形态不同**：wiki 页面的链接指向 GitHub/Modrinth/CurseForge/Discord 等
  多种站点。GitHub Releases 资产与 Modrinth 版本文件可直接下载安装；
  CurseForge 需要官方 API key、Discord 需要登录，只能打开浏览器手动下载
  （安装页会给出准确的落盘目录）。见 ``AUTO_HOSTS``。
- **降级不自动执行**：与 mod 更新一致的保守边界——只有在能解析出版本且
  上游版本确实更高时才提示更新，无法判定时要求人工确认。

数据文件：``packs_db.json``（wiki 解析结果 + 用户别名/绑定源）。
"""
import functools
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import net, utils, versions
from .packs_wiki import KIND_RESOURCE, KIND_SHADER, KIND_LABELS

# ---- 包类型 ----
DIR_NAMES = {KIND_RESOURCE: "resourcepacks", KIND_SHADER: "shaderpacks"}
PACK_SUFFIXES = (".zip",)

# 可直接下载安装的站点（其余只能浏览器手动下载）
AUTO_HOSTS = ("github.com", "modrinth.com")
# 明确无法自动下载的站点 → 给用户的说明
MANUAL_HOST_NOTES = {
    "curseforge.com": "CurseForge 下载接口需要官方 API key，请在打开的页面手动下载",
    "discord.com": "Discord 附件需要登录，请在打开的页面手动下载",
    "web.archive.org": "该文件只存在于网络存档，请在打开的页面手动下载",
    "gitlab.com": "GitLab 暂不支持自动下载，请在打开的页面手动下载",
    "planetminecraft.com": "该站点不支持自动下载，请在打开的页面手动下载",
}
# GTNH 打包脚本生成的元数据资产，不是资源包本体
META_ASSET_RE = re.compile(r"^(gtnh-pack-update\.json|.*\.(json|txt|md|sha1|sig|asc|zs))$", re.I)


class PacksError(Exception):
    """资源包/光影管理相关错误（消息面向用户）。"""


def _safe_component(name: str) -> bool:
    """只接受 Windows 上的单个普通文件名，拒绝路径、设备名与数据流。"""
    return bool(name and name not in (".", "..")
                and not re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                and not name.endswith((".", " "))
                and not re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I))


# ---------- 目录识别 ----------
def default_resolution() -> str:
    return "auto"


def instance_root_from(any_dir: Path | None) -> Path | None:
    """从 mods / resourcepacks / shaderpacks 目录反推实例根目录。

    ``…/instance/mods`` → ``…/instance``；已经是实例根目录时原样返回。
    """
    if not any_dir:
        return None
    p = Path(any_dir)
    if p.name.lower() in ("mods", *(v.lower() for v in DIR_NAMES.values())):
        return p.parent
    return p


def resolve_pack_dir(cfg, kind: str) -> Path | None:
    """确定某类包的安装目录。

    优先级：设置页显式填写的路径 → 由客户端实例根目录推断
    （``<实例根>/resourcepacks``、``<实例根>/shaderpacks``）。
    目录只在「需要写入」时才要求存在，浏览列表不依赖它。
    """
    explicit = (cfg.data.get("pack_folders") or {}).get(kind) or ""
    if explicit.strip():
        return Path(explicit.strip())
    root = instance_root_from(cfg.client_mods_dir)
    return (root / DIR_NAMES[kind]) if root else None


def detect_pack_dirs(root) -> dict:
    """从所选目录探测 resourcepacks/shaderpacks（供设置页「自动检测」）。

    ``root`` 可以是实例根目录、``.minecraft``、或直接是某一类包目录。
    返回 ``{"resourcepacks": Path|None, "shaderpacks": Path|None, "root": Path|None}``。
    """
    res = {"resourcepacks": None, "shaderpacks": None, "root": None}
    p = Path(root) if root else None
    if not p or not p.exists():
        return res
    # 直接选中某一类包目录
    low = p.name.lower()
    for dirname in DIR_NAMES.values():
        if low == dirname and p.is_dir():
            res[dirname] = p
            res["root"] = p.parent
            return res
    if low == "mods" and p.is_dir():
        p = p.parent
    # 选中的已经是 .minecraft / minecraft 本身时，直接就在这里找
    candidates = [p]
    if low not in (".minecraft", "minecraft"):
        candidates += [p / ".minecraft", p / "minecraft"]
    for base in candidates:
        if not base.is_dir():
            continue
        found = False
        for kind, dirname in DIR_NAMES.items():
            cand = base / dirname
            if cand.is_dir():
                res[dirname] = cand        # 结果以目录名为键（见 detect_pack_dirs 文档）
                found = True
        if found and res["root"] is None:
            res["root"] = base
    return res


# ---------- 磁盘上的包 ----------
@dataclass
class PackFile:
    """resourcepacks/shaderpacks 里的一个包（zip 或文件夹）。"""
    path: Path
    file_name: str
    is_dir: bool
    version: str | None = None
    pack_id: str | None = None
    match_quality: str = "none"     # exact|contains|none
    valid: bool | None = None       # 结构校验结果（None=未检查）
    note: str = ""
    mtime: float = 0.0
    size: int = 0

    @property
    def ext(self) -> str:
        return "folder" if self.is_dir else self.path.suffix.lower()


def _zip_has(zip_path: Path, probes: tuple, *, top_only: bool = True) -> bool:
    """压缩包内是否存在某个条目。

    ``top_only=True`` 只看顶层（顶层目录/文件），``False`` 允许任意层级
    ——用于识别「多包了一层文件夹」的压缩包（wiki 明确提到的坑）。
    """
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/").lstrip("./")
                if not name:
                    continue
                if top_only:
                    if any(name == p or name.startswith(p) for p in probes):
                        return True
                elif any(p in name for p in probes):
                    return True
    except (OSError, ValueError, zipfile.BadZipFile, NotImplementedError):
        return False
    return False


def validate_pack(path: Path, kind: str) -> tuple[bool, str]:
    """静态检查包结构是否像资源包/光影包（不修改任何文件）。

    - 资源包：顶层 ``pack.mcmeta``（1.7.10 允许缺失）+ ``assets/``
    - 光影包：顶层 ``shaders/`` 目录（wiki 明确要求压缩包内直接含 shaders）
    文件夹形态同样按顶层条目判断。返回 (是否像, 说明)。
    """
    if path.is_dir():
        names = {p.name.lower() for p in path.iterdir()} if path.is_dir() else set()
        if kind == KIND_SHADER:
            return ("shaders" in names,
                    "" if "shaders" in names else "文件夹内没有 shaders/ 目录，光影可能无法加载")
        ok = "assets" in names or "pack.mcmeta" in names
        return (ok, "" if ok else "文件夹内没有 assets/ 或 pack.mcmeta")
    if path.suffix.lower() != ".zip":
        return False, f"不是 .zip 文件（{path.suffix or '无扩展名'}）"
    if kind == KIND_SHADER:
        probes = ("shaders/", "shaders")
        ok = _zip_has(path, probes)
        if ok:
            return True, ""
        # 允许 "包名/shaders/" 这类多一层包裹，但明确提示（wiki 提到此坑）
        if _zip_has(path, ("shaders/",), top_only=False):
            return True, "压缩包内多了一层文件夹，游戏可能读不到光影配置，建议解压后使用"
        return False, "压缩包内没有 shaders/ 文件夹，不是有效的光影包"
    probes = ("pack.mcmeta", "assets/")
    if _zip_has(path, probes) or _zip_has(path, probes, top_only=False):
        return True, ""
    return False, "压缩包内没有 pack.mcmeta 或 assets/，可能不是资源包"


# 资源包/光影包文件名里的版本号：``Shadow.UI.v5.45``、``DarkReimagined_2.0.1``、
# ``GTNH-Faithful-x32.v2.2.0-Reupload``、``Modernity-GTNH-Dark-UI-2026-09-28``、
# ``ComplementaryUnbound_r5.9.3``。jar 那套 split_mc_mod_version 按 ``-`` 切分并要求
# MC 锚点，对这里的 ``名字.版本`` 命名不适用（会把 "Shadow.UI.v5.45" 当成整个名字）。
#
# 版本段必须以 ``.``/``_``/``-`` 或字符串开头为界，这样 ``x32``、``GT5``、
# ``1_16`` 这类名字/分辨率残片不会被当成版本段起点。
# 空格也可以作为起点，但仅当其后紧跟「可选 v/b/r 前缀 + 数字 + 分隔符 + 数字」
# 的版本形态（``GT V1.0.0`` → ``V1.0.0``）：否则 ``GT V1.0.0`` 会从 ``1.0.0``
# 内部的 ``.`` 起匹配，读成 ``0.0``；而 ``Pack 4K``、``Some Pack 2`` 这类
# 空格后的普通数字不会误判成版本。
#
# 不要放宽成 ``(?:^|[^\w])``：那会匹配 ``…9.3`` 这种从数字中间开始的片段
# （``DarkReimagined_2.0.1`` → ``0.1``、``BSL_v10.1.8`` → ``1.8``）。
# 也不要用 lookbehind/``\b``——``_`` 属于 ``\w``，会把 ``_r5.9.3`` 的前导 ``r``
# 跳过而只取到 ``9.3``。
#
# 取第一个匹配而不是最后一个（``DarkReimagined_2.0.1`` 取最后一个会得到 ``0.1``）。
#
# 可选的装饰前缀只允许实际出现的 ``v``/``b``（GTNH Mod 常见）与 ``r``（Modrinth
# 光影），不放成任意字母：``[a-z]?`` 会让 ``GTNH-Faithful-x32.v2.2.0`` 把分辨率
# 残片 ``x32`` 当成版本段起点，读成 ``x32.v2.2.0``。
PACK_VERSION_RE = re.compile(
    r"(?:^|[._\-]|\s(?=[vbr]?\d+[.\-]\d))([vbr]?\d[\w]*(?:[.\-]\w+)*)", re.I)


def extract_pack_version(stem: str) -> str | None:
    """从资源包/光影包文件名主干提取版本号（取最后一个像版本的片段）。

    去掉版本后面跟着的纯文字标签：``…v2.2.0-Reupload`` → ``v2.2.0``，
    ``…v5.45-Modernity.version`` → ``v5.45``；纯数字段（日期 ``2026-09-28``、
    ``5.9.3``）全部保留，``beta``/``rc`` 这类预发布词也不算标签而保留。
    """
    best = None
    for m in PACK_VERSION_RE.finditer(stem or ""):
        candidate = _trim_version_tail(m.group(1))
        if candidate and any(c.isdigit() for c in candidate):
            best = candidate
    return best


# 明确属于版本一部分的字母段（预发布/补丁标记），不作为"后面跟着的标签"删除
VERSION_KEEP_WORDS = frozenset({
    "beta", "alpha", "rc", "pre", "preview", "dev", "snapshot", "gtnh", "release",
})


def _trim_version_tail(version: str) -> str:
    """从尾部逐个剥掉「没有数字且不是版本标记」的段。"""
    parts = re.split(r"([.\-_])", (version or "").strip())
    # parts = [seg, sep, seg, sep, ...]
    while len(parts) >= 3:
        seg = parts[-1]
        if not seg or any(c.isdigit() for c in seg) or seg.lower() in VERSION_KEEP_WORDS:
            break
        parts = parts[:-2]
    return "".join(parts).rstrip(".-_")


def _pack_tokens(s: str) -> list:
    """按非字母数字切词（统一小写）。"""
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t]


def _adjacent_bigrams(tokens: list) -> set:
    """相邻词拼接：``[outlined, ores]`` → ``{outlinedores}``。

    文件名常把条目名里的多个词连写（``GTNH-OutlinedOres``），拼接词让
    ``Outlined Ores`` 这种条目名可以整体命中。
    """
    return {tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)}


@functools.lru_cache(maxsize=50000)
def _merge_walk(f_tokens: tuple, f_idx: int, c_tokens: tuple, c_idx: int) -> bool:
    """条目名的连续多个词允许拼起来匹配文件名的一个连写词。

    必须带 lru_cache：「跳过文件名多出的词」这条分支会让同一组 (f_idx, c_idx)
    被反复展开，记忆化之前对大文件名（十几个词）会指数爆炸——实测扫描整个
    resourcepacks 目录能卡住几十秒。参数用 tuple 才能进缓存。
    """
    if c_idx >= len(c_tokens):
        return True
    if f_idx >= len(f_tokens):
        return False
    # 文件名多出的词（版本段等）可以跳过；只推进 f_idx，保证收敛
    if _merge_walk(f_tokens, f_idx + 1, c_tokens, c_idx):
        return True
    joined = ""
    for k in range(c_idx, len(c_tokens)):
        joined += c_tokens[k]
        ft = f_tokens[f_idx]
        if ft == joined or (len(joined) >= 5 and ft.startswith(joined)):
            if _merge_walk(f_tokens, f_idx + 1, c_tokens, k + 1):
                return True
    return False


def _sequential_coverage(cand_tokens: list, file_tokens: list, allow_prefix: bool) -> int:
    """顺序扫描文件名，返回被覆盖的条目词数（不要求全覆盖）。"""
    i = 0
    for ft in file_tokens:
        if i >= len(cand_tokens):
            break
        ct = cand_tokens[i]
        if ft == ct:
            i += 1
        elif allow_prefix and len(ct) >= 5 and (ft.startswith(ct) or ct.startswith(ft)):
            i += 1
    return i


def _ordered_coverage(cand_tokens: list, file_tokens: list,
                      cand_norm: str = "") -> tuple:
    """条目名按顺序覆盖文件名的情况 → (覆盖词数, 是否精确)。

    真实命名差异都靠这一层（见 test_packs_manager 的真实文件名回归）：
      * ``Modernity`` ``GTNH`` ``Dark`` ``UI``：严格逐词顺序
      * ``complementary`` ↔ ``complementaryunbound``：词是对方的子串
      * ``euphoria`` + ``patches`` ↔ ``euphoriapatches``：多词合并成一个连写词
      * ``outlined`` + ``ores`` ↔ ``outlinedores``：同上

    顺序很关键：``Modernity-GTNH`` 虽然两个词都在 ``Modernity-GTNH-Dark-UI-2.9.X``
    里，但只能覆盖 2 个词；后者能覆盖 4 个，于是更具体的条目名胜出。
    """
    if not cand_tokens or not file_tokens:
        return 0, False
    covered = _sequential_coverage(cand_tokens, file_tokens, False)
    if covered < len(cand_tokens):
        covered = max(covered,
                      _sequential_coverage(cand_tokens, file_tokens, True))
    if covered < len(cand_tokens):
        # 连写词：条目名整体出现在文件名里（Shadow.UI ← ShadowUI）
        if cand_norm and cand_norm in set(file_tokens) | _adjacent_bigrams(file_tokens):
            return len(cand_tokens), True
        if _merge_walk(tuple(file_tokens), 0, tuple(cand_tokens), 0):
            return len(cand_tokens), False
    return covered, covered == len(cand_tokens)


# 紧跟在包名后面、表示"这是同系列另一个包"的修饰词。
# 例：Modernity / Modernity-GTNH / Modernity-GTNH-Dark-UI / ModernityAdjunct 是
# 4 个不同的包，单词条目名 Modernity 不能把它们全吞掉。
SERIES_CONTINUATION_WORDS = ("gtnh", "adjunct", "addon", "extension", "extended",
                            "dark", "light", "plus", "legacy", "modern", "ui")


def _single_token_ok(cand_norm: str, bare_stem: str) -> bool:
    """单词条目名是否指向「去掉版本段的名字」这个包。

    单词条目名（自定义登记常见，如 ``Modernity``）只做"词是子串"会到处误配，
    因此要求它等于去版本名字的整段、首词或末词，且后面不跟着同系列的修饰词：

      * ``Modernity`` ← ``Modernity-f1``（首词）→ 通过
      * ``Complementary`` ← ``ComplementaryUnbound``（前缀后缀 ``unbound``
        不是系列修饰词）→ 通过
      * ``OutlinedOres`` ← ``GTNH-OutlinedOres``（末词）→ 通过
      * ``Modernity`` ← ``Modernity-GTNHAddon-…`` / ``ModernityAdjunct-f1``
        → 不通过（后面跟着 ``gtnh``/``adjunct``，是另一个包）
    """
    joined = re.sub(r"[^a-z0-9]+", "", (bare_stem or "").lower())
    if not joined:
        return False
    if joined == cand_norm:
        return True
    if joined.startswith(cand_norm):
        rest = joined[len(cand_norm):]
        if not rest:
            return True
        if not any(rest.startswith(w) for w in SERIES_CONTINUATION_WORDS):
            return True
    words = [w for w in re.split(r"[^A-Za-z0-9]+", (bare_stem or "").lower()) if w]
    return bool(words) and cand_norm == words[-1]


def match_score(cand_tokens: list, file_tokens: list,
                cand_norm: str, file_norm: str, *,
                bare_stem: str | None = None) -> tuple:
    """条目名与文件名的匹配打分 → (是否命中, 分数, 是否精确)。

    用**顺序覆盖**判定：条目名的词按顺序在文件名里逐个找到落点即为命中，
    连写词（``outlined``+``ores`` ↔ ``outlinedores``）与子串（``complementary``
    ↔ ``complementaryunbound``）都算。文件名校验用真实数据回归
    （见 test_packs_manager 的 TestMatchPack / 真实目录用例）：

      * ``Modernity-GTNH-Dark-UI-2.9.X.zip`` 必须落到 ``Modernity-GTNH-Dark-UI``
        而不是 ``Modernity-GTNH``（否则两个包折叠成一条，Dark-UI 永远显示未安装）
      * ``Modernity-f1-3.10.2.zip`` / ``ModernityAdjunct-f1-1.6.zip`` 不属于任何
        wiki 条目，必须不匹配（只命中 ``modern`` 一个词不算）
      * ``ComplementaryUnbound_r5.8.1 + EuphoriaPatches_1.9.3`` 要落到
        ``Complementary +Euphoria Patches``（多词合并成一个连写词）

    不接受「部分覆盖」作为命中：那会让 ``ModernityAdjunct…`` 被
    ``Modernity-GTNH-Dark-UI``（覆盖 1 个词但名字更长）抢走。

      200+ 归一化全等；120+ 覆盖全部条目词（词数越多越具体）
       95  文件名里含条目名整体；80/30 词级兜底（别名/缩写）
    """
    if not cand_tokens or not file_tokens:
        return False, 0.0, False
    if cand_norm and file_norm and cand_norm == file_norm:
        return True, 200.0 + len(cand_norm), True
    covered, exact = _ordered_coverage(cand_tokens, file_tokens, cand_norm)
    vocab = set(file_tokens) | _adjacent_bigrams(file_tokens)
    if covered == len(cand_tokens):
        # 单词条目名（自定义条目常见，如 "Modernity"）必须恰好覆盖"去掉版本段
        # 的名字"：否则 ``Modernity-GTNHAddon-OutlinedOres.zip`` 里
        # ``modernity`` 的确是个整词，会被它抢走。
        if len(cand_tokens) == 1 and len(cand_tokens[0]) >= 6 \
                and bare_stem is not None and not _single_token_ok(cand_norm, bare_stem):
            return False, 0.0, False
        return True, 120.0 + 5.0 * covered + len(cand_norm), exact
    if cand_norm and len(cand_norm) >= 6 and cand_norm in file_norm:
        return True, 95.0 + len(cand_norm), True
    hits = sum(1 for t in cand_tokens if t in vocab)
    frac = hits / len(cand_tokens)
    if hits >= 2 and frac >= 0.6:
        # frac 微调：同一批资产里名字更完整的胜出
        # （Modernity-GTNH 必须赢过 Modernity，两者都命中 2 个词）
        return True, 80.0 + 10.0 * hits + 2.0 * frac, False
    if hits == 1 and len(cand_tokens) == 1 and len(cand_tokens[0]) >= 6:
        return True, 30.0 + len(cand_tokens[0]), False
    return False, 0.0, False


def _bare_name(stem: str) -> str:
    """去掉版本段后的名字部分（``Shadow.UI.v5.45`` → ``Shadow.UI``）。"""
    ver = extract_pack_version(stem)
    if not ver:
        return stem
    idx = stem.lower().rfind(ver.lower())
    return stem[:idx].rstrip("._- ") if idx > 0 else stem


def _candidate_tokens(entry: dict) -> list:
    """参与匹配的候选名（英文名 + 用户关联过的别名）。"""
    out = []
    for s in [entry.get("name_en") or ""] + list(entry.get("aliases") or []):
        toks = _pack_tokens(s)
        if toks:
            out.append(toks)
    return out


def scan_pack_dir(folder: Path, kind: str, *, with_size: bool = True) -> list:
    """扫描 resourcepacks/shaderpacks 目录 → PackFile 列表（zip 与文件夹）。

    跳过隐藏项、``.disabled`` 与下载中断的 ``.part``；文件夹形态也计入
    （光影包与部分资源包常以解压后的文件夹存在）。

    ``with_size=False`` 时不算文件夹体积：列表/匹配只需要文件名，而真实
    resourcepacks 里常有几十 MB 的解压包，逐文件统计会让刷新明显卡顿。
    """
    out: list = []
    if not folder or not Path(folder).is_dir():
        return out
    try:
        items = sorted(Path(folder).iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for p in items:
        name = p.name
        if name.startswith(".") or name.endswith(".disabled") or name.endswith(".part"):
            continue
        try:
            if p.is_dir():
                rec = PackFile(path=p, file_name=name, is_dir=True)
                st = p.stat()
                rec.size = (sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                            if with_size else 0)
            elif p.suffix.lower() in PACK_SUFFIXES:
                rec = PackFile(path=p, file_name=name, is_dir=False)
                st = p.stat()
                rec.size = st.st_size if with_size else 0
            else:
                continue
        except OSError:
            continue
        rec.mtime = st.st_mtime
        stem = name[:-4] if name.lower().endswith(".zip") else name
        rec.version = extract_pack_version(stem)
        out.append(rec)
    return out


def match_pack(f: PackFile, entries: list) -> tuple[str | None, str]:
    """把磁盘上的包匹配到 db 条目，返回 (pack_id|None, quality)。

    资源包/光影包的命名与 jar 差别很大（``GTNH-OutlinedOres.2.1.-.Modernity.version.zip``、
    ``ComplementaryUnbound_r5.8.1 + EuphoriaPatches_1.9.3``），因此按
    「版本段剥离 + 顺序对齐 + 连写词合并」综合打分，取分数最高者：

      exact    — 归一化全等 / 全等词命中
      contains — 顺序对齐或词级多数命中

    打分而不是「首个命中」，否则 ``Modernity-GTNH-UI-2.9.X.zip`` 会被
    ``Modernity-GTNH`` 抢走，``Modernity-f1-3.10.2.zip`` 也会误配到它。
    """
    stem = f.file_name[:-4] if f.file_name.lower().endswith(".zip") else f.file_name
    file_tokens = _pack_tokens(stem)
    if not file_tokens:
        return None, "none"
    file_norm = "".join(file_tokens)
    bare = _bare_name(stem)
    bare_tokens = _pack_tokens(bare)
    bare_norm = "".join(bare_tokens)

    best: tuple | None = None
    for e in entries:
        for cand_tokens in _candidate_tokens(e):
            cand_norm = "".join(cand_tokens)
            hit, score, exact = match_score(cand_tokens, file_tokens, cand_norm, file_norm,
                                            bare_stem=bare)
            if not hit and bare_tokens and bare_norm != file_norm:
                # 版本段可能剥不干净（v2.9.X/v2.1 这种 tag），用去掉版本的名字再试
                hit, alt_score, exact = match_score(
                    cand_tokens, bare_tokens, cand_norm, bare_norm, bare_stem=bare)
                score = alt_score if hit else score
            if not hit:
                continue
            # score 相同时按条目名长度决胜：更长的条目名更具体
            # （Modernity-GTNH 必须胜过单词条目的 Modernity，
            #   Modernity-GTNH-Dark-UI 必须胜过 Modernity-GTNH；
            #   否则多个包会折叠成一条记录，更细的那条永远显示不出来）
            key = (score, len(cand_norm))
            if best is None or key > best[0]:
                best = (key, e["id"], exact)
    if best is None:
        return None, "none"
    return best[1], ("exact" if best[2] else "contains")


def _slug(s: str) -> str:
    """英文名 → id 片段（``Modernity Adjunct`` → ``modernity-adjunct``）。"""
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out or "pack"


def unmatched_packs(cfg, db: "PacksDB", kind: str | None = None) -> list:
    """安装目录里存在、但没匹配到任何条目的包。

    返回 ``[(kind, PackFile)]``；供界面提示「这些包工具不认识，
    可登记为自定义资源包以跟踪更新」。
    """
    out: list = []
    for k in ([kind] if kind else list(DIR_NAMES)):
        folder = resolve_pack_dir(cfg, k)
        if not folder:
            continue
        entries = db.by_kind(k)
        for f in scan_pack_dir(folder, k, with_size=False):
            pid, _q = match_pack(f, entries)
            if not pid:
                out.append((k, f))
    return out


def suggest_pack_name(file_name: str) -> str:
    """从未匹配文件名里给一个可编辑的建议名（去掉版本段）。"""
    stem = file_name
    for suffix in (".zip", ".disabled"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    bare = _bare_name(stem)
    return re.sub(r"[._\-]+", " ", bare).strip() or stem


# ---------- PacksDB ----------
class PacksDB:
    """packs_db.json：wiki 解析结果 + 用户别名/绑定源 + 安装记录。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.packs: list = []
        self.meta: dict = {}
        self.load()

    def load(self):
        data = utils.load_json(self.path, None)
        if not data:
            bak = self.path.with_suffix(self.path.suffix + ".bak")
            recovered = utils.load_json(bak, None)
            if recovered:
                self.packs = [p for p in (recovered.get("packs") or []) if isinstance(p, dict)]
                self.meta = recovered.get("meta") or {}
                utils.atomic_write_json(
                    self.path, {"version": 1, "meta": self.meta, "packs": self.packs})
                utils.append_log(self.path.parent,
                                 "packs_db.json 缺失或损坏，已从 packs_db.json.bak 自动恢复")
                return
            self.packs, self.meta = [], {}
            return
        self.packs = [p for p in (data.get("packs") or []) if isinstance(p, dict)]
        self.meta = data.get("meta") or {}

    def save(self, backup: bool = False):
        if backup:
            utils.backup_file(self.path)
        utils.atomic_write_json(
            self.path, {"version": 1, "meta": self.meta, "packs": self.packs})

    # ---- 查询 ----
    def get(self, pack_id: str):
        for p in self.packs:
            if p["id"] == pack_id:
                return p
        return None

    def all(self) -> list:
        return list(self.packs)

    def by_kind(self, kind: str | None = None, *, include_removed: bool = False) -> list:
        out = []
        for p in self.packs:
            if not include_removed and p.get("wiki_removed"):
                continue
            if kind and p.get("kind") != kind:
                continue
            out.append(p)
        return out

    def installable(self, kind: str | None = None) -> list:
        """可下载/安装的条目（排除整合包自带的只读条目）。"""
        return [p for p in self.by_kind(kind) if not p.get("local")]

    def by_category(self, kind: str) -> dict:
        result: dict = {}
        for p in self.by_kind(kind):
            result.setdefault(p.get("category") or "?", []).append(p)
        return result

    def categories(self, kind: str) -> list:
        order = list(self.by_category(kind).keys())
        # 自带资源包固定排在最后
        order.sort(key=lambda c: (c == "游戏自带资源包", c))
        return order

    def add_alias(self, pack_id: str, alias: str) -> None:
        p = self.get(pack_id)
        if not p or not alias:
            return
        if alias not in p.setdefault("aliases", []):
            p["aliases"].append(alias)
            self.save()

    def add_custom(self, entry: dict) -> str:
        """登记一个 wiki 之外的资源包/光影包（组=自定义）。

        典型场景：包里装的 ``Modernity-f1-3.10.2.zip``、``ModernityAdjunct-f1-1.6.zip``
        来自 Modrinth，wiki 页面根本没收录，但用户希望能跟踪更新。
        ``source_url`` 填 GitHub / Modrinth 地址即可自动检查与下载。
        """
        kind = entry.get("kind")
        if kind not in DIR_NAMES:
            raise PacksError(f"未知的包类型: {kind}")
        name_en = (entry.get("name_en") or "").strip()
        if not name_en:
            raise PacksError("英文名不能为空（用于匹配已装文件）")
        url = (entry.get("source_url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            if re.fullmatch(r"[\w.\-]+/[\w.\-]+", url):
                url = f"https://github.com/{url}"
            else:
                raise PacksError("下载源需为完整链接（https://...）或 owner/repo")
        base = f"custom-{_slug(name_en)}"
        pack_id, n = base, 2
        while self.get(pack_id):
            pack_id = f"{base}-{n}"
            n += 1
        urls = {"links": [], "github": None, "modrinth": None,
                "curseforge": None, "primary": None, "direct": []}
        if url:
            sites = {"github": "github.com", "modrinth": "modrinth.com",
                     "curseforge": "curseforge.com"}
            key = next((k for k, dom in sites.items() if dom in url), None)
            if key:
                urls[key] = url
            urls["links"] = [{"url": url, "label": "下载源"}]
            urls["primary"] = url
            if not host_note(url):
                urls["direct"] = [url]
        self.packs.append({
            "id": pack_id,
            "kind": kind,
            "name_en": name_en,
            "name_cn": (entry.get("name_cn") or "").strip(),
            "category": entry.get("category") or "自定义",
            "author": (entry.get("author") or "").strip(),
            "scope": "", "loader": "",
            "desc": entry.get("desc") or "",
            "detail": entry.get("desc") or "",
            "version_requirements": [],
            "icon": None,
            "urls": urls,
            "aliases": list(entry.get("aliases") or []),
            "local": False,
            "custom": True,
            "wiki_removed": False,
            "bound_source": url or None,
            "source_override": bool(url),
        })
        self.save(backup=True)
        utils.append_log(self.path.parent,
                         f"登记自定义资源包/光影 {name_en}（类型 {kind}，源 {url or '无'}）")
        return pack_id

    def remove_custom(self, pack_id: str) -> bool:
        p = self.get(pack_id)
        if not p or not p.get("custom"):
            return False
        self.packs.remove(p)
        self.remove_installed(p.get("kind"), pack_id, save=False)
        self.save(backup=True)
        utils.append_log(self.path.parent, f"删除自定义资源包/光影 {p.get('name_en') or pack_id}")
        return True

    def update_entry(self, pack_id: str, fields: dict) -> bool:
        p = self.get(pack_id)
        if not p:
            return False
        for k, v in fields.items():
            if k in ("id", "kind", "wiki_removed"):
                continue
            p[k] = v
        self.save(backup=True)
        return True

    # ---- 安装记录（跟随磁盘现状，见 reconcile_installed）----
    def installed(self, kind: str) -> dict:
        return dict((self.meta.get("installed") or {}).get(kind) or {})

    def set_installed(self, kind: str, pack_id: str, fields: dict, *, save: bool = True) -> None:
        rec = self.meta.setdefault("installed", {}).setdefault(kind, {})
        cur = rec.get(pack_id) or {}
        cur.update(fields)
        rec[pack_id] = cur
        if save:
            self.save()

    def remove_installed(self, kind: str, pack_id: str, *, save: bool = True) -> None:
        rec = (self.meta.get("installed") or {}).get(kind) or {}
        if pack_id in rec:
            rec.pop(pack_id)
            if save:
                self.save()

    # ---- wiki 合并 ----
    def merge_wiki(self, fresh: list, *, update_fetched_at: bool = True) -> list:
        """合并新解析的 wiki 条目；保留用户别名/绑定源。返回变更说明列表。"""
        if not fresh:
            raise ValueError("资源包/光影解析结果为空，已取消合并（本地数据未改动）。"
                             "请稍后重试；若反复出现，可能是页面结构变更，需更新解析器")
        changes: list = []
        old_by_id = {p["id"]: p for p in self.packs}
        fresh_ids = {p["id"] for p in fresh}
        new_packs: list = []
        for fp in fresh:
            old = old_by_id.get(fp["id"])
            if old:
                fp["aliases"] = list(dict.fromkeys(
                    (old.get("aliases") or []) + (fp.get("aliases") or [])))
                if old.get("source_override"):
                    fp["source_override"] = True
                    fp["bound_source"] = old.get("bound_source")
                    fp["asset_regex"] = old.get("asset_regex")
                elif old.get("bound_source"):
                    fp["bound_source"] = old["bound_source"]
                for k, label in (("category", "分类"), ("loader", "加载器"),
                                 ("scope", "范围"), ("author", "作者")):
                    if old.get(k) != fp.get(k):
                        changes.append(f'{fp.get("name_en") or fp["id"]}: {label} '
                                       f'{old.get(k) or "—"} → {fp.get(k) or "—"}')
                if old.get("urls") != fp.get("urls"):
                    changes.append(f'{fp.get("name_en") or fp["id"]}: 下载链接已更新')
                if any(old.get(k) != fp.get(k) for k in ("desc", "detail", "icon")):
                    changes.append(f'{fp.get("name_en") or fp["id"]}: Wiki 说明已更新')
            else:
                changes.append(f'新增: {fp.get("name_en") or fp["id"]}'
                               f'（{KIND_LABELS.get(fp.get("kind"), fp.get("kind"))} / '
                               f'{fp.get("category")}）')
            new_packs.append(fp)
        for p in self.packs:
            if p["id"] not in fresh_ids:
                # 自定义条目不是从 wiki 来的，wiki 里「没有」是常态，
                # 不能打 wiki_removed（否则会被 by_kind 过滤掉、从界面消失）
                if not p.get("custom") and not p.get("wiki_removed"):
                    p["wiki_removed"] = True
                    changes.append(f'移除: {p.get("name_en") or p["id"]}（wiki 已删除，本地保留记录）')
                new_packs.append(p)
        self.packs = new_packs
        if update_fetched_at:
            self.meta["wiki_fetched_at"] = utils.now_str()
        else:
            self.meta["wiki_imported_at"] = utils.now_str()
        self.save(backup=True)
        return changes


# ---------- 下载源解析 ----------
@dataclass
class PackAsset:
    """一个可下载的候选资产。"""
    url: str
    file_name: str
    version: str | None = None
    tag: str | None = None
    published_at: str | None = None
    prerelease: bool = False
    size: int = 0
    source: str = ""            # github|modrinth
    note: str = ""


@dataclass
class PackSourceResult:
    """一次下载源查询结果。"""
    source: str                 # github|modrinth|manual
    status: str                 # ok|none|unsupported|error
    assets: list = field(default_factory=list)
    latest_version: str | None = None
    latest_tag: str | None = None
    published_at: str | None = None
    note: str = ""
    url: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok" and bool(self.assets)


def _host_of(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()


def host_note(url: str) -> str:
    """不可自动下载的站点 → 给用户的说明（可自动下载时返回空串）。"""
    host = _host_of(url)
    for dom, note in MANUAL_HOST_NOTES.items():
        if host == dom or host.endswith("." + dom):
            return note
    if any(host == h or host.endswith("." + h) for h in AUTO_HOSTS):
        return ""
    return "该站点不支持自动下载，请在打开的页面手动下载"


def entry_auto_url(entry: dict) -> str | None:
    """条目的首选自动下载地址（GitHub / Modrinth），没有则 None。"""
    urls = entry.get("urls") or {}
    bound = entry.get("bound_source")
    if bound and not host_note(bound):
        return bound
    candidates = []
    if entry.get("source_type") == "github" and (entry.get("source") or {}).get("owner"):
        src = entry["source"]
        candidates.append(f'https://github.com/{src["owner"]}/{src["repo"]}')
    candidates += [urls.get("github"), urls.get("modrinth")]
    for c in candidates:
        if c and not host_note(c):
            return c
    for link in (urls.get("direct") or []):
        if link and not host_note(link):
            return link
    return None


def _release_groups(assets: list) -> list:
    """按「同一次发布」给资产分组，最新的发布排在最前。

    GitHub 的 ``/releases`` 与 Modrinth 的 ``/version`` 都按时间倒序返回，
    因此首次出现的分组即最新发布。同名 tag 的资产（同一次发布的多个文件）
    归为一组，跨发布顺序不受影响。
    """
    order: list = []
    groups: dict = {}
    for a in assets:
        key = (a.tag or "", a.published_at or "")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(a)
    return [groups[k] for k in order]


def _pick_asset(assets: list, entry: dict, *, asset_regex: str = "") -> PackAsset | None:
    """从候选资产中挑一个：显式正则 > 最新一次发布 > 条目名匹配 > 唯一候选。

    多资产仓库常见（Shadow.UI.v5.45.zip 与 Shadow.UI.v5.45-Modernity.version.zip），
    默认必须给出确定答案而不是随机取第一个；无法确定时返回 None，
    由上层要求用户选择。

    「最新一次发布」这一层很关键：Modrinth 的 ``/version`` 会返回该项目**全部**
    历史版本的文件（Complementary 有 25 个、BSL 有 49 个），只看"候选总数"
    永远判为歧义、无法一键安装。先收敛到最新发布，再看该次发布里是否唯一。
    """
    real = [a for a in assets if not META_ASSET_RE.match(a.file_name or "")]
    if not real:
        return None
    if asset_regex:
        try:
            rx = re.compile(asset_regex, re.I)
        except re.error as e:
            raise PacksError(f"资产筛选正则无效: {e}")
        matched = [a for a in real if rx.search(a.file_name)]
        if matched:
            return matched[0]     # 用户显式绑定优先于任何自动推断
        # 绑定过却匹配不上（上游改名了）：在最新一次发布里再试，
        # 仍匹配不上返回 None 让用户重新选择——绝不悄悄换成别的文件
        latest_binding = [a for a in _release_groups(real)[0] if rx.search(a.file_name)]
        return latest_binding[0] if len(latest_binding) == 1 else None

    groups = _release_groups(real)
    latest = groups[0]
    name_tokens = _pack_tokens(entry.get("name_en") or "")

    def name_hits(candidates: list) -> list:
        if not name_tokens:
            return []
        cand_norm = "".join(name_tokens)
        scored = []
        for a in candidates:
            ftokens = _pack_tokens(a.file_name)
            hit, score, _exact = match_score(name_tokens, ftokens, cand_norm,
                                             "".join(ftokens))
            if hit:
                scored.append((score, -len(a.file_name), a))
        if not scored:
            return []
        best = max(scored, key=lambda x: (x[0], x[1]))
        # 同分多个候选（Shadow.UI…zip 与 …-Modernity.version.zip）不猜
        return [s[2] for s in scored if s[0] == best[0]]

    hit = name_hits(latest)
    if len(hit) == 1:
        return hit[0]
    if len(latest) == 1:
        return latest[0]
    # 最新发布里确实有多个文件且无法确定 → 要求用户绑定
    return None


def list_assets(cfg, entry: dict, *, force: bool = False) -> PackSourceResult:
    """查询条目的可下载资产。

    GitHub 与 Modrinth 都走 ``net.http_get_cached``（ETag + TTL），
    重复检查更新不会反复消耗配额。无法自动下载的条目返回 status="unsupported"。
    """
    url = entry_auto_url(entry)
    if not url:
        primary = (entry.get("urls") or {}).get("primary")
        note = host_note(primary) if primary else "该条目没有可用下载链接"
        return PackSourceResult(source="manual", status="unsupported", note=note, url=primary or "")
    host = _host_of(url)
    if host == "github.com" or host.endswith(".github.com"):
        return _github_assets(cfg, url, force=force)
    if host == "modrinth.com" or host.endswith(".modrinth.com"):
        return _modrinth_assets(cfg, url, force=force)
    return PackSourceResult(source="manual", status="unsupported",
                            note=host_note(url), url=url)


def _github_assets(cfg, url: str, *, force: bool = False) -> PackSourceResult:
    from .wiki import github_repo_from_url
    repo = github_repo_from_url(url)
    if not repo:
        return PackSourceResult(source="manual", status="unsupported", url=url,
                                note="该 GitHub 链接不是仓库地址，只能手动下载")
    owner, name = repo
    api = f"https://api.github.com/repos/{owner}/{name}/releases?per_page=15"
    cache_file = cfg.cache_dir / f"packgh_{owner}_{name}.json".replace("/", "_")
    headers = {"Accept": "application/vnd.github+json"}
    if cfg.github_token:
        headers["Authorization"] = f"Bearer {cfg.github_token}"
    try:
        data, src = net.http_get_cached(
            api, cache_file=cache_file, ttl_hours=cfg.check_interval_hours,
            headers=headers, proxy=cfg.proxy, force=force)
    except net.HttpError as e:
        if e.code == 404:
            return PackSourceResult(source="github", status="none", url=url,
                                    note="仓库没有 Release，只能从源码页手动下载")
        return PackSourceResult(source="github", status="error", url=url,
                                note=f"GitHub 查询失败: {e}")
    releases = data if isinstance(data, list) else []
    assets: list = []
    for rel in releases:
        tag = rel.get("tag_name") or ""
        published = rel.get("published_at") or rel.get("created_at") or ""
        for a in (rel.get("assets") or []):
            fname = a.get("name") or ""
            if not fname or META_ASSET_RE.match(fname):
                continue
            if not a.get("browser_download_url"):
                continue
            assets.append(PackAsset(
                url=a["browser_download_url"], file_name=fname,
                version=tag or None, tag=tag, published_at=published,
                prerelease=bool(rel.get("prerelease")), size=int(a.get("size") or 0),
                source="github", note=f"Release {tag}" if tag else ""))
    if not assets:
        return PackSourceResult(source="github", status="none", url=url,
                                note="Release 中没有可下载的资源包附件，请从源码页手动下载")
    latest = releases[0] if releases else {}
    return PackSourceResult(
        source="github", status="ok", assets=assets,
        latest_version=(latest.get("tag_name") or None),
        latest_tag=latest.get("tag_name"),
        published_at=latest.get("published_at") or latest.get("created_at"),
        url=url, note=f"GitHub {owner}/{name}（{src}）")


def _modrinth_assets(cfg, url: str, *, force: bool = False) -> PackSourceResult:
    from .packs_wiki import modrinth_ref
    ref = modrinth_ref(url)
    if not ref:
        return PackSourceResult(source="manual", status="unsupported", url=url,
                                note="无法识别 Modrinth 项目，请手动下载")
    ptype, slug = ref
    api = f"https://api.modrinth.com/v2/project/{slug}/version"
    cache_file = cfg.cache_dir / f"packmr_{slug}.json"
    try:
        data, src = net.http_get_cached(
            api, cache_file=cache_file, ttl_hours=cfg.check_interval_hours,
            headers={"Accept": "application/json"}, proxy=cfg.proxy, force=force)
    except net.HttpError as e:
        return PackSourceResult(source="modrinth", status="error", url=url,
                                note=f"Modrinth 查询失败: {e}")
    versions_list = data if isinstance(data, list) else []
    assets: list = []
    for v in versions_list:
        ver = v.get("version_number") or ""
        published = v.get("date_published") or ""
        for f in (v.get("files") or []):
            fname = f.get("filename") or ""
            if not f.get("url"):
                continue
            assets.append(PackAsset(
                url=f["url"], file_name=fname, version=ver or None, tag=ver,
                published_at=published, size=int(f.get("size") or 0),
                source="modrinth", note=f"版本 {ver}" if ver else ""))
    if not assets:
        return PackSourceResult(source="modrinth", status="none", url=url,
                                note="Modrinth 项目没有可下载文件")
    latest_ver = assets[0].version if assets else None
    return PackSourceResult(
        source="modrinth", status="ok", assets=assets,
        latest_version=latest_ver, latest_tag=latest_ver,
        published_at=assets[0].published_at, url=url,
        note=f"Modrinth {ptype}/{slug}（{src}）")


def pick_asset(result: PackSourceResult, entry: dict) -> PackAsset | None:
    """按条目名从查询结果中挑出默认资产（多候选返回 None 要求用户选择）。"""
    if not result or not result.ok:
        return None
    return _pick_asset(result.assets, entry,
                       asset_regex=(entry.get("asset_regex") or ""))


def source_kind(url: str) -> str:
    """URL 属于哪种下载源：``github`` / ``modrinth`` / ``manual``。"""
    host = _host_of(url)
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "modrinth.com" or host.endswith(".modrinth.com"):
        return "modrinth"
    return "manual"


def set_download_source(db: PacksDB, pack_id: str, url: str, *,
                        asset_regex: str | None = None) -> dict:
    """把条目绑定到用户指定的下载源（覆盖 wiki 上的链接）。

    wiki 的链接会过时：``Modernity-GTNH`` 页面写的是 ``github.com/ModernityGTNH``
    组织页 + ``ABKQPO/Modernity-GTNH``，前者不是仓库、后者不是当前发布页，
    自动解析必然失败。绑定后 ``entry_auto_url`` 优先返回该地址，
    解绑（url 传空）即恢复用 wiki 链接。

    返回 ``{"kind","note","repo"}``；repo 为 (owner, repo) 或 None。
    """
    entry = db.get(pack_id)
    if not entry:
        raise PacksError(f"条目不存在: {pack_id}")
    url = (url or "").strip()
    if not url:
        db.update_entry(pack_id, {"bound_source": None, "source_override": False,
                                  "asset_regex": None})
        utils.append_log(db.path.parent, f"解除下载源绑定 {pack_id}")
        return {"kind": "cleared", "note": "已恢复使用 wiki 上的链接", "repo": None}
    if not url.startswith(("http://", "https://")):
        # 允许直接粘贴 "owner/repo"
        if re.fullmatch(r"[\w.\-]+/[\w.\-]+", url):
            url = f"https://github.com/{url}"
        else:
            raise PacksError("请输入完整链接（https://...）或 owner/repo 形式的仓库")
    kind = source_kind(url)
    from .wiki import github_repo_from_url
    repo = github_repo_from_url(url) if kind == "github" else None
    if kind == "github" and not repo:
        # 组织页/非仓库页：GitHub 上同名仓库可省略仓库名，但必须能取出 owner/repo
        m = re.match(r"https?://github\.com/([\w.\-]+)/?$", url)
        if not m:
            raise PacksError(
                "该 GitHub 链接不是仓库地址（如 https://github.com/作者/仓库）。"
                "组织页请改用具体仓库链接")
        repo = (m.group(1), m.group(1))
    note = ""
    if kind == "modrinth":
        from .packs_wiki import modrinth_ref
        if not modrinth_ref(url):
            raise PacksError("无法从该 Modrinth 链接识别项目，请使用项目页链接")
    if kind == "manual":
        note = host_note(url)
    fields = {"bound_source": url, "source_override": True}
    if asset_regex is not None:
        fields["asset_regex"] = asset_regex
    db.update_entry(pack_id, fields)
    utils.append_log(
        db.path.parent,
        f"绑定下载源 {pack_id} → {url}" + (f"（{note}）" if note else ""))
    return {"kind": kind, "note": note, "repo": repo}


# ---------- 版本比较 ----------
def version_status(local: str | None, remote: str | None) -> str:
    """比较已装版本与上游版本。

    newer / same / older / unknown。``unknown`` 表示无法判定（缺版本号、
    或上游把非版本串当作 tag，如 ``Stable``/``machines``/``weekly-2026-09-28``），
    调用方必须交给人工判断，不能据此宣称"有新版"。
    """
    if not remote or not _looks_like_version(remote):
        return "unknown"
    if not local or not _looks_like_version(local):
        return "unknown"
    try:
        c = versions.compare(_normalize_version(local), _normalize_version(remote))
    except versions.VersionParseError:
        return "unknown"
    if c < 0:
        return "newer"
    if c > 0:
        return "older"
    return "same"


# 单个字母+数字的装饰前缀：Modrinth 光影版本 ``r5.9.3``、某些包 ``b1.2``。
# 仅在前缀只有一个字母时剥离，避免把 ``beta1``/``rc2`` 这类预发布标记拆坏。
DECORATIVE_VERSION_PREFIX_RE = re.compile(r"^([a-z])(?=\d)", re.I)


def _normalize_version(s):
    """归一化版本串以便比较：剥掉装饰性单字母前缀（``r5.9.3`` → ``5.9.3``）。

    ``v`` 前缀由 versions.parse_version 自己处理；这里只补它不认识的形态。
    """
    text = str(s or "").strip()
    m = DECORATIVE_VERSION_PREFIX_RE.match(text)
    if m and m.group(1).lower() != "v":
        text = text[1:]
    return text


def normalize_version(s) -> str | None:
    """对外暴露的版本归一化（安装记录与列表展示用）。"""
    if not s:
        return None
    text = _normalize_version(s)
    return text or None


def _looks_like_version(s) -> bool:
    """该字符串是否真的是版本号（用版本解析器验证，而非只看到数字）。

    ``v5.45``/``2.2.0``/``r5.9.3`` → True；
    ``Stable``/``machines``/``weekly-2026-09-28`` → False（上游把这些当 tag，
    拿来做新旧比较只会产生假更新）。
    """
    try:
        versions.parse_version(_normalize_version(s))
    except versions.VersionParseError:
        return False
    return True


# ---------- 备份 ----------
def backup_dir(cfg, kind: str, pack_id: str) -> Path:
    if not _safe_component(pack_id):
        raise PacksError(f"无效的条目 id: {pack_id}")
    return cfg.backup_dir / DIR_NAMES[kind] / pack_id


def backup_existing(cfg, kind: str, pack_id: str, source: Path) -> Path | None:
    """安装/更新前把已存在的同名包移入备份目录（保持文件名）。

    备份按 ``backup/<resourcepacks|shaderpacks>/<条目id>/`` 存放，同名文件
    加时间戳后缀，因此「多个同前缀文件」与「多次更新同一文件」都不会互相覆盖。
    """
    if not source.exists():
        return None
    dest = backup_dir(cfg, kind, pack_id)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / source.name
    if target.exists():
        stamp = utils.timestamp_str()
        base = f"{source.stem}.{stamp}" if not source.is_dir() else f"{source.name}.{stamp}"
        suffix = source.suffix if not source.is_dir() else ""
        target = dest / f"{base}{suffix}"
        n = 2
        while target.exists():
            target = dest / f"{base}.{n}{suffix}"
            n += 1
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    # copy2 保留源文件的旧 mtime；轮换应按备份创建时间，而非包的发布时间。
    os.utime(target, None)
    _prune_backups(dest, cfg.backup_keep)
    return target


def _prune_backups(folder: Path, keep: int) -> None:
    """只保留最近 keep 份（按 mtime），避免备份无限增长。"""
    try:
        keep = max(1, int(keep))
        items = sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except (OSError, ValueError):
        return
    for old in items[keep:]:
        try:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink()
        except OSError:
            pass


def list_backups(cfg, kind: str | None = None) -> list:
    """列出备份：``[{kind, pack_id, path, file_name, size, mtime}]``。

    ``kind`` 可传包类型（``resourcepack``）或目录名（``resourcepacks``）。
    """
    root = cfg.backup_dir
    out: list = []
    kinds = [DIR_NAMES.get(kind, kind) if kind else None] if kind else list(DIR_NAMES.values())
    for dirname in [k for k in kinds if k]:
        base = root / dirname
        if not base.is_dir():
            continue
        for pack_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not pack_dir.is_dir():
                continue
            for item in sorted(pack_dir.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if item.is_dir():
                        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                    else:
                        size = item.stat().st_size
                    st = item.stat()
                except OSError:
                    continue
                out.append({"kind": dirname, "pack_id": pack_dir.name,
                            "path": item, "file_name": item.name,
                            "size": size, "mtime": st.st_mtime})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def restore_backup(cfg, record: dict, target_dir: Path) -> Path:
    """把备份还原到安装目录（覆盖同名）。返回落盘路径。"""
    src: Path = record["path"]
    if not src.exists():
        raise PacksError(f"备份不存在: {src}")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / record["file_name"]
    if src.is_dir():
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
    else:
        if dest.exists():
            dest.unlink()
        shutil.copy2(src, dest)
    utils.append_log(cfg.data_dir, f"恢复资源包备份 {record['file_name']} → {dest}")
    return dest


# ---------- 安装/更新 ----------
def install_asset(cfg, entry: dict, asset, *, progress_cb=None,
                  target_dir: Path | None = None, previous_file: Path | None = None) -> Path:
    """下载并安装一个资产到 resourcepacks/shaderpacks。

    下载到缓存目录后再落盘（避免半成品直接进游戏目录）；已存在的同名包
    先移入备份。返回最终路径。
    """
    kind = entry.get("kind")
    if kind not in DIR_NAMES:
        raise PacksError(f"未知的包类型: {kind}")
    folder = Path(target_dir) if target_dir else resolve_pack_dir(cfg, kind)
    if not folder:
        raise PacksError(
            f"未设置{DIR_NAMES[kind]}目录，无法安装。请在设置中填写客户端整合包目录，"
            f"或直接指定 {DIR_NAMES[kind]} 文件夹")
    file_name = getattr(asset, "file_name", None) or Path(str(getattr(asset, "url", ""))).name
    if not file_name or "." not in file_name:
        file_name = f"{entry.get('name_en') or entry['id']}.zip"
    if not _safe_component(file_name) or Path(file_name).suffix.lower() != ".zip":
        raise PacksError(f"无效的资源包文件名: {file_name}")
    if not _safe_component(entry['id']):
        raise PacksError(f"无效的条目 id: {entry['id']}")
    cache = cfg.cache_dir / "packs" / f"{entry['id']}__{file_name}"
    url = getattr(asset, "url", None) or str(asset)
    net.download(url, cache, proxy=cfg.proxy, progress_cb=progress_cb)
    valid, note = validate_pack(cache, kind)
    if not valid:
        raise PacksError(f"下载文件校验失败: {note}")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / file_name
    if dest.is_symlink() or dest.resolve().parent != folder.resolve():
        raise PacksError(f"安装目标超出指定目录: {dest}")
    if previous_file is not None:
        backup_existing(cfg, kind, entry["id"], previous_file)
    backup_existing(cfg, kind, entry["id"], dest)
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".pack-", suffix=".part", delete=False) as f:
            staging = Path(f.name)
        shutil.copy2(cache, staging)
        os.replace(staging, dest)
    except OSError as e:
        raise PacksError(f"写入 {dest} 失败（游戏/服务端可能正在运行）: {e}")
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
    version = (normalize_version(getattr(asset, "version", None))
               or entry.get("installed_version"))
    utils.append_log(
        cfg.data_dir,
        f"安装{KIND_LABELS.get(kind, kind)} {entry.get('name_en') or entry['id']} → {dest}"
        + (f"（结构校验: {note}）" if note else ""))
    entry["_last_install"] = {"path": str(dest), "file_name": file_name,
                              "version": version, "valid": valid, "note": note}
    return dest


@dataclass
class InstallPlanItem:
    """单个条目的安装计划项。"""
    pack_id: str
    name: str
    kind: str
    action: str                 # install|update|skip|manual|error
    detail: str = ""
    version: str | None = None
    url: str | None = None
    file_name: str | None = None
    asset: object = None
    ambiguous_assets: list = field(default_factory=list)


def plan_install(cfg, db: PacksDB, entries: list, *, results: dict | None = None,
                 progress_cb=None) -> list:
    """为若干条目生成安装/更新计划（只查询，不改动磁盘）。

    ``results`` 可预置 ``{pack_id: PackSourceResult}``（避免重复查询）。
    """
    plans: list = []
    for entry in entries:
        kind = entry.get("kind")
        name = entry.get("name_en") or entry["id"]
        if entry.get("local"):
            plans.append(InstallPlanItem(entry["id"], name, kind, "skip",
                                         detail="整合包自带，无需下载"))
            continue
        res = (results or {}).get(entry["id"])
        if res is None:
            if progress_cb:
                progress_cb(f"查询 {name} 的下载源...")
            res = list_assets(cfg, entry)
            if results is not None:
                results[entry["id"]] = res
        if res.status == "unsupported":
            plans.append(InstallPlanItem(entry["id"], name, kind, "manual",
                                         detail=res.note or "该站点需手动下载",
                                         url=res.url or (entry.get("urls") or {}).get("primary")))
            continue
        if not res.ok:
            plans.append(InstallPlanItem(entry["id"], name, kind, "error",
                                         detail=res.note or "没有可下载文件",
                                         url=res.url))
            continue
        chosen = pick_asset(res, entry)
        rec = db.installed(kind).get(entry["id"]) or {}
        local_ver = rec.get("version")
        if chosen is None:
            plans.append(InstallPlanItem(
                entry["id"], name, kind, "manual", url=res.url,
                detail=f"上游有 {len(res.assets)} 个候选文件，需手动选择",
                ambiguous_assets=res.assets))
            continue
        status = version_status(local_ver, chosen.version)
        target_ver = normalize_version(chosen.version) or chosen.version
        if rec.get("file_name"):
            # 已安装：只在确实能判定有更高版本时提示更新，绝不让"无法判定"
            # 变成自动重装（否则装完立刻又显示可更新，形成死循环）。
            if status == "same":
                plans.append(InstallPlanItem(entry["id"], name, kind, "skip",
                                             detail="已是最新", version=target_ver,
                                             url=chosen.url, file_name=chosen.file_name,
                                             asset=chosen))
            elif status == "older":
                plans.append(InstallPlanItem(entry["id"], name, kind, "skip",
                                             detail=f"本地版本更高（{local_ver}）",
                                             version=target_ver, url=chosen.url,
                                             file_name=chosen.file_name, asset=chosen))
            elif status == "newer":
                plans.append(InstallPlanItem(
                    entry["id"], name, kind, "update",
                    detail=f"{local_ver or '未记录'} → {target_ver or '未知'}",
                    version=target_ver, url=chosen.url,
                    file_name=chosen.file_name, asset=chosen))
            else:
                plans.append(InstallPlanItem(
                    entry["id"], name, kind, "skip",
                    detail="无法比较版本（本地或上游版本号未识别），如需重装请手动选择",
                    version=target_ver, url=chosen.url,
                    file_name=chosen.file_name, asset=chosen))
            continue
        detail = target_ver or chosen.tag or "未知版本"
        plans.append(InstallPlanItem(entry["id"], name, kind, "install", detail=detail,
                                     version=target_ver, url=chosen.url,
                                     file_name=chosen.file_name, asset=chosen))
    return plans


def apply_install(cfg, db: PacksDB, entry: dict, asset, *, progress_cb=None,
                  target_dir: Path | None = None) -> tuple[bool, str]:
    """执行一个条目的安装/更新，并写回安装记录。返回 (成功, 说明)。"""
    kind = entry.get("kind")
    rec = db.installed(kind).get(entry["id"]) or {}
    old_name = rec.get("file_name") or ""
    folder = (Path(target_dir) if target_dir else resolve_pack_dir(cfg, kind)) if kind in DIR_NAMES else None
    old = folder / old_name if folder and _safe_component(old_name) else None
    new_name = getattr(asset, "file_name", None)
    if not (old is not None and old.name != new_name and old.is_file() and not old.is_symlink()
            and old.suffix.lower() == ".zip" and old.resolve().parent == folder.resolve()):
        old = None
    try:
        dest = install_asset(cfg, entry, asset, progress_cb=progress_cb,
                             target_dir=target_dir, previous_file=old)
        # 只处理当前目标目录中该条目记录的旧 zip，保留其他文件及外部路径。
        if (old is not None and old != dest and old.is_file() and not old.is_symlink()
                and old.suffix.lower() == ".zip" and old.resolve().parent == dest.parent.resolve()):
            old.unlink()
    except (PacksError, net.HttpError, OSError) as e:
        return False, f"安装失败: {e}"
    info = entry.get("_last_install") or {}
    db.set_installed(kind, entry["id"], {
        "file_name": info.get("file_name") or dest.name,
        "path": str(dest),
        "version": info.get("version"),
        "installed_at": utils.now_str(),
        "valid": info.get("valid"),
        "note": info.get("note") or "",
        "source_url": getattr(asset, "url", ""),
    })
    db.add_alias(entry["id"], dest.stem if not dest.is_dir() else dest.name)
    note = info.get("note") or ""
    return True, (f"已安装到 {dest}" + (f"（提示: {note}）" if note else ""))


def reconcile_installed(cfg, db: PacksDB, *, kinds=None) -> dict:
    """用磁盘扫描结果校正安装记录。

    返回 ``{kind: {pack_id: PackFile}}``。磁盘上已不存在的记录被清除；
    未记录但能匹配到条目的文件会被补记为「已装（未记录版本）」，
    这样手动放进去的包也能参与更新检查。

    目录不存在（实例尚未创建、盘暂时不可用、目录刚被清空设置）时跳过该校正：
    扫不到文件≠没装，此时清记录会把 installed_at/source_url/绑定一并抹掉。
    """
    live: dict = {}
    for kind in (kinds or list(DIR_NAMES)):
        folder = resolve_pack_dir(cfg, kind)
        if folder is None or not folder.is_dir():
            live[kind] = {}
            continue
        # 列表刷新只关心文件名与匹配关系，不算文件夹体积（大包会明显卡）
        files = scan_pack_dir(folder, kind, with_size=False)
        entries = db.by_kind(kind)
        for f in files:
            f.pack_id, f.match_quality = match_pack(f, entries)
        side_live: dict = {}
        for f in files:
            if not f.pack_id:
                continue
            side_live[f.pack_id] = f
            rec = db.installed(kind).get(f.pack_id) or {}
            fields = {"file_name": f.file_name, "path": str(f.path)}
            if rec.get("file_name") != f.file_name or not rec.get("version"):
                fields["version"] = f.version
            for key in ("installed_at", "source_url", "valid", "note"):
                if key in rec:
                    fields[key] = rec[key]
            db.set_installed(kind, f.pack_id, fields, save=False)
        for pack_id in list(db.installed(kind)):
            if pack_id not in side_live:
                db.remove_installed(kind, pack_id, save=False)
        live[kind] = side_live
    db.save()
    return live


def installed_index(db: PacksDB) -> dict:
    """``{(kind, pack_id): record}``，便于列表标注。"""
    out = {}
    for kind in DIR_NAMES:
        for pack_id, rec in db.installed(kind).items():
            out[(kind, pack_id)] = rec
    return out


def folder_hint(cfg, kind: str) -> str:
    """给手动下载用户的落盘提示。"""
    folder = resolve_pack_dir(cfg, kind)
    if not folder:
        return f"未设置 {DIR_NAMES[kind]} 目录（请在设置中填写）"
    return str(folder)
