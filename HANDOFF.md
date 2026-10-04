# HANDOFF — 资源包与光影管理功能（交接说明）

> 日期：2026-10-04　分支：`main`　基线 commit：`bcb85fa`（v1.6.4 之后，工作区版本号已到 1.6.5）
> 读者：接下来接手这个工作区的人 / agent。先读本文再动代码。

## 最近更新 — 2026-10-04 15:09

已按用户后续授权完成本地提交、构建与替换。实现提交 `d2db4fe`，GUI/CLI 的 v1.6.5
成品已替换项目根目录两个 exe，与 dist 的 SHA-256 一致。旧版备份、成品验证及哈希
见第 9 节。未推送、未创建或修改发布 tag、未发布远端 Release。
此前第 4 节的待修问题已处理；Modernity 的严格匹配规则及 Config/packs 的职责边界保留。

## 1. 原始交接时的工作区现状（历史记录）

**全部改动都未 commit。** 工作区里叠着两层东西：

1. **「资源包与光影」整套新功能**（由 DeepSeek 生成，未经压缩的原始实现 + 它自己写的测试与 README 章节）。
2. **一轮人工审计修复**（本会话完成）：修掉 2 个高危问题、若干缺陷与死代码，全部带回归测试。

改动清单：

| 文件 | 状态 | 内容 |
|---|---|---|
| `gtnhmod/packs.py` | 新增（已改） | 包管理核心：目录识别、扫描、文件名匹配打分、下载源解析（GitHub/Modrinth）、安装/更新/备份、PacksDB |
| `gtnhmod/packs_wiki.py` | 新增（已改） | wiki「资源包与光影」页抓取与 wikitext 解析 |
| `gtnhmod/cli.py` | 修改 | CLI 菜单 11 + `--packs-check` / `--packs-update` |
| `gtnhmod/gui.py` | 修改 | 「资源包与光影」页签、详情/下载源/资产绑定/备份对话框 |
| `gtnhmod/config.py` | 修改 | `pack_folders` 配置、目录探测 |
| `gtnhmod/wiki.py` | 修改 | `WikiProfile` 抽象（多页面共用抓取通道与浏览器验证） |
| `tests/test_packs_manager.py` `tests/test_packs_parse.py` `tests/test_packs_cli.py` | 新增 | 功能回归（全部离线打桩） |
| `tests/test_browser_verification.py` `tests/test_gui_smoke.py` `tests/test_settings_network.py` | 修改 | 新增用例 |
| `tests/fixtures/packs_shader_sample.txt` | 新增 | 2026-10 抓取的真实页面 wikitext，解析测试夹具 |
| `README.md` | 修改 | 新功能文档 + `--packs-update` 语义更正 |

`data/`、`.claude/`、`.zcode/` 含用户路径与本地状态，**不要提交**。

## 2. 审计修复了什么（已全部完成并测试通过）

### P1-1 `--packs-update` 曾会批量安装整个 wiki（已修）

原实现用 `PacksDB.installable()`（= wiki 全部非自带条目）当安装清单，非交互模式下
无任何确认，会把上游所有可自动下载的资源包/光影全部下载（几十 GB 级）。
已改为与 mod 侧 `--update-all` 一致的语义：**先 `reconcile_installed` 再按安装记录过滤，
只动已安装条目**。涉及：

- `gtnhmod/cli.py` 的 `run_packs_update()` 与 `CliApp._packs_update_all()`
- CLI 菜单项改名「安装/更新全部已安装条目」；`README.md` 语义说明更正
- 回归：`tests/test_packs_cli.py`（未安装条目不被批量拉取 / 已最新不重装 / 未设目录跳过）

如果产品本意就是"全量安装"，回退点在上述两处过滤 + README，但**不推荐**（危险默认）。

### P1-2 浏览器人机验证会被挑战页中止（已修）

`gtnhmod/wiki.py` 的 `fetch_wikitext_via_browser()` 轮询循环里，`profile.validate(text)`
对任何一次 200 响应直接抛 `HttpError` → 浏览器被杀、整个验证流程失败。
已改为：校验不通过视为「用户还没过 Cloudflare」，`continue` 继续轮询直至超时。
影响面含「可添加MOD」页面（同一函数）。回归：`tests/test_browser_verification.py::TestPollingThroughChallenge`
（全打桩：浏览器/CDP/websocket/sleep 都 mock，离线跑）。

### P2 批次

- **死代码清理**（DeepSeek 编辑事故残留）：`packs.py` `match_pack()` 内 return 之后粘了一整段旧版
  `scan_pack_dir`（已删）；删除无引用的 `set_pack_dir`（Config 同名方法才是实际使用的）、
  `normalize_pack_name`、`_version_tokens`、`asset_snapshot`、`stale_check`、
  `packs_wiki.NON_PACK_ASSET_RE` 及 `import time`。
- **GUI（`gtnhmod/gui.py`）**：安装成功后清除 `_pack_updates` 里过时的「可更新 → x」标记；
  `_on_tab_changed` 加 busy 保护（后台安装时主线程不再并发读写 packs_db.json）；
  登记自定义包单选路径把外层对话框作为 `parent` 传入，登记成功后外层对话框会自动关闭。
- **`reconcile_installed()` 目录缺失保护**（`packs.py`）：安装目录不存在（实例没启动过、
  盘暂不可用）时跳过校正，不再清空该类型全部安装记录（否则 installed_at/source_url/绑定会被抹掉）。
  回归：`test_missing_pack_dir_keeps_records`。
- **版本提取修复**（`packs.py` `PACK_VERSION_RE`）：起点边界补空格
  （`\s(?=[vbr]?\d+[.\-]\d)`，仅当空格后是明显版本形态）。修掉
  `GT V1.0.0` 被提取成 `0.0` 的 bug（GTNH 自带资源包真实命名），同时保证
  `Texture Pack 4K`、`Some Pack 2` 不误判。用例在 `TestExtractPackVersion.CASES`。

### 测试整理

- 删除只覆盖已死代码的 `TestStaleCheck`、`test_snapshot_marks_ambiguous`；
- `test_missing_file_clears_record` 改名 `test_record_corrected_when_recorded_file_missing`（原断言是"校正"不是"清除"）。

## 3. 测试与环境注意（Windows）

- **`python` 命令是 Windows Store 占位符**（无输出、退出码 49），**必须用 `py`**。
- 系统 Python 3.10.10（`C:\Users\26870\AppData\Local\Programs\Python\Python310`），项目**没有 venv**；
  本会话已把 pytest 装进该系统 Python（此前未装，DeepSeek 疑似从未跑过测试）。
- 跑全量：`py -m pytest tests/ -q`，约 15 秒。原交接为 **477 passed**；续审最终结果见第 8 节。
- 测试全部离线（GitHub/Modrinth 响应打桩），不联网、不碰真实游戏目录。
- websocket-client、requests 已在系统环境；可选依赖 `curl_cffi`（fast-wiki extra）未装也能过测试。

## 4. 已知未处理事项（有意留下，按优先级）

1. **GUI 冒烟测试 TclError 闪烁（已处理）**：续审复现第二测试类初始化时 Tcl 库文件
   读取失败，文件实际存在。单纯垃圾回收不能稳定修复。测试模块改为共享一个 Tk
   解释器，测试类仍使用独立数据目录/GuiApp/控件，类间取消回调并销毁控件，模块结束
   销毁解释器。没有跳过用例或修改系统 Tcl 安装，也没有改变实际 GUI 启动行为。
2. **`Modernity-2.0.x-3e5583d.zip` 一类文件不匹配 `Modernity-GTNH` 条目**：文件名不含
   "GTNH"，匹配规则刻意要求条目名多词覆盖（有测试锁定 `Modernity-f1-*` 不得误配）。
   用户出路：「登记自定义包」或在条目详情里绑定下载资产。不要为了这一个文件放宽匹配器。
3. **CLI skip 状态提示（已处理）**：交互和非交互检查均输出计划里的真实原因。
   GUI 检查也保留查询失败、需手动处理和无法比较版本的结果，不再一律报「均为最新」。
4. **安装记录的 version 可能与文件名不一致**（version 来自上游 tag，文件名可能没版本号），
   设计如此，排查问题时别当成 bug。
5. `config.py` 的 `Config.pack_dir` 访问器与 `packs.resolve_pack_dir` 职责有部分重叠
   （写走 Config，读/推断走 packs），可接受，暂不合并。

## 5. 功能速览（30 秒版）

- 数据源：wiki 页「资源包与光影」，`packs_wiki.py` 解析 → `packs_db.json`（条目 + 安装记录 +
  用户别名/绑定源）。自定义条目（`custom=true`）刷新 wiki 不会被标记删除。
- 安装目标：`resourcepacks/`、`shaderpacks/`（保留 zip 不解压）；目录由客户端 mods 目录推断，
  或在设置页显式指定（`pack_folders`）。
- 可自动下载：GitHub Releases、Modrinth；CurseForge/Discord 等只给手动下载指引。
- 多资产仓库不猜：歧义时要求用户「选择下载资产」绑定（`asset_regex`），绑定跨 wiki 刷新保留。
- 更新边界：无法解析版本 → `unknown` → 不自动判更新；本地更高不降级。
- 备份：安装/更新前旧文件进 `data/backup/{resourcepacks|shaderpacks}/{条目id}/`，按 `backup_keep` 轮换。

## 6. 协作边界（摘自 AGENTS.md，仍然有效）

以下为原审计边界；用户后续明确授权了本地提交、构建和替换，见第 9 节。

- 源码改动后跑 `py -m pytest tests/ -q`；
- 不更新依赖、不重构 UI、不重新打包（PyInstaller 的 build/dist 不算源码）；
- `data/`、`.claude/`、`.zcode/` 保持本地不提交；
- 版本号以 `pyproject.toml` 与 `gtnhmod/__init__.py` 为准（当前一致：1.6.5），发布 tag `v*`。

## 7. 如果接下来要提交

建议拆两笔，便于回溯：

1. `feat: 资源包与光影管理（wiki 解析/扫描匹配/安装更新/备份 + GUI/CLI）` —— 新功能的主体；
2. `fix: 资源包功能审计修复（--packs-update 语义、浏览器验证轮询、版本提取、reconcile 保护、死代码）`
   —— 本会话的修复与测试。

混在一起也能接受，但 P1-1 的语义变更值得在 commit message 里单独说清楚。

## 8. 续审修复与最终验收（2026-10-04）

- **未知本地版本不自动更新**：此前 `version_status(None, remote)` 返回 `newer`，
  无法证明不会降级；现返回 `unknown`，已安装项跳过，首次安装及手动选择资产仍可执行。
- **下载写入边界**：拒绝路径穿越、Windows 设备名/数据流、非 zip 文件名及无效包结构；
  校验通过再备份、写临时文件、原子替换，同名目标写入失败保持原内容。
- **跨文件名更新**：新包通过校验并备份旧 zip 后再安装；成功后只移除当前目标目录里
  此条目记录的旧 zip。外部路径、链接、文件夹及其他包不自动清理。
- **备份完整性**：时间戳碰撞用递增编号保留各份；轮换按备份创建时间，避免复制源的
  陈旧 mtime 导致新建备份立即被删。
- **安装版本记录**：扫描同一文件保留上游记录版本；换成无版本文件时清掉旧版本号，
  避免错误比较。没有把上游 tag 与文件名版本不一致当成错误。
- **GUI 并发与结果**：`refresh_packs` 在后台忙碌期间挂起，解除忙碌后补刷新，覆盖筛选/
  重新扫描/Wiki 完成回调；检查结果显示失败及需人工判断数量，日志保留每条真实原因。
- **回归与边界**：关键缺陷已用离线回归验证修复前失败、修复后通过；全量测试、编译检查、差异检查结果
  记录在下方。未连接上游做在线验收、未修改实际游戏目录、未游戏内测试。

最终验证：`py -m pytest tests/ -q --tb=short`：**489 passed、72 subtests passed**（16.55 秒）；
`py -m compileall -q gtnhmod tests` 与 `git diff --check` 均通过。
分支仍为 `main`，暂存区为空，原有实现与本轮修复均未提交。没有发布验收或游戏内运行结论。

## 9. 本地提交、构建与替换（2026-10-04 15:09）

用户在续审结束后授权「提交、构建并替换」。已完成：

- 实现提交：`d2db4fe`（资源包/光影功能及审计修复，15 个任务文件）；运行数据与 exe 未入 Git。
- 构建前重新验证：**489 passed、72 subtests passed**；编译和差异检查通过。
- 使用现有 `scripts/build_exe.bat`，两个 PyInstaller 构建均成功；未安装或更新依赖。
  构建有既有的可选模块/打包工具兼容性警告，成品冒烟与本地 HTTP 验证未见运行错误。
- GUI 成品：v1.6.5 窗口正常创建，通过 WM_CLOSE 正常退出，隔离数据目录没有 GUI 错误日志。
- CLI 成品：资源包 Wiki 本地 HTTP 请求、39 条解析结果、两种包类型、数据库与原文缓存
  落盘均通过；`--packs-check` / `--packs-update` 正常退出且没有批量下载未安装包。
- 仓库既有 `verify_packaged_wiki.py`：两次 MOD Wiki 请求/解析/落盘验证通过。
- 根目录 `GTNHModManager.exe`、`gtnh-cli.exe` 已替换，哈希与 dist 一致；用户配置、
  data 及真实游戏目录保持原状。

| 成品 | SHA-256 | 字节数 |
|---|---|---:|
| `GTNHModManager.exe` | `799978A20D59FB7BD5273D44AF8BFE34A3F11561C2B4111B06341C57E9F2429B` | 82580478 |
| `gtnh-cli.exe` | `1195171F57BCB7F32A5D6E8951528716F458E732F642A740504D7F57D9E52413` | 82501092 |

回退目录：`dist/rollback/20261004-150359/`（Git 忽略）。`manifest.json` 记录旧程序原路径、
备份路径与已核对的哈希；`replacement.json` 记录新成品哈希。需要回退时先关闭工具，再
用 manifest 中的根目录备份替换两个根目录 exe。该目录保留旧成品与任务用成品验证脚本，
用于恢复与复核，不是开发源或发布包。

所有成品验证使用临时数据目录/本地 HTTP，未验证真实 Wiki 当前可用性或游戏内加载。
版本仍为 1.6.5，现有 tag 保持原样；本轮只是本地构建替换，未远端发布，也不改变项目生命周期。
