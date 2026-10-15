"""打包前生成 gtnhmod/_build_info.py，记录构建来自哪个提交。

只有当前提交恰好带有与 __version__ 一致的 ``v<版本>`` 标签、且工作区干净时，
才标记为发布构建（界面显示纯版本号）；其余构建显示 ``<版本>-dev+<提交号>``。

用法：python scripts/write_build_info.py                    生成
      python scripts/write_build_info.py --require-release  生成；不是发布构建则失败（发布工作流用）
      python scripts/write_build_info.py --clean            删除（构建结束后调用）
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "gtnhmod" / "_build_info.py"


def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main():
    # CI 的 Windows 控制台是 cp1252，中文提示会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if "--clean" in sys.argv:
        TARGET.unlink(missing_ok=True)
        return
    text = (ROOT / "gtnhmod" / "__init__.py").read_text(encoding="utf-8")
    version = re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)
    commit = git("rev-parse", "--short", "HEAD")
    # 只看已跟踪文件的改动：构建产物和本地数据不算“未提交修改”
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    # GitHub Actions 中以触发构建的那个标签为准（浅克隆里的本地标签不可靠）；
    # 本地构建则看当前提交上的标签。
    if os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITHUB_REF_TYPE"):
        ref_type = os.environ.get("GITHUB_REF_TYPE")
        tags = [os.environ.get("GITHUB_REF_NAME", "")] if ref_type == "tag" else []
    else:
        tags = git("tag", "--points-at", "HEAD").split()
    release = f"v{version}" in tags and not dirty
    if "--require-release" in sys.argv and not release:
        sys.exit(f"标签 {tags or '（无）'} 与 __version__ {version} 不一致，或工作区有未提交修改，拒绝发布构建")
    TARGET.write_text(
        '"""构建信息（scripts/write_build_info.py 自动生成，不提交）。"""\n'
        f"BUILD = {{'commit': {commit!r}, 'dirty': {dirty!r}, 'release': {release!r}}}\n",
        encoding="utf-8")
    label = version if release else f"{version}-dev+{commit}" + (".dirty" if dirty else "")
    print(f"构建版本：{label}")


if __name__ == "__main__":
    main()
