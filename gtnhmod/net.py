"""统一网络层：UA、超时、退避重试、代理、ETag 条件缓存、流式下载。

纯 urllib 实现（标准库）。GitHub API 匿名限流 60次/时，配合 http_get_cached
的条件请求（304 不计数）与新鲜度缓存使用；限流计数见 rate_remaining。
"""
import gzip
import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import utils

USER_AGENT = "GTNH-ModManager/1.0 (Windows; Python stdlib urllib)"

# 最近一次响应的 GitHub API 剩余配额（无则 None）
rate_remaining: int | None = None


class HttpError(Exception):
    """HTTP 请求错误。code=-1 为网络层错误，-2 为响应非 JSON。"""

    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}" if code > 0 else f"网络错误: {msg}")
        self.code = code


def _opener_for(proxy_cfg):
    if proxy_cfg and proxy_cfg.get("host"):
        host = proxy_cfg["host"]
        port = proxy_cfg.get("port", 8080)
        auth = ""
        if proxy_cfg.get("user"):
            auth = f'{proxy_cfg["user"]}:{proxy_cfg.get("pass", "")}@'
        url = f"http://{auth}{host}:{port}"
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": url, "https": url}))
    return None


def _note_rate_limit(headers: dict) -> None:
    global rate_remaining
    v = next((value for key, value in headers.items()
              if key.lower() == "x-ratelimit-remaining"), None)
    if v is not None:
        try:
            rate_remaining = int(v)
        except ValueError:
            pass


def _open_request(url: str, headers: dict, timeout: int, proxy_cfg):
    """发出 GET，返回 (原始bytes, 响应头dict)。"""
    req_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    req_headers.update(headers or {})
    req = urllib.request.Request(url, headers=req_headers)
    opener = _opener_for(proxy_cfg)
    if opener is None:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        hdrs = dict(resp.headers.items())
        _note_rate_limit(hdrs)
        return raw, hdrs


def http_get(url: str, *, headers=None, timeout=30, retries=2, binary=False, proxy=None):
    """GET 请求，返回 bytes 或 UTF-8 str。5xx/超时/连接错误退避重试；4xx 直接抛出。"""
    last = None
    for attempt in range(retries + 1):
        try:
            raw, _ = _open_request(url, headers, timeout, proxy)
            return raw if binary else raw.decode("utf-8")
        except urllib.error.HTTPError as e:
            last = HttpError(e.code, f"{url} -> {e.reason}")
            if e.code in (304, 400, 401, 403, 404, 422, 429):
                raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    if isinstance(last, HttpError):
        raise last
    raise HttpError(-1, f"{url} ({last})")


def http_get_cached(url: str, *, cache_file: Path, ttl_hours: float = 6.0,
                    headers=None, proxy=None):
    """带 ETag 条件缓存与新鲜度缓存的 GET，返回 (json数据, source)。

    source ∈ fresh（新拉取）/ cache（ttl内缓存）/ not_modified（304）
              / cache_stale（限流或出错时退回的过期缓存）。
    404 抛 HttpError(404)；403/429 时有缓存则退回缓存，无缓存抛出。
    """
    cache = utils.load_json(cache_file, None)
    now = time.time()
    if cache and now - cache.get("fetched_at", 0) < ttl_hours * 3600:
        return cache.get("data"), "cache"
    req_headers = dict(headers or {})
    if cache and cache.get("etag"):
        req_headers["If-None-Match"] = cache["etag"]
    try:
        raw, hdrs = _open_request(url, req_headers, 30, proxy)
    except urllib.error.HTTPError as e:
        if e.code == 304 and cache:
            cache["fetched_at"] = now
            utils.atomic_write_json(cache_file, cache)
            return cache.get("data"), "not_modified"
        if e.code in (403, 429) and cache:
            return cache.get("data"), "cache_stale"
        raise HttpError(e.code, f"{url} -> {e.reason}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HttpError(-2, f"响应不是 JSON: {url}")
    cache = {"etag": hdrs.get("ETag"), "fetched_at": now, "data": data}
    utils.atomic_write_json(cache_file, cache)
    return data, "fresh"


def download(url: str, dest: Path, *, timeout=60, proxy=None, progress_cb=None) -> None:
    """下载到 dest。progress_cb(done_bytes, total_bytes)。失败清理半成品。

    url 无 :// 时视为本地路径（local_folder 源），直接复制。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if "://" not in url:
        src = Path(url)
        shutil.copy2(src, dest)
        if progress_cb:
            total = src.stat().st_size
            progress_cb(total, total)
        return
    tmp = dest.with_name(dest.name + ".part")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        opener = _opener_for(proxy) or urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            total = 0
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except ValueError:
                pass
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
