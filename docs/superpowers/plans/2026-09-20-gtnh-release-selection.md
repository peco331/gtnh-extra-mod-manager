# GTNH 发布目标筛选实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 所有更新入口只在 GTNH 合格发布中选择最新版本，默认包含测试版，兼容说明不控制默认选择。

**Architecture:** 把平台证据识别与 Mod 版本比较分离，统一 GitHub 发布解析，保留 `(options, error)` 预取契约。网络响应与纯候选判定分离，便于后续查询并发而不并发写数据库。

**Tech Stack:** Python 3.10+、标准库 unittest、现有 Tkinter 与 urllib。

**Spec:** `docs/superpowers/specs/2026-09-20-update-workflow-design.md`。

## Global Constraints

- 默认包含 Pre-release/Beta，在符合 GTNH 目标的发布中按发布时间选择。
- 不把发布说明推断出的 GTNH 小版本兼容性作为自动选择门槛。
- 保留绑定源、过滤规则、手动选版、锁定、防自动降级和备份恢复。
- 不更新依赖、不发布、不推送、不打包、不操作真实 mods 或用户数据。
- 第一阶段只交付正确的目标筛选；速度、浏览器验证、设置另分实施计划，不能报告为已完成。

## Review Focus

1. Mod 自身版本号恰好像 Minecraft 版本号，不能误排除。
2. Release 内多个游戏平台资产，不得只验证 tag 就接受全部 jar。
3. 未标记版本的文件在通用仓库与已确认 GTNH 源中行为不同。
4. 同版本不同平台不能先去重再筛选，防止正确资产丢失。
5. 分页达到预算、没有资产及不完整响应，不能误报已最新。

## Task 1：平台证据判定与回归测试

**Files:** 新建 `gtnhmod/targets.py`、`tests/test_targets.py`；扩展 `tests/test_sources_mock.py`。

**Interfaces:** `TargetDecision(status, reason)`，status 为 eligible / excluded / unknown；`classify_target(file_name, *, release_tag='', target_profile='unknown')` 返回该对象。target_profile 仅允许 unknown / gtnh，gtnh 必须来自已确认的配置，不从 Wiki URL 或仓库名自动推断。

- [ ] 编写混合资产与明确 mc 前缀用例，先运行确认失败：

```python
def test_explicit_modern_mc_is_excluded(self):
    result = classify_target('Demo-mc1.20.1-4.0.jar')
    self.assertEqual(result.status, 'excluded')

def test_mod_version_is_not_mc_version(self):
    result = classify_target('Demo-1.20.1.jar', target_profile='gtnh')
    self.assertEqual(result.status, 'eligible')

def test_unknown_generic_asset_requires_confirmation(self):
    self.assertEqual(classify_target('Demo-4.0.jar').status, 'unknown')

def test_profile_cannot_override_explicit_conflict(self):
    result = classify_target('Demo-mc1.21.1-fabric-4.0.jar', target_profile='gtnh')
    self.assertEqual(result.status, 'excluded')
```

- [ ] 增加 release_tag 与文件名冲突、1.7.10 Forge、GTNH 构建、sources/deobf 排除的表驱动用例。所有用例使用合成文件名，不从用户目录读取。
- [ ] 实现纯判定函数，证据优先级为明确冲突、明确目标、已确认源配置、unknown；保留可展示原因。

```python
@dataclass(frozen=True)
class TargetDecision:
    status: str
    reason: str
```

- [ ] `py -m unittest tests.test_targets -v`；检查字符串边界，不允许单个宽泛数字正则吞掉 Mod 版本。

## Task 2：Release 与资产统一筛选

**Files:** `gtnhmod/sources.py`、`tests/test_sources_mock.py`。

**Interfaces:** `GitHubSource` 新增可选 `target_profile='unknown'`；`Source.from_entry` 从 source 字段传递。已有 VersionOption 增加带默认值的 target_status、target_reason，保持现有位置参数顺序。

- [ ] 添加两个发布的模拟 API 测试：较新的 mc1.21.1 与较旧的 1.7.10-GTNH；未配置 tag_regex 时也必须选择后者。

```python
self.assertEqual(source.check(None).latest_version, '2.0-GTNH')
self.assertEqual(source.list_versions()[0].version, '2.0-GTNH')
self.assertTrue(all('1.21.1' not in c.file_name
                    for c in source.list_versions()[0].candidates))
```

- [ ] 添加同 Release 多资产、测试版、发布时间与版本号排序相反、同版本跨平台资产、目标在第 2 页、5 页预算耗尽的模拟响应；记录请求数量。
- [ ] 运行 `py -m unittest tests.test_sources_mock -v`，确认新测试因当前 releases/latest 与评分逻辑失败。
- [ ] 统一 check/list_versions 使用相同的发布列表、目标判定和排序，按页缓存；不再让 check 绕过列表规则。只在目标筛选后去重。

```python
eligible = [option for option in options if option.target_status == 'eligible']
eligible.sort(key=lambda option: option.published_at or '', reverse=True)
```

排序实现应解析时间为 UTC；上例只表达筛选先于排序，不能接受非法时间静默高排。缺少时间时使用稳定后备顺序并提示。分页结果不完整通过 SourceError 表达，调用者不得变成空的成功列表。

- [ ] 无目标资产与未知平台保留明确原因；手动版本列表可展示 unknown，但不能默认执行。运行新旧 Source 测试并审阅预取契约。

## Task 3：统一默认选版与客户端交互

**Files:** `gtnhmod/updater.py`、`gtnhmod/gui.py`、`gtnhmod/cli.py`、`tests/test_compat_versions.py`、`tests/test_gui_smoke.py`、`tests/test_e2e.py`。

**Interfaces:** 保留 `list_install_options(...) -> (options, err)`、install_mod/update_mod 的 prefetched 二元组；option 字典传递 target_status、target_reason。兼容 compat 字段仍供提示使用。

- [ ] 编写预发布且 compat=incompatible 的合格目标默认被选中测试；明确平台 excluded 与 unknown 均不能默认应用。

```python
options = [dict(version='3.0-beta1', prerelease=True, compat='incompatible',
                target_status='eligible', candidates=[candidate])]
chosen, note = updater._pick_default_option(options)
self.assertEqual(chosen['version'], '3.0-beta1')
```

- [ ] 测试手动指定跨平台版本被拒绝、锁定和禁用继续跳过、补发低版本需确认，以及版本列表与检查结果目标一致。
- [ ] 运行对应测试确认旧“正式版/兼容优先”行为失败，再更改推荐标记与默认策略。删除不可达的兼容阻止分支，保留信息提示。
- [ ] 修改 GUI/CLI 的“最新兼容版”“推荐”文案为“最新 GTNH 发布”“发布说明提示”；解释 unknown 平台为何需要确认。版本选择器不得把明确排除项变成可安装项。
- [ ] 审阅所有 `prefetched=` 调用，运行 GUI 选版测试和 CLI/E2E 测试，防止再次出现 bare list 解包回归。

## Task 4：源配置与下载后冲突检查

**Files:** `gtnhmod/db.py`、`gtnhmod/gui.py`、`gtnhmod/cli.py`、`gtnhmod/downloader.py`、`tests/test_targets.py`、`tests/test_sources_mock.py`、`tests/test_compat_versions.py`。

**Interfaces:** `source.target_profile` 缺失按 unknown；只在用户明确确认该源/过滤规则用于 GTNH 后保存 gtnh；重新绑定源使旧确认失效。Wiki 合并保留同源配置，但不把确认传播至新仓库。

- [ ] 添加确认配置在同源刷新后保留、新源绑定后失效、配置缺失不崩溃的测试。
- [ ] 用内存 zip 构造仅含 fabric.mod.json、显式 mcversion=1.20.1 的 mcmod.info、空白元数据和正常旧式 Forge jar；测试冲突在备份/替换前拒绝。

```python
with self.assertRaises(VerifyError):
    verify_target_jar(modern_jar)
self.assertEqual(old_jar.read_bytes(), old_bytes)
```

- [ ] 实现 `verify_target_jar(path)` 的静态冲突检查；限制读取的元数据大小，解析错误给出需确认，不执行 jar。纯静态检查不宣称实际游戏兼容。
- [ ] GUI/CLI 在未知平台时提供“查看源/确认此源用于 GTNH”，不要求普通用户编辑正则。明确冲突不可由此确认覆盖。
- [ ] 运行以上回归和原有下载失败、文件占用、备份恢复测试。

## Task 5：文档与整体交付检查

**Files:** `README.md`、本计划与设计状态。

- [ ] 更新默认测试版、GTNH 目标筛选、兼容说明仅供参考、混合源未知状态的说明。
- [ ] 执行完整验证：

```powershell
py -m unittest discover -s tests -v
py -m compileall -q gtnhmod tests
git diff --check
git status --short
```

- [ ] 审阅最终差异，确认无个人路径、凭据、缓存、jar 和打包产物被纳入。
- [ ] 汇报实际测试数、模拟请求证据、分支/提交和工作区状态。明确第一阶段完成不等于 Wiki 浏览器方案、并发下载或设置改造完成。
- [ ] 第一阶段验证通过后按整体设计展开后续阶段；不把未经验证的浏览器实现先写成既成能力。


## 本次执行记录（2026-09-20）

- 用户已批准此计划，由当前会话直接实施；在既有功能分支工作，未创建重复源码目录。
- Task 1–4 已实现：平台判定、统一 Release 列表、测试版默认选择、GUI/CLI 源确认、下载前后门槛。
- 回归覆盖 GitHub 混合发布/资产、第二页目标、150 条预算、未知源、确认生命周期、发布说明警告、本地源、元数据冲突、手动选版门槛与防降级。
- 初始新增测试失败已实测；平台 API 缺失、旧默认策略和旧 jar 校验均出现预期失败，修改后通过。
- Ruling: 发布时间部分缺失时整体保留上游顺序并提示，而非把缺失项排到最后；避免隐藏可能更新的发布，仍不宣称已确认发布时间顺序。
- Ruling: Release 搜索达到 150 条整页时保守报告范围不足，不将部分列表冒充完整结果；大仓库需要后续改善索引策略。
- 独立审查发现明确旧 MC 证据被启发式覆盖、Mod 版本误判、缺失日期排序与两段 MC 版本边界，均补失败回归后修复。
- 本地源没有发布页时间语义，继续按 Mod 版本号排序，但使用同样的平台门槛。
- 完整自动验证结果见本次提交交付信息；未测试真实 Wiki 或游戏环境，未操作实际 mods，未打包或发布。
- 后续工作：并发查询和下载复用、统一任务交互、Wiki 验证可行性验证、设置简化；本阶段不宣称完成这些工作。
