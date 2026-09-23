"""更新源抽象。

GitHubSource：检查与安装共用 GTNH 发布列表（ETag 条件缓存），404 时回退 tags 列表
  取最新 tag 再查该 tag 的 release；匿名限流 60次/时，配合缓存与可选 token。
LocalFolderSource：本地目录，最新版本=目录内可解析的最新 jar。
ManualSource：无上游，手动替换。CurseForgeSource：无 API key，仅返回页面链接供浏览器打开。
"""
import re
import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from .targets import classify_target
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
    prerelease: bool = False         # 默认包含测试发布
    target_status: str = "eligible"
    target_reason: str = ""


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
                ttl_hours=cfg.check_interval_hours, proxy=cfg.proxy,
                target_profile=src.get("target_profile", "unknown"))
        if st == "local_folder":
            return LocalFolderSource(src.get("path") or "", src.get("name_regex") or "",
                                     target_profile=src.get("target_profile", "unknown"))
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
                asset_regex: str = "", exclude_regex: str = "", *,
                target_profile: str = "unknown") -> list:
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
        if re.search(r"(?i)(?:^|[-_.])(sources|deobf|javadoc|api|dev)(?:[-_.]|$)", name):
            continue
        if classify_target(name, release_tag=tag, target_profile=target_profile).status != "eligible":
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
                 api_base: str = "https://api.github.com", proxy=None,
                 target_profile: str = "unknown"):
        self.owner, self.repo = owner, repo
        self.asset_regex, self.exclude_regex = asset_regex, exclude_regex
        self.tag_regex = tag_regex
        self.token = token
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl_hours = ttl_hours
        self.api_base = api_base.rstrip("/")
        self.proxy = proxy
        self.target_profile = target_profile

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
        if not rel.get("tag_name"):
            rel = dict(rel, tag_name=fallback_tag)
        option = self._release_option(rel)
        if option is None or option.target_status != "eligible":
            raise SourceError("标签对应发布不适用于 GTNH 或游戏平台需确认")
        return UpdateInfo(option.version, option.candidates, option.body, utils.now_str(),
                          option.target_reason, option.published_at)

    def _check_via_tags(self, *, force: bool = False) -> UpdateInfo:
        tags_data, _ = self._api(f"/repos/{self.owner}/{self.repo}/tags", "tags",
                                 force=force)
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
                               f"rel_{best}", force=force)
        except net.HttpError as e:
            if e.code != 404:
                raise
            note = "；".join(x for x in
                             (f"最新tag {best} 无Release资产，需手动下载", self._rate_note()) if x)
            return UpdateInfo(extract_version(best), None, None, utils.now_str(), note)
        info = self._from_release(rel, best)
        return info

    def check(self, current_version: str | None, *, force: bool = False) -> UpdateInfo:
        options = self.list_versions(force=force)
        if not options:
            return UpdateInfo(None, None, None, utils.now_str(), "没有找到 GTNH 发布")
        option = options[0]
        if option.target_status != "eligible":
            raise SourceError(option.target_reason or "游戏平台需确认")
        return UpdateInfo(option.version, option.candidates, option.body, utils.now_str(),
                          "；".join(x for x in (option.target_reason, self._rate_note()) if x),
                          option.published_at)

    def _release_option(self, rel):
        if not isinstance(rel, dict) or rel.get("draft"):
            return None
        tag = rel.get("tag_name") or ""
        if not self._tag_ok(tag):
            return None
        ver = extract_version(tag)
        if not ver:
            return None
        decision = classify_target("", release_tag=tag, target_profile=self.target_profile)
        assets = rel.get("assets") or []
        jars = [a for a in assets if isinstance(a, dict)
                and (a.get("name") or "").lower().endswith(".jar")
                and not re.search(r"(?i)(?:^|[-_.])(sources|deobf|javadoc|api|dev)(?:[-_.]|$)", a["name"])
                and (not self.exclude_regex or not re.search(self.exclude_regex, a["name"], re.I))
                and (not self.asset_regex or re.search(self.asset_regex, a["name"], re.I))]
        cands = pick_assets(jars, tag, self.repo, self.asset_regex, self.exclude_regex,
                            target_profile=self.target_profile)
        states = [classify_target(a["name"], release_tag=tag,
                                 target_profile=self.target_profile).status for a in jars]
        if decision.status == "excluded" or (states and all(s == "excluded" for s in states)):
            return None
        status = "eligible" if cands else decision.status
        if not cands and "unknown" in states:
            status = "unknown"
        reason = "" if cands else ("该 GTNH 发布无可用 jar，需手动处理" if status == "eligible"
                                    else "游戏平台未确定，请确认此下载源用于 GTNH")
        return VersionOption(ver, tag, rel.get("body"), rel.get("published_at"),
                             cands or None, bool(rel.get("prerelease")), status, reason)

    def list_versions(self, *, force: bool = False) -> list:
        """同一份发布列表供检查和安装使用；平台筛选先于排序和去重。"""
        options = []
        for page in range(1, 6):
            try:
                releases, _ = self._api(
                    f"/repos/{self.owner}/{self.repo}/releases?per_page=30&page={page}",
                    f"releases_page_{page}", force=force)
            except net.HttpError as e:
                if e.code != 404 or page != 1:
                    raise
                releases = []
            if not isinstance(releases, list) or any(not isinstance(r, dict) for r in releases):
                raise SourceError("发布列表格式错误，无法确认最新 GTNH 发布")
            options.extend(o for r in releases if (o := self._release_option(r)) is not None)
            if len(releases) < 30:
                break
        else:
            raise SourceError("发布搜索范围已达 150 条，请缩小下载源或手动选择；不能确认最新版本")
        if not options and (self.tag_regex or not releases):
            # 标签后备也必须走相同的平台筛选，不能绕过资产检查。
            info = self._check_via_tags(force=force)
            if info.latest_version:
                status = "eligible" if info.candidates else "unknown"
                options.append(VersionOption(info.latest_version, info.latest_version,
                    info.release_body, info.published_at, info.candidates, False, status, info.note))
        def date_key(option):
            try:
                dt = datetime.fromisoformat((option.published_at or "").replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    raise ValueError("missing timezone")
                return dt.timestamp()
            except (ValueError, TypeError, OverflowError, AttributeError):
                return None
        dates = [date_key(option) for option in options]
        if any(date is None for date in dates):
            # 部分缺失时不能仅把这些项排到最后：可能漏掉真正较新的发布。
            for option in options:
                option.target_reason = "；".join(x for x in (option.target_reason,
                    "发布时间不完整，按上游顺序显示，无法确认时间顺序") if x)
        else:
            options = [option for _, option in sorted(zip(dates, options),
                       key=lambda pair: pair[0], reverse=True)]
        seen, unique = set(), []
        for option in options:
            if option.version not in seen:
                seen.add(option.version)
                unique.append(option)
        return unique


# ---------- 本地目录 ----------

class LocalFolderSource(Source):
    source_type = "local_folder"

    def __init__(self, path: str, name_regex: str = "", *, target_profile="unknown"):
        self.path = Path(path) if path else None
        self.name_regex = name_regex or ""
        self.target_profile = target_profile

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
            decision = classify_target(p.name, target_profile=self.target_profile)
            if decision.status == "excluded":
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
        decision = classify_target(p.name, target_profile=self.target_profile)
        if decision.status != "eligible":
            raise SourceError(decision.reason)
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
                                 [DownloadCandidate(str(p), p.name, p.stat().st_size)],
                                 target_status=classify_target(p.name, target_profile=self.target_profile).status,
                                 target_reason=classify_target(p.name, target_profile=self.target_profile).reason)
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
