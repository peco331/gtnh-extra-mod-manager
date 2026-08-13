"""GTNH 额外MOD管理工具。

纯标准库实现：核心逻辑（本包） + CLI 壳（cli.py）+ Tkinter GUI 壳（gui.py）。
数据来源：https://gtnh.huijiwiki.com/wiki/可添加MOD
"""

__version__ = "1.0.0"

# 端别常量
SIDES = ("client", "server")
SIDE_LABELS = {"client": "客户端", "server": "服务端", "both": "双端"}
