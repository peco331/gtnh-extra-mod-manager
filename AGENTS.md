# GTNH Mod Manager 协作规则

- 这是 Windows 上的 GTNH Mod Manager / launcher 工程；项目状态和发布边界以 `README.md`、`pyproject.toml` 与当前 Git 历史为准。
- 源码修改后运行仓库已有的 Python tests；PyInstaller 的 `build/`、`dist/`、`.exe` 和本地操作数据不作为开发源。
- `data/`、`.claude/`、`.zcode/` 可能含用户路径、日志或临时状态，保持本地且不要提交。
- 不在本次审计中更新依赖、重构 UI、重新打包或修改实际启动器行为。
