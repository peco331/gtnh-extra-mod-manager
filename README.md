# GTNH Mod Manager

给 GTNH（GregTech: New Horizons）玩家用的额外 MOD、资源包与光影管理工具，Windows 图形界面 + 命令行。

目录来自 GTNH 中文 Wiki 的[可添加 MOD](https://gtnh.huijiwiki.com/wiki/可添加MOD)与[资源包与光影](https://gtnh.huijiwiki.com/wiki/资源包与光影)，也可以添加自定义来源。

## 能做什么

- **一键安装 / 更新**：直接在 Wiki 条目上安装，自动挑选适用于 GTNH（Minecraft 1.7.10）的版本，更新前会列出计划让你确认。
- **客户端与服务端分开管理**：各自记录安装状态与版本。
- **资源包与光影**：从 GitHub Releases / Modrinth 下载，同样支持检查更新。
- **日常管理**：启用 / 禁用、锁定（跳过更新）、备份与恢复、识别手动放入的未受管文件。
- **离线可用**：Wiki 刷新失败会明确报错并保留上次目录。

> 本工具只管理“额外内容”，不安装整合包、不启动游戏。更新前请关闭游戏或服务端；静态检查不能代替游戏内测试，核心 MOD 请谨慎操作。

## 快速开始

1. 从 [Releases](https://github.com/peco331/gtnh-extra-mod-manager/releases) 下载 `GTNHModManager-<版本>-win64.zip`，解压到可写目录。
2. 双击 `GTNHModManager.exe` 打开图形界面（`gtnh-cli.exe` 为命令行版）。
3. 在「设置」中填写客户端和/或服务端的 `mods` 目录（可「自动检测」），只用一端就只填一端。
4. 打开「可添加 MOD」，点条目上的「安装」。已有的 MOD 先「扫描」，未识别的 jar 可关联条目、登记为自定义 MOD 或忽略。

升级时保留 exe 旁的 `data/` 目录，只替换程序文件。

内嵌 Wiki 网页需要本机安装 Edge 或 Chrome；没有时可切换到「离线目录」。遇到 Cloudflare 人机验证，在弹出的专用浏览器窗口里完成即可，不会读取你自己浏览器的数据。

## 常用功能

**更新**
- 「检查更新」只查询已安装、已启用且未锁定的 MOD；「可更新」标记重启后仍保留。
- 「全部更新」/「更新选中」会先展示确认表，确认后才下载替换。
- 查询失败、需手动处理、无法比较版本的条目会如实标出，不会显示成“已最新”；不会自动降级。
- 旧文件自动备份到 `data/backup/`，可在「备份与恢复」还原。

**选版规则**
- GitHub 来源默认含 Pre-release，排除明确标注其他 MC 版本或 Fabric / NeoForge / Quilt 的构建。
- 缺少 GTNH 平台依据的自定义来源需先确认；整合包小版本兼容只作提示。
- 同一发布有多个难以区分的 jar 时会让你选择具体文件。

**自定义来源**：GitHub 仓库、本地文件夹或手动维护。CurseForge 等不支持自动下载的来源会给出页面入口，下载后放进目录重新扫描即可。

**资源包与光影**：入口为「资源包与光影」页。安装到 `resourcepacks/`、`shaderpacks/`，zip 保持压缩；是否在游戏内选用由游戏设置决定，工具不改动。自动下载支持 GitHub Releases 与 Modrinth。

**外观**：设置页可选跟随系统 / 浅色 / 暗色。

**列表**：用法同资源管理器的「详细信息」视图。点表头排序；每列都能拖动分隔线调宽（最后一列也可以），放不下时横向滚动；双击分隔线或右键表头可让列宽适合内容；文字被截断时把鼠标停在上面即可看到全文。拖过的列宽会被记住。

## 命令行与计划任务

```powershell
gtnh-cli.exe --check          # 检查已安装 MOD
gtnh-cli.exe --update-all     # 直接更新可更新的 MOD（无确认）
gtnh-cli.exe --packs-check    # 检查资源包与光影
gtnh-cli.exe --packs-update   # 更新已安装的资源包与光影（无确认）
```

不带参数则进入交互菜单。批量更新只处理已安装条目；有查询错误或更新失败时退出码为 `1`，需手动处理的条目会打印原因。建议在游戏关闭时运行计划任务并保留日志。

## 数据位置

默认保存在 exe 旁的 `data/`（可用环境变量 `GTNHMOD_DATA_DIR` 指定，目录不可写时回退到 `%APPDATA%\GTNHModManager`）。

| 位置 | 内容 |
| --- | --- |
| `config.json` | 目录、GitHub Token、代理等设置（含凭据，请勿分享） |
| `mods_db.json` / `packs_db.json` | 目录、自定义来源与绑定 |
| `installed.json` | 安装版本、锁定状态 |
| `backup/` | 旧文件备份，默认每个条目保留 3 份 |
| `logs/operations.log` | 操作日志 |

迁移时请保留整个数据目录，仅重新扫描无法恢复自定义来源、锁定与绑定。

## 常见问题

- **Wiki 刷新失败 / Cloudflare 验证**：安装 `curl_cffi` 可走快速通道；仍被拦截会打开专用浏览器窗口，完成验证并保持窗口打开。
- **GitHub 限流或检查很慢**：在设置里填写 GitHub Token，点击「保存并测试 GitHub API」；默认缓存 6 小时。
- **文件未识别 / 版本未知**：核对文件名与来源绑定，关联已有条目或登记自定义条目。
- **更新时文件被占用**：关闭游戏或服务端后重试，下载内容保留在缓存中。

## 从源码运行与开发

需要 Windows + Python 3.10+（含 Tkinter）：

```powershell
py -m pip install --user curl_cffi websocket-client Pillow   # 可选依赖
py -m gtnhmod gui
py -m gtnhmod cli
py -3.10 -m unittest discover -s tests
```

打包使用 `scripts\build_exe.bat`（需 `pyinstaller`）。版本号只在 `gtnhmod/__init__.py` 维护，推送 `v*` 标签触发 [Release 工作流](.github/workflows/release.yml)。改动记录见 [CHANGELOG.md](CHANGELOG.md)，贡献前请阅读 [AGENTS.md](AGENTS.md)。
