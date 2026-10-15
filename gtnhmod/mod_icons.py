"""MOD 列表缩略图：公开 Wiki 图片 / JAR logo，后台读取，Tk 主线程显示。"""
import hashlib
import io
import json
import os
import tempfile
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

from . import net
from .scanner import normalize_name

ICON_SIZE = 16
MAX_IMAGE_BYTES = 2 * 1024 * 1024


def allowed_icon_url(url, wiki_url):
    """只接受配置的 Wiki 或其公开图片 CDN，不把来源绑定当成图片地址。"""
    try:
        image, site = urlsplit(url), urlsplit(wiki_url)
        if image.username or image.password or not image.hostname:
            return False
        return ((image.scheme, image.netloc.lower()) == (site.scheme, site.netloc.lower())
                and image.scheme in ('http', 'https')) or (
                    image.scheme == 'https' and image.netloc.lower() in (
                        'huiji-public.huijistatic.com', 'huiji-thumb.huijistatic.com')
                    and image.path.startswith('/gtnh/uploads/'))
    except (TypeError, ValueError, AttributeError):
        return False


def thumbnail_png(data):
    """限大小解码，只保留首帧，等比缩放到透明的 16px 画布。"""
    from PIL import Image
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError('图标文件过大')
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 4_194_304 or max(image.size) > 4096:
            raise ValueError('图标尺寸过大')
        image = image.convert('RGBA')
        image.thumbnail((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (ICON_SIZE, ICON_SIZE))
        canvas.paste(image, ((ICON_SIZE - image.width) // 2,
                             (ICON_SIZE - image.height) // 2))
        output = io.BytesIO()
        canvas.save(output, format='PNG')
        return output.getvalue()


def jar_icon(path, names=()):
    """只读归档成员，不解压到游戏目录；多 MOD 的 JAR 先按名称匹配。"""
    try:
        path = Path(path)
        if path.is_dir():
            icon = path / 'pack.png'
            if (not icon.is_symlink() and icon.resolve().parent == path.resolve()
                    and icon.is_file() and icon.stat().st_size <= MAX_IMAGE_BYTES):
                return thumbnail_png(icon.read_bytes())
            return None
        with zipfile.ZipFile(path) as archive:
            records = []
            try:
                info = archive.getinfo('mcmod.info')
                if info.file_size <= 64 * 1024:
                    metadata = json.loads(archive.read(info).decode('utf-8-sig'))
                    records = metadata if isinstance(metadata, list) else (
                        metadata.get('modList', [metadata]) if isinstance(metadata, dict) else [])
            except (KeyError, ValueError, UnicodeError):
                pass
            records = [r for r in (records if isinstance(records, list) else []) if isinstance(r, dict)]
            matched = [r for r in records if any(normalize_name(str(r.get(k) or '')) in names
                                                for k in ('name', 'modid'))]
            selected = matched or (records if len(records) == 1 else [])
            candidates = [r.get('logoFile') for r in selected]
            candidates += ['logo.png', 'icon.png', 'pack.png']
            for name in candidates:
                if not isinstance(name, str) or not name:
                    continue
                member = PurePosixPath(name.replace('\\', '/').lstrip('/'))
                if '..' in member.parts:
                    continue
                try:
                    info = archive.getinfo(str(member))
                    if info.file_size > MAX_IMAGE_BYTES:
                        continue
                    return thumbnail_png(archive.read(info))
                except (KeyError, ValueError, OSError):
                    continue
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        pass
    return None


def download_icon(url, wiki_url, proxy):
    if not allowed_icon_url(url, wiki_url):
        return None

    class Redirects(urllib.request.HTTPRedirectHandler):
        handler_order = 100

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not allowed_icon_url(newurl, wiki_url):
                raise ValueError('图标跳转到非 Wiki 地址')
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = net._opener_for(proxy) or urllib.request.build_opener()
    opener.add_handler(Redirects())
    request = urllib.request.Request(url, headers={
        'User-Agent': net.USER_AGENT, 'Accept': 'image/*',
        'Referer': wiki_url.rsplit('/api.php', 1)[0] + '/'})
    try:
        with opener.open(request, timeout=6) as response:
            length = response.headers.get('Content-Length')
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ValueError('图标文件过大')
            return thumbnail_png(response.read(MAX_IMAGE_BYTES + 1))
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 429, 567):
            raise
        # 图片 CDN 与正文一样可能拒绝 urllib 的 TLS 指纹；复用现有可选通道。
        return _download_browser_tls(url, wiki_url, proxy)


def _download_browser_tls(url, wiki_url, proxy):
    from curl_cffi import requests
    proxies = None
    if proxy and proxy.get('host'):
        auth = (quote(proxy.get('user', ''), safe='') + ':'
                + quote(proxy.get('pass', ''), safe='') + '@') if proxy.get('user') else ''
        address = f"http://{auth}{proxy['host']}:{proxy.get('port', 8080)}"
        proxies = {'http': address, 'https': address}
    with requests.Session(trust_env=proxy is None, proxies=proxies) as session:
        for _ in range(5):
            if not allowed_icon_url(url, wiki_url):
                raise ValueError('图标跳转到非 Wiki 地址')
            response = session.get(url, impersonate='chrome', timeout=6, stream=True,
                                   allow_redirects=False, headers={
                                       'Referer': wiki_url.rsplit('/api.php', 1)[0] + '/',
                                       'Accept': 'image/*'})
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if response.status_code != 200:
                    raise ValueError('图标在线请求失败')
                length = response.headers.get('Content-Length')
                if length and int(length) > MAX_IMAGE_BYTES:
                    raise ValueError('图标文件过大')
                data = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_IMAGE_BYTES:
                        raise ValueError('图标文件过大')
                return thumbnail_png(bytes(data))
            finally:
                response.close()
    raise ValueError('图标跳转次数过多')


class ModIconCache:
    def __init__(self, master, cfg, emit):
        self.master, self.cfg, self.emit = master, cfg, emit
        self.images, self.pending, self.failed_until = {}, set(), {}
        self.closed = False
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='mod-icon')
        self.fallback = tk.PhotoImage(master=master, width=ICON_SIZE, height=ICON_SIZE)
        # 中性拼图占位符，不借用其他 MOD 的标识。
        self.fallback.put('#c5d2de', to=(2, 4, 14, 14))
        self.fallback.put('#557b9a', to=(3, 5, 13, 13))
        self.fallback.put('#557b9a', to=(6, 2, 10, 6))
        self.fallback.put('#c5d2de', to=(10, 8, 15, 11))

    def request(self, entry, jars=()):
        url = (entry or {}).get('icon_url') or ''
        if not allowed_icon_url(url, self.cfg.wiki_url):
            url = ''
        files = []
        for path in dict.fromkeys(Path(p) for p in jars):
            try:
                stat = path.stat()
                extra = None
                if path.is_dir():
                    try:
                        icon_stat = (path / 'pack.png').stat()
                        extra = (icon_stat.st_mtime_ns, icon_stat.st_size)
                    except OSError:
                        pass
                stamp = (str(path), stat.st_mtime_ns, stat.st_size)
                files.append(stamp + (extra,) if path.is_dir() else stamp)
            except OSError:
                continue
        names = tuple(filter(None, (normalize_name((entry or {}).get(k))
                                    for k in ('id', 'name_en', 'name_cn'))))
        if not url and not files:
            return '', self.fallback
        key = hashlib.sha256(json.dumps((url, files, names if files else ()),
                                        ensure_ascii=False).encode('utf-8')).hexdigest()
        if (not self.closed and key not in self.images and key not in self.pending
                and time.monotonic() >= self.failed_until.get(key, 0)):
            self.pending.add(key)
            self.pool.submit(self._load, key, url, tuple(p[0] for p in files), names,
                             self.cfg.wiki_url, self.cfg.proxy)
        return key, self.images.get(key, self.fallback)

    def _load(self, key, url, paths, names, wiki_url, proxy):
        data = None
        cache = self.cfg.data_dir / 'cache' / 'mod_icons' / (key + '.png')
        try:
            if cache.is_file() and cache.stat().st_size <= MAX_IMAGE_BYTES:
                try:
                    data = thumbnail_png(cache.read_bytes())
                except (ValueError, OSError):
                    pass
            if data is None:
                # Wiki 的通用占位图不覆盖 JAR 自带的真实图标。
                if url and 'unknown_pack.' not in url.lower():
                    try:
                        data = download_icon(url, wiki_url, proxy)
                    except Exception:
                        pass
                if data is None:
                    for path in paths:
                        data = jar_icon(path, names)
                        if data:
                            break
                if data is None and url and 'unknown_pack.' in url.lower():
                    data = download_icon(url, wiki_url, proxy)
                if data and not self.closed:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=cache.parent, suffix='.tmp', delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(data)
                    try:
                        os.replace(temporary, cache)
                    finally:
                        temporary.unlink(missing_ok=True)
        except Exception:
            pass  # 装饰性图片失败不影响安装、更新或已有缓存。
        if not self.closed:
            self.emit(key, data)

    def receive(self, key, data):
        """由 GUI 事件泵调用；所有 PhotoImage 都在 Tk 主线程创建。"""
        self.pending.discard(key)
        if self.closed:
            return
        if data:
            try:
                self.images[key] = tk.PhotoImage(master=self.master, data=data)
                return
            except tk.TclError:
                pass
        self.failed_until[key] = time.monotonic() + 60

    def close(self):
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        # 工作线程可能晚于 Tk 退出；图像引用须在主线程释放。
        self.images.clear()
        self.fallback = None
