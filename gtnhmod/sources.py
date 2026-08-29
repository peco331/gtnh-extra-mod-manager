"""更新源抽象。

GitHubSource：查 releases/latest（ETag 条件缓存，304 不计数），404 时回退 tags 列表
  取最新 tag 再查该 tag 的 release；匿名限流 60次/时，配合缓存与可选 token。
LocalFolderSource：本地目录，最新版本=目录内可解析的最新 jar。
ManualSource：无上游，手动替换。CurseForgeSource：无 API key，仅返回页面链接供浏览器打开。
"""
import re
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from . import net, utils
from .versions import (MC_VERSION_RE, VersionParseError, _MC_SET,
                       max_version, order_key, parse_version, split_mc_mod_version)


class SourceError(Exception):
    pass


@dataclass
class DownloadCandidate:
    url: str
    file_name: str
    size: int | None = None


@dataclass
class UpdateInfo:
    latest_version: str | None
    candidates: list | None     # None = 无法自动下载
    release_body: str | None    # 更新日志
    checked_at: str
    note: str = ""
    published_at: str | None = None   # 最新版发布时间（下载页/发布页）


@dataclass
class VersionOption:
    """一个可选版本（安装/更新选择器用）。"""
    version: str
    tag: str = ""
    body: str | None = None
    published_at: str | None = None
    candidates: list | None = None   # None = 该版本无自动下载资产
    prerelease: bool = False         # GitHub prerelease（默认路径自动跳过）


def sort_version_options(options: list) -> list:
    """按版本降序排列（可解析的在前，不可解析的保持原顺序放最后）。"""
    parseable, unparse = [], []
    for o in options:
        try:
            parse_version(o.version)
            parseable.append(o)
        except VersionParseError:
            unparse.append(o)
    # order_key 是全序（变体构建 v1.85/Multi/Multiplayer 也严格分先后），
    # compare 的判等语义不可传递、不能用作排序比较器
    parseable.sort(key=lambda o: order_key(o.version), reverse=True)
    return parseable + unparse


class Source(ABC):
    source_type = "abstract"

    @abstractmethod
    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        """查询最新版本。force=True 时忽略新鲜度缓存。"""

    def list_versions(self, *, force: bool = False) -> list:
        """列出可选版本（最新在前）。默认实现退化为只有最新版。"""
        info = self.check(None, force=force)
        if not info.latest_version:
            return []
        return [VersionOption(info.latest_version, info.latest_version,
                              info.release_body, None, info.candidates)]

    @staticmethod
    def from_entry(entry: dict, cfg):
        """按条目 source_type 构造源。"""
        st = entry.get("source_type")
        src = entry.get("source") or {}
        if st == "github":
            return GitHubSource(
                owner=src.get("owner") or "", repo=src.get("repo") or "",
                asset_regex=src.get("asset_regex") or "",
                exclude_regex=src.get("exclude_regex") or "",
                tag_regex=src.get("tag_regex") or "",
                token=cfg.github_token, cache_dir=cfg.cache_dir,
                ttl_hours=cfg.check_interval_hours, proxy=cfg.proxy)
        if st == "local_folder":
            return LocalFolderSource(src.get("path") or "", src.get("name_regex") or "")
        if st == "curseforge":
            return CurseForgeSource((entry.get("urls") or {}).get("curseforge"))
        return ManualSource()


# ---------- GitHub ----------

def extract_version(tag: str) -> str | None:
    """从 tag 提取 mod 版本：首段是 MC 版本则去掉（1.7.10-0.8.0 → 0.8.0）。

    第二段若是已知 MC 版本（如 1.0.1-1.7.10-GTNH），说明首段是 mod 版本，不剥离。
    提取结果必须以数字或 v+数字开头——"p3" 这类纯补丁名的杂项 tag 不是
    可比较的版本，返回 None 让调用方跳过（否则会被解析成"版本3"排到最前）。
    """
    tag = (tag or "").strip()
    if not tag:
        return None
    parts = tag.split("-")
    ver = tag
    if len(parts) >= 2 and MC_VERSION_RE.match(parts[0]) and parts[1] not in _MC_SET:
        ver = "-".join(parts[1:])
    if not re.match(r"[vV]?\d", ver):
        return None
    return ver


def _score_asset(name: str, tag: str, tag_clean: str, repo: str) -> int:
    if name == f"{repo}-{tag}.jar":
        return 100
    if name == f"{repo}-{tag_clean}.jar":
        return 95
    if name in (f"{repo}-1.7.10-{tag}.jar", f"{repo}-1.7.10-{tag_clean}.jar"):
        return 90
    if tag and tag.lower() in name.lower():
        return 70
    if repo and repo.lower() in name.lower():
        return 60
    return 20


def pick_assets(assets: list, tag: str, repo: str,
                asset_regex: str = "", exclude_regex: str = "") -> list:
    """从 release assets 中挑 jar 候选（评分排序，下载失败可降级）。"""
    tag_clean = (tag or "").lstrip("v")
    cands = []
    for a in assets:
        name = a.get("name") or ""
        if not name.lower().endswith(".jar"):
            continue
        if exclude_regex and re.search(exclude_regex, name, re.I):
            continue
        if asset_regex and not re.search(asset_regex, name, re.I):
            continue
        score = _score_asset(name, tag, tag_clean, repo)
        cands.append((score, len(name), a))
    cands.sort(key=lambda x: (-x[0], x[1]))
    return [DownloadCandidate(a["browser_download_url"], a["name"], a.get("size"))
            for _, _, a in cands]


class GitHubSource(Source):
    source_type = "github"

    def __init__(self, owner: str, repo: str, *, asset_regex: str = "",
                 exclude_regex: str = "", tag_regex: str = "", token: str = "",
                 cache_dir: Path = None, ttl_hours: float = 6.0,
                 api_base: str = "https://api.github.com", proxy=None):
        self.owner, self.repo = owner, repo
        self.asset_regex, self.exclude_regex = asset_regex, exclude_regex
        self.tag_regex = tag_regex
        self.token = token
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl_hours = ttl_hours
        self.api_base = api_base.rstrip("/")
        self.proxy = proxy

    # ---- 内部 ----
    def _api(self, path: str, cache_key: str, *, force: bool = False):
        """请求 API 并做条件缓存。返回 (data, source)。404 缓存后抛出。"""
        url = f"{self.api_base}{path}"
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if not self.cache_dir:
            data = net.http_get(url, headers=headers, proxy=self.proxy, binary=True)
            import json
            return json.loads(data), "fresh"
        cache_file = self.cache_dir / f"github_{self.owner}_{self.repo}_{cache_key}.json"
        try:
            data, src = net.http_get_cached(url, cache_file=cache_file,
                                            ttl_hours=self.ttl_hours,
                                            headers=headers, proxy=self.proxy,
                                            force=force)
        except net.HttpError as e:
            if e.code == 404:
                # 缓存"不存在"状态，避免反复 404 请求
                utils.atomic_write_json(cache_file, {"not_found": True, "fetched_at": time.time()})
                raise
            raise
        if (isinstance(data, dict) and data.get("not_found")) or data is None:
            # data=None：not_found 标记缓存（http_get_cached 返回 cache["data"]）
            raise net.HttpError(404, f"{self.owner}/{self.repo} 无 {path}（缓存）")
        return data, src

    def _tag_ok(self, tag: str) -> bool:
        """tag_regex 过滤：仓库混装多版本（如其他MC版本）时只取匹配的版本。"""
        return (not self.tag_regex) or bool(re.search(self.tag_regex, tag or "", re.I))

    def _rate_note(self) -> str:
        r = net.rate_remaining
        if r is not None and r < 10 and not self.token:
            return f"GitHub API 匿名配额仅剩 {r} 次，建议在设置中配置 Token"
        return ""

    def _from_release(self, rel: dict, fallback_tag: str = "") -> UpdateInfo:
        tag = rel.get("tag_name") or fallback_tag
        version = extract_version(tag)
        assets = rel.get("assets") or []
        cands = pick_assets(assets, tag, self.repo, self.asset_regex, self.exclude_regex)
        note = self._rate_note()
        if not cands:
            note = "；".join(x for x in (note, "该Release无可用jar资产，需手动下载") if x)
        return UpdateInfo(version or None, cands or None, rel.get("body"),
                          utils.now_str(), note,
                          rel.get("published_at") or rel.get("created_at"))

    def _check_via_tags(self) -> UpdateInfo:
        tags_data, _ = self._api(f"/repos/{self.owner}/{self.repo}/tags", "tags")
        names = [t.get("name") for t in tags_data
                 if isinstance(t, dict) and t.get("name") and self._tag_ok(t.get("name"))]
        best = max_version(names)
        if not best:
            note = f"仓库 {self.owner}/{self.repo} 无可用版本标签"
            if self.tag_regex:
                note = (f"仓库 {self.owner}/{self.repo} 无匹配 {self.tag_regex!r} 的版本"
                        "（已过滤其他版本）")
            return UpdateInfo(None, None, None, utils.now_str(), note)
        try:
            rel, _ = self._api(f"/repos/{self.owner}/{self.repo}/releases/tags/{urllib.parse.quote(best)}",
                               f"rel_{best}")
        except net.HttpError as e:
            if e.code != 404:
                raise
            note = "；".join(x for x in
                             (f"最新tag {best} 无Release资产，需手动下载", self._rate_note()) if x)
            return UpdateInfo(extract_version(best), None, None, utils.now_str(), note)
        info = self._from_release(rel, best)
        return info

    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        if self.tag_regex:
            # 仓库混装多版本：releases/latest 可能是不匹配的版本（如其他MC版本），
            # 需按 tag 过滤后取最新
            return self._check_via_releases(force)
        try:
            rel, src = self._api(f"/repos/{self.owner}/{self.repo}/releases/latest", "latest",
                                 force=force)
        except net.HttpError as e:
            if e.code != 404:
                raise
            # releases/latest 404（如仓库只有 prerelease 版本）→ 查 release 列表兜底
            return self._check_via_releases(force)
        return self._from_release(rel)

    def _check_via_releases(self, force: bool = False) -> UpdateInfo:
        """查最近30个 release（优先非 prerelease，按 tag_regex 过滤），无匹配再退 tags 列表。"""
        try:
            releases, _ = self._api(f"/repos/{self.owner}/{self.repo}/releases?per_page=30",
                                    "releases", force=force)
        except net.HttpError as e:
            if e.code != 404:
                raise
            releases = []
        if isinstance(releases, list):
            rel = next((r for r in releases
                        if self._tag_ok(r.get("tag_name") or "") and not r.get("prerelease")),
                       None)
            if rel is None:
                rel = next((r for r in releases
                            if self._tag_ok(r.get("tag_name") or "")), None)
            if rel is not None:
                return self._from_release(rel)
        return self._check_via_tags()

    def list_versions(self, *, force: bool = False) -> list:
        """列出最近发布（最多30个），最新在前，含每个版本的资产候选。"""
        try:
            releases, _ = self._api(f"/repos/{self.owner}/{self.repo}/releases?per_page=30",
                                    "releases", force=force)
        except net.HttpError as e:
            if e.code != 404:
                raise
            info = self._check_via_tags()
            if not info.latest_version:
                return []
            return [VersionOption(info.latest_version, info.latest_version,
                                  info.release_body, None, info.candidates)]
        options = []
        for rel in releases if isinstance(releases, list) else []:
            tag = rel.get("tag_name") or ""
            if not self._tag_ok(tag):
                continue
            ver = extract_version(tag)
            if not ver:
                continue
            cands = pick_assets(rel.get("assets") or [], tag, self.repo,
                                self.asset_regex, self.exclude_regex)
            options.append(VersionOption(ver, tag, rel.get("body"),
                                         rel.get("published_at"), cands or None,
                                         bool(rel.get("prerelease"))))
        # 按版本去重（保留最新发布的那条）
        seen, uniq = set(), []
        for o in options:
            if o.version in seen:
                continue
            seen.add(o.version)
            uniq.append(o)
        if not uniq and self.tag_regex:
            # 最近30个 release 无匹配 → 查 tags 列表兜底（可能有更老但匹配的版本）
            info = self._check_via_tags()
            if not info.latest_version:
                return []
            return [VersionOption(info.latest_version, info.latest_version,
                                  info.release_body, None, info.candidates)]
        return sort_version_options(uniq)


# ---------- 本地目录 ----------

class LocalFolderSource(Source):
    source_type = "local_folder"

    def __init__(self, path: str, name_regex: str = ""):
        self.path = Path(path) if path else None
        self.name_regex = name_regex or ""

    def _scan_versions(self) -> dict:
        """扫描目录，返回 {版本: jar路径}。"""
        versions: dict = {}
        if not self.path or not self.path.is_dir():
            return versions
        try:
            files = sorted(self.path.iterdir())
        except OSError:
            return versions
        for p in files:
            if not p.is_file() or not p.name.lower().endswith(".jar"):
                continue
            if self.name_regex and not re.search(self.name_regex, p.name, re.I):
                continue
            name, mc, ver = split_mc_mod_version(p.name[:-4])
            if not ver:
                continue
            try:
                parse_version(ver)
            except VersionParseError:
                continue
            versions[ver] = p
        return versions

    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        versions = self._scan_versions()
        if not self.path or not self.path.is_dir():
            return UpdateInfo(None, None, None, utils.now_str(),
                              f"目录不存在: {self.path or '(未设置)'}")
        if not versions:
            return UpdateInfo(None, None, None, utils.now_str(),
                              f"目录 {self.path} 中未发现可识别的jar")
        best = max_version(list(versions))
        p = versions[best]
        cand = DownloadCandidate(str(p), p.name, p.stat().st_size)
        note = f"本地目录: {self.path}（共 {len(versions)} 个版本）"
        # 本地源"最新版发布时间"以最新 jar 文件时间为准
        try:
            published = utils.fmt_ts(p.stat().st_mtime) or None
        except OSError:
            published = None
        return UpdateInfo(best, [cand], None, utils.now_str(), note, published)

    def list_versions(self, *, force: bool = False) -> list:
        versions = self._scan_versions()
        options = [VersionOption(v, v, None, None,
                                 [DownloadCandidate(str(p), p.name, p.stat().st_size)])
                   for v, p in versions.items()]
        return sort_version_options(options)


# ---------- 手动 / CurseForge ----------

class ManualSource(Source):
    source_type = "manual"

    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        return UpdateInfo(None, None, None, utils.now_str(),
                          "手动维护的mod，无自动更新源；替换文件后重新扫描即可识别新版本")


class CurseForgeSource(Source):
    source_type = "curseforge"

    def __init__(self, url: str = ""):
        self.url = url or ""

    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        note = ("CurseForge 无 API key 无法自动下载；请在浏览器中手动下载后放入 mods 目录，"
                "工具重新扫描即可识别新版本")
        return UpdateInfo(None, None, None, utils.now_str(), note)
