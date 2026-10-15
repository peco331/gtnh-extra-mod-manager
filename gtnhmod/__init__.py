"""GTNH 额外MOD管理工具。

纯标准库实现：核心逻辑（本包） + CLI 壳（cli.py）+ Tkinter GUI 壳（gui.py）。
数据来源：https://gtnh.huijiwiki.com/wiki/可添加MOD
"""

# 唯一的版本来源（pyproject.toml 从这里读取）。表示「下一次发布」的版本：
# 只在发布时修改，日常修复不改；本地构建靠提交号区分，见 display_version()。
__version__ = "1.7.0"

try:  # 打包时由 scripts/write_build_info.py 生成，源码运行时不存在
    from ._build_info import BUILD
except ImportError:
    BUILD = None


def display_version() -> str:
    """界面显示的版本：发布构建为 ``1.9.2``，其余为 ``1.9.2-dev+<提交号>``。"""
    if not BUILD:
        return f"{__version__}-dev"
    if BUILD.get("release"):
        return __version__
    commit = BUILD.get("commit") or "unknown"
    return f"{__version__}-dev+{commit}" + (".dirty" if BUILD.get("dirty") else "")

# 端别常量
SIDES = ("client", "server")
SIDE_LABELS = {"client": "客户端", "server": "服务端", "both": "双端"}
