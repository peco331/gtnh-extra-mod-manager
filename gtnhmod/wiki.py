"""wiki 抓取与 wikitext 解析。

数据源：gtnh.huijiwiki.com MediaWiki API（action=parse&format=json&prop=wikitext）。
页面结构：
  == 星门规则模组 ==
    === 功能增强 / 性能优化 / 视听增强 / 旧版本限定 ===
      每节若干 {{可添加MOD表格行 |参数=值 ...}}（详细介绍内含嵌套模板）
  == 非星门规则模组 ==（运行环境未标注 → 默认双端）
  == LiteLoader == / == 模组文件夹 ==（无条目，忽略）
解析产物为 mods_db 条目 dict 列表，与自定义源同构（见 db.py）。
"""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import net, utils

TEMPLATE_NAME = "可添加MOD表格行"
TEMPLATE_NAMES = ("可添加MOD表格行", "额外项目")
HEAD_TEMPLATE = TEMPLATE_NAME + "/头"
TAIL_TEMPLATE = TEMPLATE_NAME + "/尾"

STAR_GATE_SECTION = "星门规则模组"
NON_STAR_GATE_SECTION = "非星门规则模组"

# 章节名 → (分组, 分类)
SECTION_MAPPING = {
    "功能增强": ("星门规则", "功能增强"),
    "性能优化": ("星门规则", "性能优化"),
    "视听增强": ("星门规则", "视听增强"),
    "旧版本限定": ("星门规则", "旧版本限定"),
    "非星门规则模组": ("非星门规则", "非星门规则"),
}

HEADER_RE = re.compile(r"^(?<!=)(={1,6})(?!=)\s*(.+?)\s*(?<!=)\1(?!=)\s*$")
URL_RE = re.compile(r"\[(https?://[^\s\]]+)(?:\s+([^\]]*))?\]")
GITHUB_REPO_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)")
# 排除常见的源码/deobf 包，避免误选 asset
DEFAULT_EXCLUDE_REGEX = r"(sources|deobf|javadoc|api|[-.]dev(?:[-.]|$))"


@dataclass(frozen=True)
class WikiFetchResult:
    """一次 Wiki 读取的原始正文与可审计网络证据。"""
    text: str
    cache_note: str | None
    source: str
    channel: str
    response_bytes: int
    request_count: int
    text_hash: str


def _referer(cfg) -> str:
    base = cfg.wiki_url.rsplit("/api.php", 1)[0]
    return f"{base}/wiki/{urllib.parse.quote(cfg.wiki_page)}"


def _wiki_headers(cfg) -> dict:
    """请求头：Referer 必带；配置了反爬 Cookie 时带上（cf_clearance 与 UA/IP 绑定）。"""
    # 用户点击“刷新”时必须穿透中间缓存；Cookie/UA 仍只用于请求，绝不输出到日志。
    headers = {"Referer": _referer(cfg), "Cache-Control": "no-cache", "Pragma": "no-cache"}
    cookie = getattr(cfg, "wiki_cookie", "") or ""
    ua = getattr(cfg, "wiki_ua", "") or ""
    if cookie:
        headers["Cookie"] = cookie
    if ua:
        headers["User-Agent"] = ua
    return headers


def _refresh_url(url: str) -> str:
    """为主动刷新附加无语义的 cache-buster，避免 CDN/代理重放旧正文。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}gtnh_refresh={time.time_ns()}"


def _cffi_proxies(cfg):
    p = cfg.proxy
    if p and p.get("host"):
        auth = f'{p["user"]}:{p.get("pass", "")}@' if p.get("user") else ""
        url = f'http://{auth}{p["host"]}:{p.get("port", 8080)}'
        return {"http": url, "https": url}
    return None


def _fetch_impersonate_wikitext(cfg) -> str:
    """通道0（首选）：curl_cffi 模拟浏览器 TLS 指纹。

    Cloudflare 按客户端 TLS 指纹拦截（实测 urllib/curl 一律 403），模拟浏览
    器指纹可直接通过，无需人工过验证。curl_cffi 为可选依赖
    （py -m pip install --user curl_cffi），未安装时走后续通道。
    """
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        raise net.HttpError(-4, "未安装 curl_cffi")
    base = cfg.wiki_url.rsplit("/api.php", 1)[0]
    url = _refresh_url(f'{base}/index.php?title={urllib.parse.quote(cfg.wiki_page)}&action=raw')
    try:
        kw = {"impersonate": "chrome", "timeout": 30, "headers": _wiki_headers(cfg)}
        proxies = _cffi_proxies(cfg)
        if proxies:
            kw["proxies"] = proxies
        r = cffi_requests.get(url, **kw)
    except net.HttpError:
        raise
    except Exception as e:
        raise net.HttpError(-1, f"curl_cffi 请求失败: {e}")
    if r.status_code != 200:
        # Cloudflare 的验证页经常以 403 + Cf-Mitigated: challenge 返回。
        # 把它标记为“需要人工验证”，交互刷新即可立即切到浏览器流程，
        # 不再把验证页当普通限流连续重试。
        headers = getattr(r, "headers", {}) or {}
        body = getattr(r, "text", "") or ""
        cf_mitigated = str(headers.get("Cf-Mitigated", "")).lower()
        if ("challenge" in cf_mitigated
                or "just a moment" in body.lower()
                or "_cf_chl_opt" in body):
            raise net.HttpError(-3, f"{url} -> 需要 Cloudflare 人机验证")
        raise net.HttpError(r.status_code if r.status_code < 500 else -1,
                            f"{url} -> HTTP {r.status_code}")
    return r.text


def _fetch_api_wikitext(cfg) -> str:
    """通道1：MediaWiki API（action=parse，带 Referer，UA 必需否则 403）。"""
    url = _refresh_url(f'{cfg.wiki_url}?action=parse&page={urllib.parse.quote(cfg.wiki_page)}'
                       f'&format=json&prop=wikitext')
    raw = net.http_get(url, retries=0, proxy=cfg.proxy, headers=_wiki_headers(cfg))
    return json.loads(raw)["parse"]["wikitext"]["*"]


def _fetch_raw_wikitext(cfg) -> str:
    """通道2（备用）：action=raw 直接返回 wikitext，api.php 被临时限流时可用。"""
    base = cfg.wiki_url.rsplit("/api.php", 1)[0]
    url = _refresh_url(f'{base}/index.php?title={urllib.parse.quote(cfg.wiki_page)}&action=raw')
    return net.http_get(url, retries=0, proxy=cfg.proxy, headers=_wiki_headers(cfg))


def _fetch_curl_wikitext(cfg) -> str:
    """通道3（备用）：系统 curl.exe（Windows 10+ 自带）。

    该 wiki 的限流会针对 Python urllib 的 TLS 指纹（实测 curl 请求正常、
    urllib 403），curl 通道最稳。
    """
    import shutil
    import subprocess
    curl = shutil.which("curl")
    if not curl:
        raise net.HttpError(-1, "系统无 curl")
    base = cfg.wiki_url.rsplit("/api.php", 1)[0]
    url = _refresh_url(f'{base}/index.php?title={urllib.parse.quote(cfg.wiki_page)}&action=raw')
    ua = getattr(cfg, "wiki_ua", "") or net.USER_AGENT
    cmd = [curl, "-sS", "-f", "-m", "30", "-A", ua, "-e", _referer(cfg),
           "-H", "Cache-Control: no-cache", "-H", "Pragma: no-cache"]
    cookie = getattr(cfg, "wiki_cookie", "") or ""
    if cookie:
        cmd += ["-b", cookie]
    proxy = cfg.proxy
    if proxy and proxy.get("host"):
        auth = f'{proxy["user"]}:{proxy["pass"]}@' if proxy.get("user") else ""
        cmd += ["-x", f'http://{auth}{proxy["host"]}:{proxy.get("port", 8080)}']
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise net.HttpError(-1, f"curl 执行失败: {e}")
    if out.returncode != 0:
        raise net.HttpError(-1, f"curl 返回码 {out.returncode}")
    return out.stdout.decode("utf-8", "replace")


def _wiki_cache_file(cfg):
    return cfg.data_dir / "cache" / "wiki_wikitext.txt"


def _write_wiki_cache(cfg, text: str) -> None:
    """原子写入最近一次通过校验的 Wiki 原文。"""
    try:
        cache_file = _wiki_cache_file(cfg)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, cache_file)
    except OSError:
        # 缓存不可写不应让已经成功抓取的结果变成失败。
        pass


def _validate_wikitext(text: str) -> None:
    """校验抓到的确实是页面 wikitext，不是验证页/错误页。

    站点启用 Cloudflare 人机验证后，验证页以 HTTP 200 返回（curl 不加 -f 时
    退出码也是 0），会被各通道当成抓取成功——曾导致验证页覆盖本地缓存、
    解析结果为空、刷新时全部条目被误标记为 wiki 已删除。
    """
    if "Just a moment" in text or "_cf_chl_opt" in text:
        raise net.HttpError(-3, "wiki 站点要求 Cloudflare 人机验证，暂时无法抓取")
    if not any(t in text for t in TEMPLATE_NAMES):
        raise net.HttpError(-3, "返回内容不是页面 wikitext（缺少模板「可添加MOD表格行」或「额外项目」），"
                                "可能被反爬拦截或页面结构变更")


def fetch_wiki(cfg, *, interactive: bool = False, progress_cb=None) -> WikiFetchResult:
    """抓取页面 wikitext，并保留实际读取通道和网络响应证据。

    依次尝试 curl_cffi(浏览器TLS指纹) → api.php → curl+action=raw →
    urllib+action=raw；非交互调用在通道之间带退避。
    交互刷新检测到验证/拒绝时立即弹出隔离浏览器。全部失败则报错，
    绝不把本地缓存冒充本次刷新结果。
    内容先经 _validate_wikitext 校验，未通过不写缓存、继续换下一通道。
    """
    fetchers = (_fetch_impersonate_wikitext, _fetch_api_wikitext,
                _fetch_curl_wikitext, _fetch_raw_wikitext)
    errors = []
    code = None
    request_count = 0
    channels = ("curl_cffi", "mediawiki_api", "system_curl", "urllib_raw")
    for i, (channel, fn) in enumerate(zip(channels, fetchers)):
        request_count += 1
        if progress_cb:
            progress_cb(f"Wiki 网络请求 {request_count}：{channel}")
        try:
            text = fn(cfg)
            _validate_wikitext(text)
            _write_wiki_cache(cfg, text)
            response_bytes = len(text.encode("utf-8"))
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if progress_cb:
                progress_cb(
                    f"Wiki 网络响应：通道={channel}，{response_bytes} 字节，"
                    f"内容指纹={text_hash[:12]}")
            return WikiFetchResult(
                text=text,
                cache_note=None,
                source="online",
                channel=channel,
                response_bytes=response_bytes,
                request_count=request_count,
                text_hash=text_hash,
            )
        except net.HttpError as e:
            if e.code not in (403, 404, 429, -1, -3, -4):
                raise
            code = e.code
            errors.append(str(e))
            # 交互刷新遇到 Cloudflare/拒绝响应时，人工浏览器才是正确的
            # 验证边界。不要继续打剩余通道，也不要先返回旧缓存。
            if interactive and e.code in (403, 429, -3):
                break
        except (ValueError, KeyError) as e:
            errors.append(str(e))
        if (not interactive and i < len(fetchers) - 1 and code != -4):
            time.sleep(4 + i * 4)  # 4s、8s、12s 退避（该站限流对连续请求敏感）

    if interactive:
        try:
            def count_browser_request():
                nonlocal request_count
                request_count += 1

            text = fetch_wikitext_via_browser(
                cfg,
                progress_cb=progress_cb,
                request_counter=count_browser_request,
            )
            _validate_wikitext(text)
            _write_wiki_cache(cfg, text)
            response_bytes = len(text.encode("utf-8"))
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if progress_cb:
                progress_cb(
                    f"Wiki 网络响应：通道=browser，{response_bytes} 字节，"
                    f"内容指纹={text_hash[:12]}")
            return WikiFetchResult(
                text=text,
                cache_note=None,
                source="online",
                channel="browser",
                response_bytes=response_bytes,
                request_count=request_count,
                text_hash=text_hash,
            )
        except Exception as be:
            errors.append(f"浏览器验证失败: {be}")

    raise net.HttpError(403, f"wiki 抓取失败（可能被临时限流，请稍后重试）: {'; '.join(errors)}")


def fetch_wikitext(cfg, *, interactive: bool = False, progress_cb=None):
    """兼容旧调用，返回 ``(wikitext, 缓存说明|None)``。"""
    result = fetch_wiki(cfg, interactive=interactive, progress_cb=progress_cb)
    return result.text, result.cache_note


def _split_sections(text: str, level: int = 2):
    """按指定等级的标题切分，返回 [(标题, 正文)]。首元素标题为空串（标题前内容）。

    花括号深度 > 0（处于模板内部）时跳过标题识别——模板参数值里的
    行首 "==xxx==" 不应切断条目。
    """
    result = []
    cur_title, cur_lines = "", []
    depth = 0
    for line in text.split("\n"):
        depth += line.count("{{") - line.count("}}")
        m = HEADER_RE.match(line)
        if m and len(m.group(1)) == level and depth <= 0:
            result.append((cur_title, "\n".join(cur_lines)))
            cur_title, cur_lines = m.group(2).strip(), []
        else:
            cur_lines.append(line)
    result.append((cur_title, "\n".join(cur_lines)))
    return result


def _extract_blocks(text: str, template_name: str = TEMPLATE_NAME) -> list:
    """抓取所有 {{模板名 ...}} 块（花括号深度扫描，正确处理嵌套模板）。"""
    marker = "{{" + template_name
    blocks = []
    i = 0
    while True:
        idx = text.find(marker, i)
        if idx < 0:
            break
        depth, j = 0, idx
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1
                j += 2
                continue
            if text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
                continue
            j += 1
        blocks.append(text[idx + 2:j - 2 if depth == 0 else j])
        i = j
    return blocks


def _parse_params(inner: str, template_name: str = TEMPLATE_NAME) -> dict | None:
    """模板块内容 → 参数 dict；头/尾模板返回 None。"""
    inner = inner.strip()
    if any(inner in (t + "/头", t + "/尾") for t in TEMPLATE_NAMES):
        return None
    m = re.match(re.escape(template_name) + r"\s*(?P<body>.*)$", inner, re.S)
    if not m:
        return None
    params = {}
    cur_key, cur_val = None, []

    def flush():
        nonlocal cur_key, cur_val
        if cur_key is not None:
            params[cur_key] = "\n".join(cur_val).strip()
        cur_key, cur_val = None, []

    depth = 0
    for line in m.group("body").split("\n"):
        depth += line.count("{{") - line.count("}}")
        # 只在花括号深度 0 时识别参数行：嵌套模板自带的 |k=v 不泄漏为顶层参数
        m2 = re.match(r"\|\s*([^\s=|]+)\s*=(.*)$", line) if depth <= 0 else None
        if m2:
            flush()
            cur_key, cur_val = m2.group(1), [m2.group(2)]
        elif cur_key is not None:
            cur_val.append(line)  # 参数值跨行（详细介绍里的列表等）
    flush()
    return params


def parse_side(raw: str, default: str = "both"):
    """运行环境字段 → (side, uncertain)。未标注按默认（非星门规则默认双端）。"""
    s = raw.replace("<br>", "/").replace("<br />", "/").replace("<br/>", "/").replace("／", "/")
    uncertain = ("？" in s) or ("?" in s)
    s = s.replace("（？）", "").replace("(?)", "").replace("？", "").replace("?", "")
    low = s.lower()
    has_client = "客户端" in s or "client" in low
    has_server = "服务端" in s or "server" in low
    if has_client and has_server:
        side = "both"
    elif has_client:
        side = "client"
    elif has_server:
        side = "server"
    else:
        side = default
    return side, uncertain


RECOMMEND_HINTS = ("推荐", "特供", "首选", "必装")


def _is_preferred_label(label: str) -> bool:
    """链接标签是否暗示推荐（如"GTNH特供版""推荐使用该版"）。"""
    if not label:
        return False
    if "非官方" in label or "不推荐" in label:
        return False
    return any(k in label for k in RECOMMEND_HINTS)


def parse_urls(raw: str) -> dict:
    """相关地址 → 按域名分类的 urls dict + 完整链接列表（含标签，供绑定下载源选择）。

    urls["github"]/["curseforge"] 等为各域第一个链接（兼容默认源）；
    urls["links"] 保留全部：[{"url","label"}, ...]，按原文顺序；
    urls["preferred"] 为第一个带推荐/特供等标记的链接（默认下载源首选）。
    """
    urls = {"github": None, "curseforge": None, "mcmod": None, "bilibili": None,
            "other": [], "links": [], "preferred": None}
    for m in URL_RE.finditer(raw or ""):
        url = m.group(1).rstrip("/")
        label = (m.group(2) or "").strip() or url
        urls["links"].append({"url": url, "label": label})
        if urls["preferred"] is None and _is_preferred_label(label):
            urls["preferred"] = url
        for key, dom in (("github", "github.com"), ("curseforge", "curseforge.com"),
                         ("mcmod", "mcmod.cn"), ("bilibili", "bilibili.com")):
            if dom in url:
                if not urls[key]:
                    urls[key] = url
                break
        else:
            if url not in urls["other"]:
                urls["other"].append(url)
    return urls


def github_repo_from_url(url: str):
    """从 GitHub 链接提取 (owner, repo)；非仓库链接（releases/tags 等）返回 None。"""
    if not url:
        return None
    m = GITHUB_REPO_RE.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2).rstrip("/")
    if owner in ("releases", "tags", "issues", "pull", "actions", "marketplace"):
        return None
    return owner, repo


def _strip_templates(s: str) -> str:
    """按花括号深度剥掉所有 {{...}} 模板（正确处理嵌套）。"""
    out = []
    i = 0
    while i < len(s):
        if s.startswith("{{", i):
            depth, j = 1, i + 2
            while j < len(s) and depth:
                if s.startswith("{{", j):
                    depth += 1
                    j += 2
                    continue
                if s.startswith("}}", j):
                    depth -= 1
                    j += 2
                    continue
                j += 1
            i = j
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


# ---- GTNH 版本兼容表解析（尽力而为）----
# 规则类型：
#   range:    {{row|模组版本|GTNH最低|GTNH最高}} → mod版本匹配该模式时要求 gtnh ∈ [min,max]
#   gtnh_min: 对 GTNH ≥ X 的整合包，要求 mod ≥ Y（Y 可缺省）
#   gtnh_max: 对 GTNH ≤ X 的整合包，要求 mod ≤ Y（"Y是最后一个支持GTNH X的版本"）
GTNH_VER_RE = r"([\d][\w.]*(?:\s*beta\s*[\d.]+)?)"


def _clean_gtnh_ver(s: str) -> str | None:
    """从 'GTNH {{label|2.9.0 beta 1}}' 提取 '2.9.0 beta 1'；无数字/未知返回 None。"""
    s = (s or "").strip().strip("|").strip()
    if "未知" in s:
        return None
    m = re.search(r"[\d][\w.]*(?:\s*beta\s*[\d.]+)?", s)
    return m.group(0).strip() if m else None


def _inline_templates(s: str) -> str:
    """把 {{模板|参数...}} 展开为参数内容（去掉模板名），迭代直至无模板。

    这样 Accordion 里嵌套的 label 版本号等文字都会保留下来。
    """
    cur = s

    def once(text):
        out = []
        i = 0
        while i < len(text):
            if text.startswith("{{", i):
                depth, j = 1, i + 2
                while j < len(text) and depth:
                    if text.startswith("{{", j):
                        depth += 1
                        j += 2
                        continue
                    if text.startswith("}}", j):
                        depth -= 1
                        j += 2
                        continue
                    j += 1
                inner = text[i + 2:j - 2 if depth == 0 else j]
                out.append(inner.split("|", 1)[1] if "|" in inner else "")
                i = j
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    for _ in range(8):  # 最多8层嵌套
        nxt = once(cur)
        if nxt == cur:
            break
        cur = nxt
    return cur


def _extract_row_blocks(text: str) -> list:
    """抓取所有 {{row ...}} 块（含嵌套花括号）。"""
    blocks = []
    i = 0
    while True:
        idx = text.find("{{row", i)
        if idx < 0:
            break
        depth, j = 0, idx
        while j < len(text):
            if text.startswith("{{", j):
                depth += 1
                j += 2
                continue
            if text.startswith("}}", j):
                depth -= 1
                j += 2
                if depth == 0:
                    break
                continue
            j += 1
        blocks.append(text[idx + 2:j - 2 if depth == 0 else j])
        i = j
    return blocks


def _split_top_level(body: str, sep: str = "|") -> list:
    """按分隔符切分，但花括号内的分隔符不参与（处理嵌套模板）。"""
    parts, cur, depth = [], [], 0
    i = 0
    while i < len(body):
        if body.startswith("{{", i):
            depth += 1
            cur.append("{{")
            i += 2
            continue
        if body.startswith("}}", i):
            depth -= 1
            cur.append("}}")
            i += 2
            continue
        if body[i] == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(body[i])
        i += 1
    parts.append("".join(cur))
    return parts


def parse_compat(detail: str) -> list:
    """从详细介绍原文中提取 GTNH 版本兼容规则（尽力而为，原文保留展示）。"""
    rules = []
    if not detail:
        return rules
    # 1) 行式兼容表：{{row|模组版本|GTNH最低|GTNH最高}}
    for block in _extract_row_blocks(detail):
        if not block.startswith("row") or "|" not in block:
            continue
        row_body = block.split("|", 1)[1]
        parts = [p.strip() for p in _split_top_level(row_body)]
        if len(parts) < 3:
            continue
        mv = strip_markup(parts[0]).lstrip("v")
        if not re.search(r"\d", mv):
            continue
        mn = _clean_gtnh_ver(parts[1])
        mx = _clean_gtnh_ver(parts[2])
        if not mn and not mx:
            continue
        rules.append({"kind": "range", "mod_ver": mv, "min": mn, "max": mx,
                      "raw": "{{row|" + row_body + "}}"})
    # 之后基于"模板展开"转换的文本做正则（保留 label/Accordion 内的版本号）
    text = _inline_templates(detail)
    text = re.sub(r"\s+", " ", text)
    # 2) "GTNH X 版本：模组 Y 及以上版本"
    for m in re.finditer(r"GTNH\s*" + GTNH_VER_RE + r"[^。]*?模组\s*([\d][\w.]*)\s*及以上版本", text):
        rules.append({"kind": "gtnh_min", "gtnh": m.group(1), "mod_min": m.group(2),
                      "raw": m.group(0)})
    # 3) "Y 是最后一个支持/适配 GTNH X 的版本"
    for m in re.finditer(r"([\d][\w.]*)\s*是最后一个(?:支持|适配)\s*GTNH\s*" + GTNH_VER_RE + r"的版本", text):
        rules.append({"kind": "gtnh_max", "gtnh": m.group(2), "mod_max": m.group(1),
                      "raw": m.group(0)})
    # 4) "GTNH X ... 需要安装 ... Y 及以上"
    for m in re.finditer(r"GTNH\s*([\d][\w.]*)[^。]*?安装[^。]*?([\d][\w.]+)\s*及以上", text):
        rules.append({"kind": "gtnh_min", "gtnh": m.group(1), "mod_min": m.group(2),
                      "raw": m.group(0)})
    # 5) "适用于/支持 GTNH X 以上版本"（无 mod 版本约束）
    for m in re.finditer(r"(?<![不并])(?:适用于|支持|仅支持)\s*GTNH\s*" + GTNH_VER_RE + r"\s*以上(?:版本)?", text):
        rules.append({"kind": "gtnh_min", "gtnh": m.group(1), "mod_min": None,
                      "raw": m.group(0)})
    # 去重（按 raw）
    seen, uniq = set(), []
    for r in rules:
        if r["raw"] in seen:
            continue
        seen.add(r["raw"])
        uniq.append(r)
    return uniq


def strip_markup(s: str) -> str:
    """剥掉简述里的 wiki 标记，转成可读文本。"""
    if not s:
        return ""
    s = re.sub(r"\{\{.*?\}\}", "", s, flags=re.S)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[(https?://[^\s\]]+)\s+([^\]]+)\]", r"\2", s)
    s = s.replace("<br>", " ").replace("<br />", " ").replace("<br/>", " ")
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def make_id(name_en: str, name_cn: str) -> str:
    """条目 id：英文名小写去非字母数字；无英文名时用中文名哈希。"""
    base = re.sub(r"[^a-z0-9]+", "-", (name_en or "").lower()).strip("-")
    if not base:
        base = "mod-" + hashlib.md5((name_cn or "未知").encode("utf-8")).hexdigest()[:8]
    return base


def _strip_refs(s: str) -> str:
    """去掉名字里的 <ref>注释</ref> 与自闭合 <ref name="x"/> 标签。"""
    return re.sub(r"<ref\b[^>]*/\s*>|<ref\b[^>]*>.*?</ref\s*>", "", s or "",
                  flags=re.S).strip()


def _entry_from_params(params: dict, group: str, category: str) -> dict:
    name_en = _strip_refs(params.get("模组英文名") or "")
    name_cn = _strip_refs(params.get("模组中文名") or "")
    side, uncertain = parse_side(params.get("运行环境") or params.get("标签") or "", "both")
    urls = parse_urls(params.get("相关地址") or "")
    # 默认下载源：优先带"推荐/特供"等标记的链接，其次第一个github链接
    repo = github_repo_from_url(urls["preferred"]) or github_repo_from_url(urls["github"])
    if repo:
        source = {"owner": repo[0], "repo": repo[1], "asset_regex": "",
                  "exclude_regex": DEFAULT_EXCLUDE_REGEX}
        source_type = "github"
    elif "curseforge.com" in (urls["preferred"] or "") or urls["curseforge"]:
        source, source_type = {}, "curseforge"
    else:
        source, source_type = {}, "manual"
    return {
        "id": make_id(name_en, name_cn),
        "name_en": name_en,
        "name_cn": name_cn,
        "group": group,
        "category": category,
        "side": side,
        "side_uncertain": uncertain,
        "source_type": source_type,
        "source": source,
        "desc": strip_markup(params.get("简述") or ""),
        "detail": (params.get("详细介绍") or "").strip(),
        "compat": parse_compat(params.get("详细介绍") or ""),
        "urls": urls,
        "aliases": [],
        "wiki_removed": False,
    }


def _parse_entries(body: str, group: str, category: str, out: list) -> None:
    seen = {e["id"] for e in out}
    for tname in TEMPLATE_NAMES:
        for block in _extract_blocks(body, template_name=tname):
            params = _parse_params(block, template_name=tname)
            if not params:
                continue
            entry = _entry_from_params(params, group, category)
            if entry["id"] in seen:  # id 冲突时加序号
                n = 2
                while f'{entry["id"]}-{n}' in seen:
                    n += 1
                entry["id"] = f'{entry["id"]}-{n}'
            seen.add(entry["id"])
            out.append(entry)


def parse_wikitext(text: str):
    """解析 wikitext → (mods 条目列表, warnings)。"""
    mods: list = []
    warnings: list = []
    for title, body in _split_sections(text, level=2):
        if title == STAR_GATE_SECTION:
            for sub_title, sub_body in _split_sections(body, level=3):
                m = SECTION_MAPPING.get(sub_title)
                if m:
                    _parse_entries(sub_body, m[0], m[1], mods)
                elif sub_title:
                    n0 = len(mods)
                    _parse_entries(sub_body, "星门规则", sub_title, mods)
                    if len(mods) > n0:
                        warnings.append(f"未知子章节「{sub_title}」中发现 {len(mods) - n0} 个mod，已按同名分类收录")
        elif title == NON_STAR_GATE_SECTION:
            _parse_entries(body, "非星门规则", "非星门规则", mods)
        elif title:
            n0 = len(mods)
            _parse_entries(body, "非星门规则", "非星门规则", mods)
            if len(mods) > n0:
                warnings.append(f"未知章节「{title}」中发现 {len(mods) - n0} 个mod，已归入非星门规则")
    return mods, warnings


@dataclass(frozen=True)
class WikiSyncResult:
    """一次 Wiki 读取的可审计结果。

    ``source`` 仅可为 ``online``、``cache`` 或 ``import``。缓存结果可以
    给界面展示，但绝不能被当作一次成功同步而写回目录数据库。
    """
    mods: list
    warnings: list
    source: str
    attempted_at: str
    success_at: str | None
    text_hash: str
    channel: str = ""
    response_bytes: int = 0
    request_count: int = 0
    changes: list | None = None
    applied: bool = False


def wiki_result_from_text(text: str, *, source: str, attempted_at: str | None = None,
                          success_at: str | None = None, warnings=None,
                          channel: str | None = None,
                          response_bytes: int | None = None,
                          request_count: int = 0) -> WikiSyncResult:
    """把已验证的 Wiki 原文转成带来源标记的结果。

    离线导入调用者应传 ``source='import'``，不能伪造为在线刷新。
    """
    if source not in {"online", "cache", "import"}:
        raise ValueError(f"未知 Wiki 数据来源: {source}")
    _validate_wikitext(text)
    mods, parse_warnings = parse_wikitext(text)
    result_warnings = list(warnings or []) + parse_warnings
    attempted = attempted_at or utils.now_str()
    # 只有确实来自网络的验证通过内容才有 online success_at。
    online_success = success_at if source == "online" else None
    if source == "online" and online_success is None:
        online_success = attempted
    return WikiSyncResult(
        mods=mods,
        warnings=result_warnings,
        source=source,
        attempted_at=attempted,
        success_at=online_success,
        text_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        channel=channel or source,
        response_bytes=(len(text.encode("utf-8"))
                        if response_bytes is None else response_bytes),
        request_count=request_count,
    )


def sync_wiki(cfg, db, *, interactive: bool = False, progress_cb=None,
              apply_merge: bool = True) -> WikiSyncResult:
    """读取 Wiki；仅在线成功结果可合并到数据库。

    GUI 可传 ``apply_merge=False`` 后在主线程调用 :func:`apply_wiki_result`，
    避免后台线程与界面读取同一个 ModsDB。CLI 可保持默认的直接合并。
    """
    attempted_at = utils.now_str()
    fetched = fetch_wiki(cfg, interactive=interactive, progress_cb=progress_cb)
    result = wiki_result_from_text(
        fetched.text,
        source=fetched.source,
        attempted_at=attempted_at,
        success_at=utils.now_str() if fetched.source == "online" else None,
        warnings=[fetched.cache_note] if fetched.cache_note else [],
        channel=fetched.channel,
        response_bytes=fetched.response_bytes,
        request_count=fetched.request_count,
    )
    return apply_wiki_result(cfg, db, result) if apply_merge else result


def apply_wiki_result(cfg, db, result: WikiSyncResult) -> WikiSyncResult:
    """在调用者线程合并可信结果，拒绝缓存并保护异常缩减的目录。"""
    if result.applied:
        return result
    if result.source == "cache":
        return result
    if result.source not in {"online", "import"}:
        raise ValueError(f"无法合并 Wiki 来源: {result.source}")
    if not result.mods:
        raise ValueError("wiki 解析结果为空，已取消合并（本地数据未改动）")

    # 页面结构改变或抓到局部页面时，不能把大量旧条目标记为 removed。
    # 新条目和已有条目仍可更新，缺失条目暂时保留为在线状态。
    old_mods = db.wiki_mods() if hasattr(db, "wiki_mods") else []
    preserve_missing = len(old_mods) >= 4 and len(result.mods) * 2 < len(old_mods)
    warnings = list(result.warnings)
    if preserve_missing:
        warnings.append(
            f"Wiki 仅解析到 {len(result.mods)} 个条目，现有目录有 {len(old_mods)} 个；"
            "本次更新已收录内容，但未把缺失条目标记为删除")

    merge_kwargs = {"update_fetched_at": result.source == "online"}
    if preserve_missing:
        merge_kwargs["preserve_missing"] = True
    changes = db.merge_wiki(result.mods, **merge_kwargs)
    return replace(result, warnings=warnings, changes=list(changes), applied=True)


def import_wiki_text(cfg, db, text: str) -> WikiSyncResult:
    """导入离线 Wiki 原文；明确标记为 import，不冒充在线刷新。"""
    result = wiki_result_from_text(text, source="import")
    applied = apply_wiki_result(cfg, db, result)
    _write_wiki_cache(cfg, text)
    return applied


def fetch_and_parse(cfg, *, interactive: bool = False, progress_cb=None):
    """兼容旧调用：抓取并解析，返回 ``(mods, warnings)``，不写数据库。"""
    fetched = fetch_wiki(cfg, interactive=interactive, progress_cb=progress_cb)
    result = wiki_result_from_text(
        fetched.text,
        source=fetched.source,
        warnings=[fetched.cache_note] if fetched.cache_note else [],
        channel=fetched.channel,
        response_bytes=fetched.response_bytes,
        request_count=fetched.request_count,
    )
    return result.mods, result.warnings


def _read_local_cdp_json(port: int) -> list:
    """读取本机 CDP target 列表；显式绕过系统代理，避免 localhost 被转发。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json", timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def _select_wiki_cdp_target(tabs: list, page_url: str) -> dict | None:
    """只选择目标 Wiki 页面，避免误连 Edge 内部页或用户打开的其他页面。"""
    wanted = urllib.parse.urlsplit(page_url)
    wanted_path = urllib.parse.unquote(wanted.path).replace("_", " ").rstrip("/")

    def matches(tab: dict) -> bool:
        if tab.get("type") != "page" or not tab.get("webSocketDebuggerUrl"):
            return False
        current = urllib.parse.urlsplit(tab.get("url", ""))
        current_path = urllib.parse.unquote(current.path).replace("_", " ").rstrip("/")
        return (current.hostname or "").lower() == (wanted.hostname or "").lower() \
            and current_path == wanted_path

    return next((tab for tab in tabs if matches(tab)), None)


def _find_system_browser() -> Path | None:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _get_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cdp_eval(ws, expression: str, timeout_seconds: float = 5.0):
    """执行一条 CDP Runtime.evaluate，并跳过先到达的异步事件。"""
    request_id = time.time_ns() % 2_000_000_000
    ws.send(json.dumps({
        "id": request_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        },
    }))
    deadline = time.time() + timeout_seconds
    try:
        ws.settimeout(timeout_seconds)
    except (AttributeError, OSError):
        pass
    while time.time() < deadline:
        message = json.loads(ws.recv())
        if message.get("id") != request_id:
            continue
        result = message.get("result", {}).get("result", {})
        if result.get("subtype") == "error" or message.get("error"):
            return {}
        return result.get("value") or {}
    raise TimeoutError("等待浏览器脚本响应超时")


def fetch_wikitext_via_browser(cfg, timeout_seconds: int = 120, progress_cb=None,
                               request_counter=None) -> str:
    """打开系统浏览器窗口供用户完成一次人机验证，并自动读取验证通过后的 wikitext。"""
    import subprocess
    try:
        import websocket
    except ImportError as e:
        raise net.HttpError(-1, f"缺少 websocket 依赖，无法连接浏览器调试会话: {e}")

    browser = _find_system_browser()
    if not browser:
        raise net.HttpError(-1, "未找到系统 Edge 或 Chrome 浏览器，请手动导入离线 Wiki 文本")

    user_data = cfg.data_dir / "cache" / "browser_profile"
    user_data.mkdir(parents=True, exist_ok=True)
    port = _get_free_port()

    # 直接打开带验证的 wiki 页面
    page_url = cfg.wiki_url.rsplit("/api.php", 1)[0] + f"/wiki/{urllib.parse.quote(cfg.wiki_page)}"
    cmd = [
        str(browser),
        f"--user-data-dir={user_data.resolve()}",
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        page_url,
    ]
    if cfg.proxy and cfg.proxy.get("host"):
        p = cfg.proxy
        cmd.append(f"--proxy-server=http://{p['host']}:{p.get('port', 8080)}")

    if progress_cb:
        progress_cb("已弹出网页窗口，请在浏览器中完成人机验证...")

    proc = subprocess.Popen(cmd)
    ws = None
    try:
        # 等待 CDP 调试端口就绪
        ws_url = None
        t0 = time.time()
        while time.time() - t0 < 15:
            time.sleep(0.5)
            try:
                tabs = _read_local_cdp_json(port)
                target = _select_wiki_cdp_target(tabs, page_url)
                if target and target.get("webSocketDebuggerUrl"):
                    ws_url = target["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
        if not ws_url:
            raise net.HttpError(-1, "未能连接到浏览器调试会话")

        ws = websocket.create_connection(ws_url, timeout=5)
        base_url = cfg.wiki_url.rsplit("/api.php", 1)[0]
        raw_url = f"{base_url}/index.php?title={urllib.parse.quote(cfg.wiki_page)}&action=raw"
        js = f"""
        (async () => {{
            try {{
                const resp = await fetch({json.dumps(raw_url)}, {{cache: "no-store"}});
                if (resp.status === 200) {{
                    const text = await resp.text();
                    return {{status: 200, text: text}};
                }}
                return {{status: resp.status}};
            }} catch (e) {{
                return {{error: e.toString()}};
            }}
        }})()
        """

        poll_start = time.time()
        while time.time() - poll_start < timeout_seconds:
            time.sleep(1.5)
            # 检查浏览器窗口是否已被用户主动关闭
            if proc.poll() is not None:
                raise net.HttpError(-1, "浏览器验证窗口已关闭")
            try:
                if request_counter:
                    request_counter()
                val = _cdp_eval(ws, js)
                if val.get("status") == 200:
                    text = val.get("text", "")
                    if any(t in text for t in TEMPLATE_NAMES):
                        _validate_wikitext(text)
                        _write_wiki_cache(cfg, text)
                        return text
            except (websocket.WebSocketTimeoutException, TimeoutError):
                # 页面仍在验证/加载时，下一轮继续询问。
                continue
            except websocket.WebSocketException as e:
                raise net.HttpError(-1, f"浏览器调试会话断开: {e}")
            except OSError:
                pass
        raise net.HttpError(-1, f"等待浏览器人机验证超时（{timeout_seconds}秒）")
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)
        except Exception:
            pass
