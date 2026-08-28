"""Wiki 反爬 Cookie 导入（应对 Cloudflare 人机验证）。

gtnh.huijiwiki.com 开启 Cloudflare 验证后，程序直连一律被 403/验证页。
可行方案：用户在浏览器里通过一次验证，然后把该会话的 Cookie（核心是
cf_clearance，与浏览器的 User-Agent 和出口 IP 绑定）导入本工具随请求发送：

  浏览器 F12 → Network → 刷新 → 点第一个文档请求 → 右键 Copy →
  Copy as cURL → 粘贴给 parse_paste()，解析出 Cookie 与 User-Agent。

不做浏览器 cookie 库自动读取：Chrome/Edge 2024 起启用 App-Bound 加密，
第三方进程无法解密；本机也无 Firefox。
"""
import re

# 参数 token：'...'（bash）/ $'...'（bash转义形式，按原文处理）/ "..."（cmd）/ 裸token
_ARG_RE = re.compile(r"\$?'([^']*)'|\"([^\"]*)\"|(\S+)")

# 带值的 flag → 取值后的处理分支（键一律小写，查找时做 lower()）
_VAL_FLAGS = {
    "-h": "header", "--header": "header",
    "-a": "ua", "--user-agent": "ua",
    "-b": "cookie", "--cookie": "cookie",
}


def _join_continuations(text: str) -> str:
    """拼回续行：cmd 用行尾 ^，bash 用行尾 \\。"""
    text = re.sub(r"\^\s*\r?\n", " ", text)
    text = re.sub(r"\\\s*\r?\n", " ", text)
    return text


def parse_curl(text: str) -> tuple[str, str]:
    """Copy as cURL 文本 → (Cookie, User-Agent)，缺失的项为空串。"""
    cookie = ua = ""
    pending = None  # 等待取值的 flag（_VAL_FLAGS 的分支名）
    for m in _ARG_RE.finditer(_join_continuations(text or "")):
        arg = next(g for g in m.groups() if g is not None)
        if pending:
            if pending == "header":
                name, _, value = arg.partition(":")
                name = name.strip().lower()
                if name == "cookie" and not cookie:
                    cookie = value.strip()
                elif name == "user-agent" and not ua:
                    ua = value.strip()
            elif pending == "ua" and not ua:
                ua = arg.strip()
            elif pending == "cookie" and not cookie:
                cookie = arg.strip()
            pending = None
            continue
        pending = _VAL_FLAGS.get(arg.lower())
    return cookie, ua


def parse_paste(text: str) -> tuple[str, str]:
    """粘贴内容 → (Cookie, User-Agent)。

    优先按 Copy as cURL 解析；也接受直接粘贴的「Cookie: ...」/「User-Agent: ...」
    头行，或整段就是一个 cookie 串（如仅复制了 cf_clearance=... 所在行）。
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    if "-H" in text or "-b " in text or "curl" in text.lower():
        cookie, ua = parse_curl(text)
        if cookie or ua:
            return cookie, ua
    cookie = ua = ""
    for line in text.splitlines():
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        if name == "cookie" and not cookie:
            cookie = value.strip()
        elif name == "user-agent" and not ua:
            ua = value.strip()
    if not cookie:
        s = text.strip()
        if "=" in s and (";" in s or s.startswith("cf_")):
            cookie = s
    return cookie, ua
