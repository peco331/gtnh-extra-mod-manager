"""资源包与光影 Wiki 桥接：复用专用浏览器，安装动作仍由本地包管理器处理。"""
import hashlib

from . import packs_wiki, wiki
from .wiki_browser import EmbeddedWikiBrowser


def installation_badges(entry, record):
    if entry.get('local'):
        return [{'text': '整合包自带，请在游戏内启用', 'kind': 'installed'}]
    if not record or not record.get('file_name'):
        return [{'text': '未安装', 'kind': 'absent'}]
    result = [{'text': '已安装', 'kind': 'installed'},
              {'text': '版本：' + str(record.get('version')) if record.get('version') else '版本未知',
               'kind': 'installed'}]
    if not record.get('enabled', True):
        result.append({'text': '文件已禁用', 'kind': 'disabled'})
    if record.get('locked'):
        result.append({'text': '已锁定更新', 'kind': 'locked'})
    if record.get('duplicates'):
        result.append({'text': '多个本地副本，需核对', 'kind': 'disabled'})
    if record.get('directory_unavailable'):
        result.append({'text': '目录暂不可用，保留安装记录', 'kind': 'disabled'})
    return result


def catalog_payload(entries, installed):
    result = []
    for entry in entries:
        if entry.get('custom') or entry.get('wiki_removed'):
            continue
        urls = entry.get('urls') or {}
        links = [value for key, value in urls.items()
                 if key in ('github', 'modrinth', 'curseforge', 'primary') and isinstance(value, str)]
        links += [value.get('url', '') for value in urls.get('links', []) if isinstance(value, dict)]
        rec = installed.get((entry.get('kind'), entry['id'])) or {}
        result.append({'id': entry['id'], 'name': entry.get('name_en') or '',
                       'cn': entry.get('name_cn') or '', 'kind': entry.get('kind'),
                       'links': [u for u in links if u.startswith(('http://', 'https://'))],
                       'local': bool(entry.get('local')), 'installed': bool(rec.get('file_name')),
                       'badges': installation_badges(entry, rec)})
    return result


class EmbeddedPacksBrowser(EmbeddedWikiBrowser):
    bridge_layout = 'packs'

    def __init__(self, cfg, emit):
        super().__init__(cfg, emit, profile=packs_wiki.PACKS_PROFILE)

    def update(self, entries, installed, busy=False):
        self._snapshot = (catalog_payload(entries, installed), busy)

    def _result_from_text(self, text):
        packs_wiki.validate_packs_wikitext(text)
        entries, _ = packs_wiki.parse_packs_wikitext(text)
        if not entries:
            raise ValueError('资源包与光影正文未解析出条目，已保留本地目录')
        packs_wiki.write_packs_cache(self.cfg, text)
        return wiki.WikiFetchResult(text=text, cache_note=None, source='online',
                                   channel='embedded_browser', response_bytes=len(text.encode('utf-8')),
                                   request_count=1, text_hash=hashlib.sha256(text.encode('utf-8')).hexdigest())
