"""在 Tk 页签内托管应用专用的 Edge/Chrome 窗口。

网页只负责展示；隔离脚本世界收集真实点击，主线程按本地条目 ID
调用原有管理流程。不会把 Python API、路径或凭据交给远端网页。
"""
import ctypes
import copy
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import wiki


def is_catalog_url(actual, expected):
    """允许目录锚点，不允许其他站点、编辑页或历史版本触发本地操作。"""
    try:
        a, b = urlsplit(actual), urlsplit(expected)
        query = parse_qs(a.query, keep_blank_values=True)
        return (a.scheme, a.netloc.lower(), unquote(a.path).replace('_', ' ')) == (
            b.scheme, b.netloc.lower(), unquote(b.path).replace('_', ' ')) and (
            not query or query == {'action': ['view']})
    except (TypeError, ValueError, AttributeError):
        return False


def installation_badges(state):
    """文件是否存在与启用/锁定分别呈现；未知版本不猜测。"""
    if not state:
        return [{'text': '未安装', 'kind': 'absent'}]
    side = {'both': '双端', 'client': '客户端', 'server': '服务端'}.get(state.get('install_side'), '')
    badges = [{'text': '已安装' + ('：' + side if side else ''), 'kind': 'installed'}]
    for side, label in (('client', '客户端'), ('server', '服务端')):
        current = (state.get('sides') or {}).get(side)
        if current is None:
            continue
        version = str(current.get('version') or '').strip()
        if version.lower() in ('未知', 'unknown'):
            version = ''
        text = label + '：' + ('v' + version.lstrip('vV') if version else '版本未知')
        kind = 'installed'
        if not current.get('enabled', True):
            text += '（已禁用）'
            kind = 'disabled'
        if current.get('locked'):
            text += '（已锁定）'
            kind = 'locked'
        badges.append({'text': text, 'kind': kind})
    return badges


def catalog_payload(entries, installed):
    """仅传入公开名称和 Wiki 链接；不序列化数据库或配置。"""
    result = []
    for entry in entries:
        urls = entry.get('urls') or {}
        links = [v for k, v in urls.items()
                 if k in ('github', 'curseforge', 'mcmod', 'preferred') and isinstance(v, str)]
        links += [v.get('url', '') for v in urls.get('links', []) if isinstance(v, dict)]
        state = installed.get(entry['id']) or {}
        result.append({'id': entry['id'], 'name': entry.get('name_en') or '',
                       'cn': entry.get('name_cn') or '',
                       'links': [u for u in links if u.startswith(('https://', 'http://'))],
                       'installed': state.get('install_side', ''),
                       'badges': installation_badges(state)})
    return result


# 此脚本只在 CDP 的具名隔离世界运行。站点脚本访问不到动作队列；
# 按钮闭包持有本地 ID，并拒绝 dispatchEvent()/element.click() 伪造的点击。
BRIDGE_SCRIPT = r"""
(payload => {
    if (window !== window.top) return {};
    const key = s => (s || '').normalize('NFKC').toLowerCase().replace(/\s+/g, '');
    const linkKey = s => {
        try {
            const u = new URL(s);
            const p = u.pathname.replace(/\/$/, '').replace(/\.git$/, '');
            return u.hostname.toLowerCase() + p.toLowerCase();
        } catch (_) { return ''; }
    };
    let state = window.__gtnhManager;
    if (!state) {
        state = window.__gtnhManager = {events: [], revision: '', blocks: new WeakMap()};
        document.addEventListener('click', ev => {
            if (!ev.isTrusted) return;
            const a = ev.target.closest && ev.target.closest('a[href]');
            if (!a) return;
            const u = new URL(a.href, location.href);
            if (!['http:', 'https:'].includes(u.protocol)) return;
            if (u.origin !== new URL(payload.page).origin) {
                ev.preventDefault();
                ev.stopImmediatePropagation();
                state.events.push({action: 'open', url: u.href});
            } else if (a.target === '_blank') {
                a.target = '_self';
            }
        }, true);
    }
    const allowed = payload.allowed;
    const rowInfo = new Map();
    let rows = allowed ? [...document.querySelectorAll('#mw-content-text .extra-mod')] : [];
    if (allowed && payload.layout === 'packs') {
        rows = [];
        let kind = '';
        for (const node of document.querySelectorAll('#mw-content-text h2, #mw-content-text table.wikitable')) {
            if (node.tagName === 'H2') {
                const title = (node.querySelector('.mw-headline') || node).textContent;
                kind = title.includes('资源包') ? 'resourcepack' : title.includes('光影') ? 'shader' : '';
                continue;
            }
            if (!kind) continue;
            const headers = [...node.querySelectorAll('tr:first-child th')].map(x => x.textContent);
            const column = (pattern, fallback) => {
                const i = headers.findIndex(x => pattern.test(x)); return i < 0 ? fallback : i;
            };
            for (const row of node.rows) {
                if (!row.querySelector(':scope > td') || row.cells.length < 3) continue;
                rows.push(row);
                rowInfo.set(row, {kind, name:row.cells[column(/名字|名称/, 1)],
                    body:row.cells[column(/说明|简介/, 4)] || row.cells[row.cells.length - 1],
                    links:row.cells[column(/链接/, 5)] || row.cells[row.cells.length - 1]});
            }
        }
    }
    const revision = JSON.stringify([payload.entries, payload.busy]);
    const changed = revision !== state.revision;
    state.revision = revision;
    if (!document.getElementById('gtnh-manager-style')) {
        const style = document.createElement('style');
        style.id = 'gtnh-manager-style';
        style.textContent = `
            .gtnh-manager-actions {display:flex;align-items:center;flex-wrap:wrap;gap:8px;
                margin-top:10px;padding-top:8px;border-top:1px solid #9aa0a6;
                font:13px/1.6 "Microsoft YaHei UI",sans-serif}
            .gtnh-manager-actions button {font:inherit;cursor:pointer;padding:3px 12px;
                border:1px solid #10679a;border-radius:3px;color:#10679a;background:#fff}
            .gtnh-manager-actions button:first-of-type {background:#10679a;color:#fff}
            .gtnh-manager-actions button:focus-visible {outline:3px solid #f0ad4e;outline-offset:2px}
            .gtnh-manager-actions button:disabled {opacity:.5;cursor:wait}
            .gtnh-manager-pack-actions {font-size:12px;gap:4px;margin-top:6px;padding-top:6px}
            .gtnh-manager-pack-actions button {padding:2px 6px}
            .gtnh-manager-badge {padding:2px 6px;white-space:nowrap}
            .gtnh-manager-installed {color:#237241;background:#eef7f0}
            .gtnh-manager-absent {color:#535c65;background:#edf0f3}
            .gtnh-manager-disabled {color:#835000;background:#fff0cc}
            .gtnh-manager-locked {color:#604395;background:#f1eafb}
            @media (prefers-color-scheme: dark) {
                .gtnh-manager-actions {border-top-color:#4a4d52}
                .gtnh-manager-actions button {border-color:#7ab4ff;color:#7ab4ff;background:#2b2d31}
                .gtnh-manager-actions button:first-of-type {background:#2f5f8f;color:#fff}
                .gtnh-manager-installed {color:#8fe0a6;background:#1f3d2a}
                .gtnh-manager-absent {color:#b8bec6;background:#33363b}
                .gtnh-manager-disabled {color:#f0b45a;background:#3d3420}
                .gtnh-manager-locked {color:#c8b6ff;background:#332a4d}
            }
        `;
        (document.head || document.documentElement).append(style);
    }
    let matched = 0;
    const icons = [];
    for (const row of rows) {
        const info = rowInfo.get(row);
        const name = info ? info.name : row.querySelector('.extra-mod-aside-name');
        const names = name ? [name.textContent, ...[...name.children].map(n => n.textContent),
            ...[...name.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent)].map(key) : [];
        const links = [...(info ? info.links : row).querySelectorAll(
            info ? 'a[href]' : '.extra-mod-links a[href]')].map(a => linkKey(a.href));
        // 优先按条目名；改名时只接受唯一的项目链接匹配，歧义不会变成安装按钮。
        const eligible = payload.entries.filter(e => !info || e.kind === info.kind);
        // 换行变体名先按整个名称匹配，避免 Complementary + Euphoria 命中其基础包。
        let candidates = eligible.filter(e => key(e.name) && name && key(name.textContent) === key(e.name));
        if (!candidates.length) candidates = eligible.filter(e => names.includes(key(e.name)) && key(e.name));
        if (!candidates.length) candidates = eligible.filter(e =>
            e.links.some(u => links.includes(linkKey(u)) && linkKey(u)));
        const entry = candidates.length === 1 ? candidates[0] : null;
        if (entry) {
            matched++;
            const image = row.querySelector(info ? 'td:first-child img' : '.extra-mod-aside-icon img');
            if (image) icons.push({id:entry.id, url:image.currentSrc || image.src});
        }
        const signature = revision + '\n' + (entry ? entry.id : '');
        if (!changed && state.blocks.get(row) === signature && row.querySelector('.gtnh-manager-actions')) continue;
        const old = row.querySelector('.gtnh-manager-actions');
        if (old) old.remove();
        const bar = document.createElement('div');
        bar.className = 'gtnh-manager-actions' + (info ? ' gtnh-manager-pack-actions' : '');
        for (const badge of entry ? entry.badges : [{text:'目录未匹配，安装状态待同步', kind:'absent'}]) {
            const label = document.createElement('span');
            label.className = 'gtnh-manager-badge gtnh-manager-' + badge.kind;
            label.textContent = badge.text;
            bar.append(label);
        }
        const button = (text, action) => {
            const b = document.createElement('button');
            b.type = 'button'; b.textContent = text; b.disabled = payload.busy;
            b.addEventListener('click', ev => {
                ev.stopPropagation();
                if (!ev.isTrusted || b.disabled || state.events.length >= 10) return;
                state.events.push({action, id: entry ? entry.id : ''});
            });
            bar.append(b);
        };
        if (!entry) {
            button('同步管理目录', 'sync');
        } else {
            if (entry.local) {
                // 整合包自带条目只提供说明，不生成下载安装动作。
            } else if (entry.installed) {
                button(info ? '管理' : '查看已安装', 'manage');
            } else button('安装', 'install');
            button(info ? '详情' : '管理详情', 'detail');
        }
        (info ? info.body : row.querySelector('.extra-mod-body') || row).append(bar);
        state.blocks.set(row, signature);
    }
    return {url: location.href, rows: rows.length, matched, icons,
            events: state.events.splice(0, 10)};
})
"""


def _cdp_browser_call(port, method):
    """在本次专用调试端口上执行一条浏览器级 CDP 命令。"""
    import urllib.request
    import websocket
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=2) as response:
        url = json.loads(response.read().decode('utf-8'))['webSocketDebuggerUrl']
    ws = websocket.create_connection(url, timeout=3, suppress_origin=True,
                                     http_no_proxy=['127.0.0.1', 'localhost'])
    try:
        ws.send(json.dumps({'id': 1, 'method': method}))
        if method == 'Browser.close':
            return None
        while True:
            message = json.loads(ws.recv())
            if message.get('id') == 1:
                return message.get('result', {})
    finally:
        ws.close()


def _cdp_browser_pid(port):
    """专用浏览器主进程 PID；启动器转交进程后仍能定位真正的窗口。"""
    try:
        info = _cdp_browser_call(port, 'SystemInfo.getProcessInfo')
    except Exception as exc:
        raise OSError(str(exc)) from exc
    return next((p.get('id') for p in info.get('processInfo', [])
                 if p.get('type') == 'browser'), None)


def _cdp_browser_close(port):
    try:
        _cdp_browser_call(port, 'Browser.close')
    except Exception:
        pass


def _terminate_pid(pid):
    """仅结束仍为 Edge/Chrome 的专用浏览器进程，防止 PID 复用误伤。"""
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = w.HANDLE
    handle = kernel.OpenProcess(0x1000 | 0x0001, False, pid)
    if not handle:
        return
    try:
        buffer, size = ctypes.create_unicode_buffer(1024), w.DWORD(1024)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return
        if os.path.basename(buffer.value).lower() in ('msedge.exe', 'chrome.exe'):
            kernel.TerminateProcess(handle, 1)
    finally:
        kernel.CloseHandle(handle)


def _sweep_stale_profiles(root):
    """清理已退出会话留下的配置目录；运行中的 Chromium 独占 lockfile，删不掉即跳过。"""
    import shutil
    for folder in root.glob('session-*'):
        lock = folder / 'lockfile'
        try:
            # 另一个页签刚建好目录、浏览器还没加锁时不能动它。
            if time.time() - folder.stat().st_mtime < 300:
                continue
            if lock.exists():
                lock.unlink()
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)


class NativeBrowserHost:
    """仅操作指定进程的浏览器窗口；所有 HWND 操作在 Tk 主线程执行。"""
    def __init__(self, parent):
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.parent, self.hwnd = parent, None
        signatures = {
            'SetParent': ([w.HWND, w.HWND], w.HWND),
            'GetParent': ([w.HWND], w.HWND),
            'GetWindowLongPtrW': ([w.HWND, ctypes.c_int], ctypes.c_ssize_t),
            'SetWindowLongPtrW': ([w.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
            'SetWindowPos': ([w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, w.UINT], w.BOOL),
            'IsWindow': ([w.HWND], w.BOOL),
            'EnableWindow': ([w.HWND, w.BOOL], w.BOOL),
            'GetWindowRect': ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            'GetClientRect': ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            'ClientToScreen': ([w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.api, name)
            fn.argtypes, fn.restype = args, result

    @staticmethod
    def find_window(pid):
        from ctypes import wintypes as w
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        api.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        found = []
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        @callback_type
        def visit(hwnd, _):
            process_id = w.DWORD()
            api.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value == pid:
                name = ctypes.create_unicode_buffer(256)
                api.GetClassNameW(hwnd, name, 256)
                if name.value == 'Chrome_WidgetWin_1':
                    found.append(hwnd)
            return True

        api.EnumWindows.argtypes = [callback_type, w.LPARAM]
        api.EnumWindows(visit, 0)
        return found[0] if found else None

    def attach(self, hwnd, width, height):
        self.hwnd = hwnd
        self._strip_frame()
        ctypes.set_last_error(0)
        self.api.SetParent(hwnd, self.parent)
        if ctypes.get_last_error() or self.api.GetParent(hwnd) != self.parent:
            raise OSError('浏览器窗口无法嵌入页签')
        self.resize(width, height)

    def _strip_frame(self):
        # SetParent 不会自动转换 WS_POPUP/WS_CHILD；Chromium 在重新挂载
        # 窗口时还可能恢复标题栏样式，调整尺寸时一并校正。
        style = self.api.GetWindowLongPtrW(self.hwnd, -16)
        child_style = (style & ~(0x80000000 | 0x00C00000 | 0x00040000 | 0x00080000
                                 | 0x00020000 | 0x00010000)) | 0x40000000
        if style != child_style:
            ctypes.set_last_error(0)
            self.api.SetWindowLongPtrW(self.hwnd, -16, child_style)
            if ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())

    def resize(self, width, height):
        if self.hwnd and self.api.IsWindow(self.hwnd):
            self._strip_frame()
            flags = 0x0004 | 0x0010 | 0x0020 | 0x0040
            if not self.api.SetWindowPos(self.hwnd, None, 0, 0, max(1, width),
                                         max(1, height), flags):
                raise ctypes.WinError(ctypes.get_last_error())
            # Chromium 的自绘边框不完全由 WS_CAPTION/WS_THICKFRAME 控制。
            # 让客户区恰好覆盖 Tk 容器，剩余的非客户区由父窗口裁掉。
            from ctypes import wintypes as w
            outer, client, origin = w.RECT(), w.RECT(), w.POINT()
            if (self.api.GetWindowRect(self.hwnd, ctypes.byref(outer))
                    and self.api.GetClientRect(self.hwnd, ctypes.byref(client))
                    and self.api.ClientToScreen(self.hwnd, ctypes.byref(origin))):
                left, top = origin.x - outer.left, origin.y - outer.top
                extra_w = outer.right - outer.left - client.right
                extra_h = outer.bottom - outer.top - client.bottom
                viewport = self.viewport_rect()
                if viewport:
                    left, top = viewport.left - outer.left, viewport.top - outer.top
                    extra_w = outer.right - outer.left - (viewport.right - viewport.left)
                    extra_h = outer.bottom - outer.top - (viewport.bottom - viewport.top)
                if (extra_w or extra_h) and all(0 <= v <= 96 for v in (left, top, extra_w, extra_h)):
                    if not self.api.SetWindowPos(self.hwnd, None, -left, -top,
                                                  max(1, width + extra_w), max(1, height + extra_h), flags):
                        raise ctypes.WinError(ctypes.get_last_error())

    def viewport_rect(self):
        """页面渲染区的位置，不含 Chromium 在客户区内自绘的标题栏。"""
        from ctypes import wintypes as w
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.api.EnumChildWindows.argtypes = [w.HWND, callback_type, w.LPARAM]
        self.api.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        rectangles = []

        @callback_type
        def visit(hwnd, _):
            name = ctypes.create_unicode_buffer(256)
            self.api.GetClassNameW(hwnd, name, 256)
            if name.value == 'Chrome_RenderWidgetHostHWND':
                rect = w.RECT()
                if self.api.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left:
                    rectangles.append(rect)
            return True

        self.api.EnumChildWindows(self.hwnd, visit, 0)
        return max(rectangles, key=lambda r: (r.right - r.left) * (r.bottom - r.top), default=None)

    def enable(self, enabled):
        if self.hwnd and self.api.IsWindow(self.hwnd):
            self.api.EnableWindow(self.hwnd, enabled)


class EmbeddedWikiBrowser:
    """一个专用浏览器进程与串行 CDP 工作线程，不接触用户的浏览器会话。"""
    bridge_layout = 'mods'

    def __init__(self, cfg, emit, *, profile=None):
        self.cfg, self.emit = cfg, emit
        self.proxy = copy.deepcopy(cfg.proxy)
        self.page_name = profile.page if profile else cfg.wiki_page
        self.page_url = wiki._referer(cfg, profile)
        self.ready = False
        self._stop = threading.Event()
        self._attached = threading.Event()
        self._closed = threading.Event()
        self._started = False
        self._commands = queue.Queue()
        self._snapshot = ([], False)
        self._ws = self._proc = self._host = None
        self._port = self._browser_pid = None
        self._dark = False
        self._request_id = 0
        self._context = self._loader = None

    def start(self, parent, width, height):
        if os.name != 'nt':
            raise RuntimeError('Wiki 内嵌网页需要 Windows；可使用离线目录')
        self._host = NativeBrowserHost(parent)
        self._started = True
        threading.Thread(target=self._worker, daemon=True, name='wiki-browser').start()

    def attach(self, hwnd, width, height):
        self._host.attach(hwnd, width, height)
        self._attached.set()

    def resize(self, width, height):
        if self._attached.is_set():
            self._host.resize(width, height)

    def enable(self, enabled):
        if self._host:
            self._host.enable(enabled)

    def set_color_scheme(self, dark):
        """让网页按暗色/浅色偏好渲染（Wiki 的「配色模式：系统」会跟随）。"""
        self._dark = bool(dark)
        self._commands.put(('color_scheme', None))

    def _emulate_color_scheme(self):
        self._call('Emulation.setEmulatedMedia', {'features': [
            {'name': 'prefers-color-scheme', 'value': 'dark' if self._dark else 'light'}]})
        # 灰机 Wiki 不跟随 prefers-color-scheme，改用 Chromium 的自动暗色渲染页面；
        # 旧版浏览器不支持时只保留上面的偏好设置。
        try:
            self._call('Emulation.setAutoDarkModeOverride', {'enabled': self._dark})
        except RuntimeError:
            pass

    def update(self, entries, installed, busy=False):
        self._snapshot = (catalog_payload(entries, installed), busy)

    def navigate(self, action):
        if action in ('home', 'reload', 'back'):
            self._commands.put((action, None))

    def fetch_wiki(self):
        future = Future()
        if self._stop.is_set() or not self.ready:
            raise RuntimeError(f'Wiki 网页尚未就绪，请返回{self.page_name}并等待正文加载')
        self._commands.put(('fetch', future))
        return future.result(timeout=30)

    def close(self):
        self.ready = False
        self._stop.set()
        if self._ws:
            try:
                self._ws.abort()  # 唤醒阻塞的 CDP 接收，让工作线程及时回收专用进程。
            except OSError:
                pass

    def wait_closed(self, timeout=5):
        return not self._started or self._closed.wait(timeout)

    def _call(self, method, params=None, timeout=5):
        self._request_id += 1
        rid = self._request_id
        self._ws.settimeout(timeout)
        self._ws.send(json.dumps({'id': rid, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(self._ws.recv())
            if message.get('id') != rid:
                continue
            if message.get('error'):
                raise RuntimeError(message['error'].get('message', '浏览器命令失败'))
            return message.get('result', {})
        raise TimeoutError('等待 Wiki 网页响应超时')

    def _world(self):
        frame = self._call('Page.getFrameTree')['frameTree']['frame']
        loader = (frame['id'], frame.get('loaderId'))
        if loader != self._loader:
            self._context = self._call('Page.createIsolatedWorld', {
                'frameId': frame['id'], 'worldName': 'gtnh-manager'})['executionContextId']
            self._loader = loader
        return frame

    def _evaluate(self, expression, *, timeout=5):
        result = self._call('Runtime.evaluate', {
            'expression': expression, 'contextId': self._context,
            'awaitPromise': True, 'returnByValue': True}, timeout=timeout)
        if result.get('exceptionDetails'):
            raise RuntimeError('Wiki 网页脚本未能执行')
        return result.get('result', {}).get('value')

    def _fetch(self):
        frame = self._world()
        if not is_catalog_url(frame.get('url'), self.page_url):
            raise RuntimeError(f'请先返回{self.page_name}页面，再同步管理目录')
        raw_url = self.cfg.wiki_url.rsplit('/api.php', 1)[0] + '/index.php?title=' + (
            quote(self.page_name)) + '&action=raw'
        raw_url = wiki._refresh_url(raw_url)
        value = self._evaluate("""(async () => {
            const r = await fetch(%s, {cache:'no-store',
                headers:{'Cache-Control':'no-cache'}, signal:AbortSignal.timeout(15000)});
            return {status:r.status, text: r.ok ? await r.text() : ''};
        })()""" % json.dumps(raw_url), timeout=20)
        if not isinstance(value, dict) or value.get('status') != 200:
            raise RuntimeError('Wiki 在线请求失败，已保留本地目录')
        return self._result_from_text(value.get('text', ''))

    def _result_from_text(self, text):
        result = wiki.wiki_result_from_text(text, source='online',
                                            channel='embedded_browser', request_count=1)
        if not result.mods:
            raise RuntimeError('Wiki 未解析到条目，已保留本地目录')
        wiki._write_wiki_cache(self.cfg, text)
        return result

    def _worker(self):
        profile = None
        try:
            import websocket
            browser = wiki._find_system_browser()
            if not browser:
                raise RuntimeError('未找到 Edge 或 Chrome；可使用离线目录')
            profile_root = self.cfg.data_dir / 'cache' / 'wiki_view'
            profile_root.mkdir(parents=True, exist_ok=True)
            _sweep_stale_profiles(profile_root)
            # 每个窗口独立目录，避免并开程序时转交到已有浏览器进程。
            profile = tempfile.TemporaryDirectory(dir=profile_root, prefix='session-',
                                                   ignore_cleanup_errors=True)
            port = wiki._get_free_port()
            cmd = [str(browser), f'--app={self.page_url}',
                   f'--user-data-dir={profile.name}', f'--remote-debugging-port={port}',
                   '--remote-debugging-address=127.0.0.1', '--no-first-run',
                   '--no-default-browser-check', '--disable-background-mode', '--disable-sync',
                   '--window-position=-32000,-32000', '--window-size=1100,720']
            if self.proxy and self.proxy.get('host'):
                cmd.append(f"--proxy-server=http://{self.proxy['host']}:{self.proxy.get('port', 8080)}")
            elif self.proxy is not None:
                cmd.append('--no-proxy-server')
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            self._port = port
            self._proc = subprocess.Popen(cmd, startupinfo=startup,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # 首次使用新配置目录时 Edge 可能较慢，或由启动器转交给新的浏览器进程；
            # 以本次专用调试端口报告的浏览器进程为准，不依赖启动器 PID。
            deadline = time.monotonic() + 45
            launcher_exit = None
            target = hwnd = None
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    targets = wiki._read_local_cdp_json(port)
                except OSError:
                    if self._proc.poll() is not None:
                        launcher_exit = launcher_exit or time.monotonic()
                        if time.monotonic() - launcher_exit > 15:
                            raise RuntimeError('Wiki 专用浏览器启动失败')
                    self._stop.wait(.15)
                    continue
                try:
                    target = wiki._select_wiki_cdp_target(targets, self.page_url)
                    if target and not self._browser_pid:
                        self._browser_pid = _cdp_browser_pid(port)
                    hwnd = NativeBrowserHost.find_window(self._browser_pid) if self._browser_pid else None
                    if target and hwnd:
                        break
                except OSError:
                    pass
                self._stop.wait(.15)
            if self._stop.is_set():
                return
            if not target or not hwnd:
                raise RuntimeError('Wiki 浏览器启动超时；可切换离线目录或重试')
            self._ws = websocket.create_connection(target['webSocketDebuggerUrl'],
                                                   timeout=5, suppress_origin=True,
                                                   http_no_proxy=['127.0.0.1', 'localhost'])
            self._call('Page.enable')
            self._call('Browser.setDownloadBehavior', {'behavior': 'deny'})
            self._emulate_color_scheme()
            self.emit('attach', hwnd)
            while not self._attached.wait(.1):
                if self._stop.is_set():
                    return
                if time.monotonic() > deadline + 10:
                    raise RuntimeError('Wiki 网页未能嵌入窗口')
            self._call('Page.navigate', {'url': self.page_url})
            last_status = last_icons = None
            while not self._stop.is_set():
                try:
                    action, future = self._commands.get_nowait()
                except queue.Empty:
                    action = future = None
                if action:
                    try:
                        if action == 'fetch':
                            future.set_result(self._fetch())
                        elif action == 'home':
                            self._call('Page.navigate', {'url': self.page_url})
                        elif action == 'color_scheme':
                            self._emulate_color_scheme()
                        elif action == 'reload':
                            self._call('Page.reload', {'ignoreCache': True})
                        elif action == 'back':
                            self._world()
                            self._evaluate('history.back()')
                    except Exception as exc:
                        if future:
                            future.set_exception(exc)
                        else:
                            raise
                try:
                    frame = self._world()
                    entries, busy = self._snapshot
                    payload = {'page': self.page_url,
                               'layout': self.bridge_layout,
                               'allowed': is_catalog_url(frame.get('url'), self.page_url),
                               'entries': entries, 'busy': busy}
                    state = self._evaluate(BRIDGE_SCRIPT + '(' + json.dumps(payload) + ')') or {}
                except RuntimeError as exc:
                    # 导航会销毁旧执行上下文，下轮获取新上下文。
                    if not any(note in str(exc).lower() for note in (
                            'context', 'frame', 'target navigated')):
                        raise
                    self._loader = None
                    self.ready = False
                    self._stop.wait(.3)
                    continue
                self.ready = bool(state.get('rows'))
                status = (state.get('url'), state.get('rows'), state.get('matched'))
                if status != last_status:
                    self.emit('page', state)
                    last_status = status
                icons = state.get('icons', [])
                if icons != last_icons and is_catalog_url(state.get('url'), self.page_url):
                    self.emit('icons', {'page': state['url'], 'icons': icons})
                    last_icons = icons
                for event in state.get('events', []):
                    self.emit('action', {'page': state.get('url'), **event})
                self._stop.wait(.3)
        except Exception as exc:
            if not self._stop.is_set():
                message = '缺少 websocket-client，请安装源码运行依赖' if isinstance(exc, ImportError) else str(exc)
                self.emit('error', message)
        finally:
            self.ready = False
            self._stop.set()
            while not self._commands.empty():
                _, pending = self._commands.get_nowait()
                if pending and not pending.done():
                    pending.set_exception(RuntimeError('Wiki 网页已关闭'))
            if self._ws:
                try:
                    self._ws.send(json.dumps({'id': 0, 'method': 'Browser.close'}))
                    self._ws.close()
                except Exception:
                    pass
            elif self._port:
                # 嵌入前失败时页面连接尚未建立，经浏览器级连接关闭，避免留下孤立窗口。
                _cdp_browser_close(self._port)
            try:
                if self._proc:
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.terminate()
                        try:
                            self._proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
                if self._browser_pid and self._browser_pid != getattr(self._proc, 'pid', None):
                    _terminate_pid(self._browser_pid)
                if profile:
                    # Chromium 子进程可能稍晚才释放 profile 文件句柄。
                    for _ in range(15):
                        try:
                            profile.cleanup()
                            break
                        except OSError:
                            time.sleep(.1)
            finally:
                self._closed.set()
