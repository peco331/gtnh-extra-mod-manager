"""「资源包与光影」wiki 页面的抓取与解析。

数据源：gtnh.huijiwiki.com → 页面「资源包与光影」（见 PACK_PAGE）。
与「可添加MOD」（模板驱动）不同，本页是手写 wikitext，结构为：

  == 资源包 ==
    === 无障碍与实用性 / 深色模式 / 升级与重制 ===   每节一张 {| class="wikitable" 表
        列：图标 | 名字 | 作者/维护者 | 范围 | 说明 | 链接
    === 游戏自带资源包 ===                          纯项目符号列表（无下载链接）
  == 光影 ==
    === 光影加载器 / 安装方法 ===                    说明性文字，无条目
    === 推荐列表 ===                                 表格
        列：图标 | 名字 | 作者/维护者 | 适用加载器 | 说明 | 链接

解析产物为条目 dict 列表（见 _entry_from_row），字段与 mods_db 条目风格一致但
独立存放于 packs_db.json；自带资源包以 ``local=True`` 标记，不参与安装/更新。

表格列按表头名定位而非固定下标：光影表第 4 列是「适用加载器」而不是「范围」，
若按位置硬编码会把加载器列表当成说明。表头缺失时退回位置映射。
"""
import hashlib
import re

from . import net
from .wiki import WikiProfile, _inline_templates, github_repo_from_url

# ---- 页面与章节常量 ----
PACK_PAGE = "资源包与光影"
RESOURCE_SECTION = "资源包"
SHADER_SECTION = "光影"

KIND_RESOURCE = "resourcepack"
KIND_SHADER = "shader"

KIND_LABELS = {KIND_RESOURCE: "资源包", KIND_SHADER: "光影包"}

# 章节名 → 条目类别（资源包章节下按此细分）
RESOURCE_CATEGORY_MAP = {
    "无障碍与实用性": "无障碍与实用性",
    "深色模式": "深色模式",
    "升级与重制": "升级与重制",
    "游戏自带资源包": "游戏自带资源包",
}
SHADER_CATEGORY_MAP = {
    "推荐列表": "推荐列表",
}

# 只做说明、不含条目的子章节（出现时必须跳过，否则会把说明文字当条目）
NON_ENTRY_SECTIONS = frozenset({
    "摘要", "安装方法", "光影加载器", "提示", "兼容性", "注意事项",
})
BUILTIN_SECTION = "游戏自带资源包"

# 表头列名关键词 → 语义字段（按关键词匹配，容忍 colspan/style 等写法差异）
HEADER_FIELDS = (
    ("图标", "icon"),
    ("名字", "name"),
    ("名称", "name"),
    ("作者", "author"),
    ("维护者", "author"),
    ("范围", "scope"),
    ("适用加载器", "loader"),
    ("加载器", "loader"),
    ("说明", "desc"),
    ("简介", "desc"),
    ("链接", "links"),
)

# 版本门槛：{{Label|2.8.0以下}} → ("2.8.0", "below")
VERSION_LABEL_RE = re.compile(
    r"(?P<ver>\d+\.\d+(?:\.\d+)?)\s*(?P<rel>以下|以上|之前|之后|以前)?")
# 图标列 File 链接里的外链（link= 参数）
FILE_LINK_PARAM_RE = re.compile(r"\|\s*link\s*=\s*([^|\]]+)")
MODRINTH_RE = re.compile(r"modrinth\.com/(?:(shader|resourcepack|datapack|mod|project)/)?([^/\s#?]+)")
CURSEFORGE_RE = re.compile(r"curseforge\.com/([^\s#?]+)")

# 自动下载可用的域名（其余只能打开浏览器手动下载）
AUTO_SOURCE_HOSTS = ("github.com", "modrinth.com")


def _split_top_level(text: str, sep: str) -> list:
    """按 sep 切分，但忽略 [[...]]、[...]、{{...}} 内部的 sep。

    表格单元格里的 ``[[File:x.png|64px|link=https://…]]`` 含竖线，
    普通 split("|") 会把它切碎，导致列错位。
    """
    out, buf = [], []
    sq = cb = 0
    i = 0
    while i < len(text):
        two = text[i:i + 2]
        if two == "[[":
            sq += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]" and sq:
            sq -= 1
            buf.append(two)
            i += 2
            continue
        if two == "{{":
            cb += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}" and cb:
            cb -= 1
            buf.append(two)
            i += 2
            continue
        ch = text[i]
        if ch == "[":
            sq += 1
        elif ch == "]" and sq:
            sq -= 1
        if ch == sep and sq == 0 and cb == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


CELL_START_RE = re.compile(r"^[ \t]*\|(?P<mark>[-+}]*)[ \t]?(?P<rest>.*)$", re.S)


def split_table_cells(raw: str) -> list:
    """一个表格数据行的块 → 单元格列表。

    单元格以「行首的 |」起始；标记 ``|-``（行分隔）、``|}``（表结束）、
    ``|+``（表标题）不是单元格。正文里的 ``[[File:…|64px]]`` 竖线不在行首，
    因此天然不参与切分。

    以「行」为单位扫描：空单元格后面另起一行的内容仍属于该单元格
    （光影表大量写成 ``|\\n*Angelica\\n*Optifine``），不另起新格。
    """
    if not raw:
        return []
    cells: list = []
    for line in raw.split("\n"):
        m = CELL_START_RE.match(line)
        if m:
            if m.group("mark"):
                continue                # |- / |} / |+ 是标记行，不并入单元格
            cells.append(m.group("rest"))
        elif cells:
            cells[-1] += "\n" + line
    out = []
    for c in cells:
        c = "\n".join(ln.strip() for ln in c.strip().splitlines()).strip()
        out.append(c)
    return out


def _find_tables(body: str) -> list:
    """取出最外层 ``{| ... |}`` 表格块（不含首尾标记行）。

    嵌套表格（单元格里再放一张表）按深度配对，整段保留在外层表格里，
    不能被当成两张并列的表。
    """
    tables, depth, buf = [], 0, []
    for line in body.split("\n"):
        s = line.strip()
        if s.startswith("{|"):
            depth += 1
            if depth == 1:
                buf = []
                continue
        elif s.startswith("|}"):
            if depth > 0:
                depth -= 1
                if depth == 0:
                    tables.append("\n".join(buf))
                    buf = []
                else:
                    buf.append(line)
                continue
        if depth > 0:
            buf.append(line)
    return tables


def _header_cells(block: str) -> list:
    """表格的表头行 → 列名列表（``! style="…" | 名字`` 取最后一个竖线之后）。"""
    heads = []
    for line in block.split("\n"):
        s = line.strip()
        if not s.startswith("!"):
            continue
        for part in _split_top_level(s[1:], "!"):
            part = part.strip()
            if "|" in part:
                part = part.rsplit("|", 1)[-1]
            part = part.strip()
            if part:
                heads.append(part)
    return heads


def _header_field_map(heads: list) -> dict:
    """表头名列表 → {列下标: 语义字段}（未识别的列不映射）。"""
    out = {}
    for i, h in enumerate(heads):
        for kw, field in HEADER_FIELDS:
            if kw in h:
                out[i] = field
                break
    return out


def _data_rows(block: str) -> list:
    """表格块 → 数据行（按 ``|-`` 切分，跳过表头行）。

    单元格里嵌套的表格先整段取出占位，避免它自己的 ``|-`` 把外层行
    错误地切成两行（页面若出现横向显示异常的表格就是这类写法）。
    """
    nested: list = []

    def mask(m):
        nested.append(m.group(0))
        return f"\x00{len(nested) - 1}\x00"

    masked = re.sub(r"\{\|[^{}]*\|[^{}]*\|\}", mask, block)
    rows, cur = [], None
    for line in masked.split("\n"):
        s = line.strip()
        if s.startswith("|-"):
            if cur is not None:
                rows.append(cur)
            cur = []
            continue
        if cur is None:
            continue          # ``|-`` 之前的表头/说明不属于任何数据行
        cur.append(line)
    if cur is not None:
        rows.append(cur)
    out = []
    for r in rows:
        text = "\n".join(r)
        for i, original in enumerate(nested):
            text = text.replace(f"\x00{i}\x00", original)
        if text.strip():
            out.append(text)
    return out


# ---- 单元格文本清洗 ----
URL_RE = re.compile(r"\[(https?://[^\s\]]+)(?:\s+([^\]]*))?\]")
FILE_RE = re.compile(r"\[\[(?:File|Image|文件|图像):[^\]]*\]\]", re.I)
INTERWIKI_RE = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
REF_RE = re.compile(r"<ref\b[^>]*/\s*>|<ref\b[^>]*>.*?</ref\s*>", re.S)


def strip_cell_markup(s: str) -> str:
    """单元格 → 可读纯文本（保留换行语义）。

    与 wiki.strip_markup 的差别：先展开内联模板（保住 {{Label|2.8.0}} 的
    版本号与 {{item|弧光灯}} 的物品名），再逐项剥标记，并保留 ``<br>`` 换行。
    """
    if not s:
        return ""
    s = _inline_templates(s)
    s = FILE_RE.sub("", s)
    s = REF_RE.sub("", s)
    s = re.sub(r"\{\{.*?\}\}", "", s, flags=re.S)
    s = URL_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), s)
    s = INTERWIKI_RE.sub(lambda m: m.group(1), s)
    for tag, rep in (("<br />", "\n"), ("<br/>", "\n"), ("<br>", "\n"),
                     ("</li>", "\n"), ("</p>", "\n")):
        s = s.replace(tag, rep)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("'''", "").replace("''", "")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def pack_links(links_cell: str, icon_cell: str = "") -> dict:
    """链接列 → {links:[{url,label}], github, modrinth, curseforge, primary, direct}。

    ``direct`` 为可直接下载的链接（GitHub / Modrinth），供一键安装；其余
    （CurseForge 需官方 key、Discord 需登录、web.archive.org 等）只能打开浏览器。
    """
    found = []

    def add(url: str, label: str):
        url = url.strip().rstrip("）)。,;")
        if not url or url.startswith("#"):
            return
        if any(x["url"] == url for x in found):
            return
        found.append({"url": url, "label": label.strip() or url})

    for m in URL_RE.finditer(links_cell or ""):
        add(m.group(1), m.group(2) or "")
    # 图标列的 File 链接常带 link=<目标页>（如 Discord 帖子、GitHub 仓库）
    for m in FILE_LINK_PARAM_RE.finditer(icon_cell or ""):
        target = m.group(1).strip()
        if target.startswith("http"):
            add(target, "图标链接")

    info = {"links": found, "github": None, "modrinth": None,
            "curseforge": None, "primary": None, "direct": []}
    for item in found:
        url = item["url"]
        if info["github"] is None and github_repo_from_url(url):
            info["github"] = url
        mr = MODRINTH_RE.search(url)
        if info["modrinth"] is None and mr:
            info["modrinth"] = url
        cf = CURSEFORGE_RE.search(url)
        if info["curseforge"] is None and cf:
            info["curseforge"] = url
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
        if any(host == h or host.endswith("." + h) for h in AUTO_SOURCE_HOSTS):
            info["direct"].append(url)
    if found:
        info["primary"] = found[0]["url"]
    return info


def modrinth_ref(url: str):
    """Modrinth 链接 → (project_type, slug)；非 Modrinth 返回 None。"""
    m = MODRINTH_RE.search(url or "")
    if not m:
        return None
    return (m.group(1) or "project", m.group(2))


def extract_icon(icon_cell: str) -> str | None:
    """图标列 → File: 引用的图片文件名（不含扩展名）。"""
    m = re.search(r"\[\[(?:File|Image|文件|图像):([^|\]]+)", icon_cell or "", re.I)
    return m.group(1).strip() if m else None


def version_requirements(text: str) -> list:
    """从说明/范围文本提取 GTNH 版本门槛。

    ``2.8.0以下`` → [{"version": "2.8.0", "relation": "below"}]；
    ``2.9.0`` → [{"version": "2.9.0", "relation": "compatible"}]。
    """
    out = []
    for m in VERSION_LABEL_RE.finditer(text or ""):
        rel = {"以下": "below", "之前": "below", "以前": "below",
               "以上": "above", "之后": "above"}.get(m.group("rel"), "compatible")
        item = {"version": m.group("ver"), "relation": rel}
        if item not in out:
            out.append(item)
    return out


def pack_id(name_en: str, kind: str) -> str:
    """条目 id：``类别-英文名`` 归一化（光影与资源包同名时不会撞）。"""
    base = re.sub(r"[^a-z0-9]+", "-", (name_en or "").lower()).strip("-")
    if not base:
        base = "pack-" + hashlib.md5((name_en or "未知").encode("utf-8")).hexdigest()[:8]
    return f"{'shader' if kind == KIND_SHADER else 'pack'}-{base}"


def _row_fields(row: str, field_map: dict) -> dict:
    """数据行 → {语义字段: 清洗后文本}（未映射的列忽略）。"""
    cells = split_table_cells(row)
    out = {}
    for idx, field in field_map.items():
        if idx < len(cells) and field not in out:
            out[field] = cells[idx]
    return out


def _clean_list_cell(text: str) -> str:
    """去掉纯项目符号单元格的行首 ``*``/``#``（保留行首缩进的二级 ``**``）。"""
    lines = []
    for line in text.split("\n"):
        lines.append(re.sub(r"^\s*[*#]\s?", "", line) if line.strip() else "")
    return "\n".join(lines).strip()


def _entry_from_row(fields: dict, kind: str, category: str) -> dict | None:
    name_en = strip_cell_markup(fields.get("name") or "").replace("\n", " ")
    if not name_en:
        return None
    scope = strip_cell_markup(fields.get("scope") or "")
    desc = strip_cell_markup(fields.get("desc") or "")
    loader = _clean_list_cell(strip_cell_markup(fields.get("loader") or ""))
    # 页面上部分行少写一个空单元格，说明文字整体左移到「范围」列
    # （"提供功能性而不仅仅是美观效果的资源包"那行的作者就写成 5 格）。
    # 「说明」为空而「范围」有内容时按说明处理，避免简介整段丢失。
    if not desc and scope:
        desc, scope = scope, ""
    scope = _clean_list_cell(scope)
    detail = "\n".join(x for x in (scope, desc) if x)
    links = pack_links(fields.get("links") or "", fields.get("icon") or "")
    return {
        "id": pack_id(name_en, kind),
        "kind": kind,
        "name_en": name_en,
        "name_cn": "",
        "category": category,
        "author": strip_cell_markup(fields.get("author") or "").replace("\n", " "),
        "scope": scope,
        "loader": loader,
        "desc": desc,
        "detail": detail,
        "version_requirements": version_requirements(detail),
        "icon": extract_icon(fields.get("icon") or ""),
        "urls": links,
        "aliases": [],
        "local": False,          # True = 整合包自带，无下载源、不参与安装/更新
        "wiki_removed": False,
    }


def parse_builtin_packs(body: str) -> list:
    """「游戏自带资源包」小节 → 只读条目（随整合包提供，无需下载）。"""
    out = []
    for line in body.split("\n"):
        s = line.strip()
        if not s.startswith("*"):
            continue
        text = s.lstrip("*").strip()
        m = re.match(r"'''(.+?)'''\s*[：:]\s*(.*)$", text, re.S)
        if not m:
            continue
        name = strip_cell_markup(m.group(1)).replace("\n", " ")
        if not name:
            continue
        desc = strip_cell_markup(m.group(2))
        out.append({
            "id": pack_id(name, KIND_RESOURCE),
            "kind": KIND_RESOURCE,
            "name_en": name,
            "name_cn": "",
            "category": BUILTIN_SECTION,
            "author": "",
            "scope": "",
            "loader": "",
            "desc": desc,
            "detail": desc,
            "version_requirements": version_requirements(
                _inline_templates(m.group(2))),
            "icon": None,
            "urls": {"links": [], "github": None, "modrinth": None,
                     "curseforge": None, "primary": None, "direct": []},
            "aliases": [],
            "local": True,
            "wiki_removed": False,
        })
    return out


def _parse_section(body: str, kind: str, category_map: dict, out: list,
                   warnings: list) -> None:
    """解析一个二级章节下的全部三级子章节。"""
    from .wiki import _split_sections
    for sub_title, sub_body in _split_sections(body, level=3):
        title = sub_title.strip().strip("'").strip()
        if not title or title in NON_ENTRY_SECTIONS:
            continue
        if kind == KIND_RESOURCE and title == BUILTIN_SECTION:
            out.extend(parse_builtin_packs(sub_body))
            continue
        category = category_map.get(title, title)
        added = 0
        for block in _find_tables(sub_body):
            heads = _header_cells(block)
            field_map = _header_field_map(heads)
            if "name" not in field_map.values():
                continue        # 说明性表格（无「名字」列）不当条目
            for row in _data_rows(block):
                entry = _entry_from_row(_row_fields(row, field_map), kind, category)
                if entry:
                    out.append(entry)
                    added += 1
        if not added and _find_tables(sub_body):
            warnings.append(f"「{title}」中的表格未解析出条目（可能页面结构已变更）")


def parse_packs_wikitext(text: str):
    """「资源包与光影」wikitext → (条目列表, warnings)。"""
    from .wiki import _split_sections
    entries: list = []
    warnings: list = []
    for title, body in _split_sections(text, level=2):
        name = title.strip().strip("'").strip()
        if name == RESOURCE_SECTION:
            _parse_section(body, KIND_RESOURCE, RESOURCE_CATEGORY_MAP, entries, warnings)
        elif name == SHADER_SECTION:
            _parse_section(body, KIND_SHADER, SHADER_CATEGORY_MAP, entries, warnings)
    # id 去重（同名条目在不同小节出现时加序号）
    seen: dict = {}
    for e in entries:
        base = e["id"]
        if base in seen:
            seen[base] += 1
            e["id"] = f"{base}-{seen[base]}"
        else:
            seen[base] = 1
    if not entries:
        warnings.append("未从页面解析出任何资源包/光影条目（可能页面结构已变更）")
    return entries, warnings


def validate_packs_wikitext(text: str) -> None:
    """校验抓到的是「资源包与光影」页面正文，而不是验证页/错误页。"""
    if not text or not text.strip():
        raise net.HttpError(-3, "抓取到的页面内容为空")
    if "Just a moment" in text or "_cf_chl_opt" in text:
        raise net.HttpError(-3, "wiki 站点要求 Cloudflare 人机验证，暂时无法抓取")
    if RESOURCE_SECTION not in text or SHADER_SECTION not in text:
        raise net.HttpError(
            -3, f"返回内容不是「{PACK_PAGE}」页面 wikitext"
                "（缺少「资源包」/「光影」章节），可能被反爬拦截或页面已移动")


PACKS_CACHE_NAME = "packs_wikitext.txt"

PACKS_PROFILE = WikiProfile(page=PACK_PAGE, validate=validate_packs_wikitext,
                            cache_name=PACKS_CACHE_NAME)


def fetch_packs(cfg, *, interactive: bool = False, progress_cb=None):
    """抓取「资源包与光影」页面原文（复用与「可添加MOD」相同的反爬通道）。

    返回 ``WikiFetchResult``：text 为原文，channel/source 记录实际读取通道，
    便于界面展示可审计的网络证据。校验不通过不会写缓存、也不会返回旧缓存。
    """
    from .wiki import fetch_wiki
    return fetch_wiki(cfg, interactive=interactive, progress_cb=progress_cb,
                      profile=PACKS_PROFILE)


def packs_cache_file(cfg):
    """最近一次通过校验的本页原文缓存路径。"""
    return cfg.data_dir / "cache" / PACKS_CACHE_NAME


def write_packs_cache(cfg, text: str) -> None:
    """原子写入缓存；不可写不应让已成功的抓取变成失败。"""
    import os
    try:
        path = packs_cache_file(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_packs_cache(cfg) -> str | None:
    """读取上次成功抓取的原文（离线浏览用；不冒充本次在线结果）。"""
    try:
        text = packs_cache_file(cfg).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        validate_packs_wikitext(text)
    except net.HttpError:
        return None
    return text


def import_packs_text(cfg, text: str, *, source: str = "import"):
    """把已验证的原文转成解析结果（离线导入/缓存回放共用）。

    返回 (entries, warnings, text_hash)；不写数据库，由调用方决定是否合并。
    """
    validate_packs_wikitext(text)
    entries, warnings = parse_packs_wikitext(text)
    if source == "import":
        write_packs_cache(cfg, text)
    return entries, warnings, hashlib.sha256(text.encode("utf-8")).hexdigest()
