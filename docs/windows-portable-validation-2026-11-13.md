# Windows 便携单文件交付记录（2026-11-13）

> 文档类型：本轮验证记录 · [Windows 使用指南](windows.md) · [维护交接](session-handoff.md)

本轮将 Windows 冻结入口改为默认使用实际 exe 所在目录，而不是开发 checkout、当前工作目录或旧版用户目录。目标为 Windows 10/11 x64；Linux 与源码运行的默认布局保持。技术栈为 CPython 3.12、PySide6/Qt、现有 AgentCore、FastAPI/Web 资源及 CPU 检索依赖，采用 PyInstaller onefile，保留显式 onedir 选项。

当前验证分为真实 exe、原生源码与受控 Qt 组件三类：0.6.0 候选默认首启离线自检 15/15、真实 NTFS 只读回归 10/10、独立 exe/ZIP 字节与校验通过；源码 Qt 离屏五项通过。可见窗口交互曾被用户 Esc 停止，未计为通过。代码与发布入口为 [PR #52](https://github.com/vukencc/Channel-Agent/pull/52) 和 [0.6.0 Release](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.0)。以下本机候选哈希不代替正式标签流水线资产的摘要。

## 初始化与路径边界

1. bootloader 先将内嵌运行库展开到操作系统临时目录；`sys.executable` 仍指向用户拿到的 exe，`_MEIPASS` 仅供读取运行资源。
2. 在导入 Core 配置前选取数据根，优先级为 `--data-dir`、`AI_AGENT_PROJECT_ROOT`、exe 父目录；`--use-user-data` 显式选取独立的 `ChannelAgentPortable` 用户目录。
3. 首次建立 `config/`、`data/`、`cache/`、`logs/` 与最小 `.env`，逐个探测管理目录的实际写入能力。初始化加进程间锁，文件刷新后原子发布，已有配置和公开资料不覆盖。
4. 会话与模型连接分别放在 `data/state/` 和 `config/`；默认会话工作区为 `data/workspaces/`，用户可明确选择 `data/projects/default/` 等项目目录。应用、凭据、会话、知识库和缓存不得作为项目授权。
5. 默认根确实不可写时，GUI 提供用户目录、手动目录、取消。明确指定目录失败不静默换根；不安全链接、无效配置等错误也不绕过。
6. 进程内缓存与临时文件变量指向所选根；bootloader 的前置运行库展开属于单文件机制，不是持久数据目录。可搬移整个数据根；已绑定的项目保存目录身份，搬移后需重新选择，不自动给新路径授权。

没有修改用户或系统环境变量，没有迁移旧版用户目录。使用新建临时目录或独立 `.cache/reports/` 保存验证数据；未运行真实模型或用户 Flow。

## 回归与问题处理

先建立红测，再实现根选择、首次初始化、目录不可写、原子发布、资源释放和配置保留。审查额外发现并修复两项：便携根内实际配置的监督器凭据路径遗漏保护；手写逐行 `.env` 校验误拒合法多行值。前者加入真实配置路径的双向重叠拒绝，后者复用 dotenv 解析器且保留权限、大小、UTF-8、NUL 和链接校验。Windows `rename_noreplace` 只通过显式 `file_only=True` 收紧便携文件发布，通用目录移动保持。

| 验证 | 结果与范围 |
| --- | --- |
| Linux 新增/相关专项 | 107 passed、4 Windows 专属 skipped |
| Windows 原生专项 | 110 passed、21 skipped；包含实际 Windows 文件句柄分支，跳过项含宿主缺少符号链接权限及 POSIX 专项 |
| 真实隔离探针 | Bubblewrap + prlimit 通过，未放宽沙箱 |
| Linux 完整回归 | 最新 2480 passed、50 skipped、5 deselected（200.11 秒），真实隔离探针通过；此前 2476 passed、49 skipped、5 deselected（187.10 秒），独立网络命名空间、临时数据路径，未改变测试断言；更早为 2476 passed、46 skipped（185.79 秒） |
| 双平台 CI | [43351849509](https://github.com/vukencc/Channel-Agent/actions/runs/43351849509)：Linux 2475 passed、55 skipped、5 deselected；Windows 950 passed、48 skipped，均通过。此前 [43348181678](https://github.com/vukencc/Channel-Agent/actions/runs/43348181678) 来源 `9fa4e7f`，Windows 945 passed、48 skipped；`ac200c5` 的 [43344439705](https://github.com/vukencc/Channel-Agent/actions/runs/43344439705) 也通过 |
| 只读审查 | 最终实现无新增确定缺陷；原先两项审查发现已有红测及修复 |
| 实际单 exe | 0.6.0 候选仅 exe 自检 15/15；实际只读 CLI 负例通过，具体范围见下文。可见 GUI 交互未完成 |
| Source Qt 离屏组件 | 5 passed（9.50 秒）；真实 NTFS 拒绝创建触发真实 QMessageBox，验证用户目录、手选目录、取消、选择器取消和不安全配置拒绝。只有系统目录选择器被替换，frozen 元数据及公开资源为受控夹具，不是实际 exe 可见交互 |

Linux 首轮完整回归为 3 failed、2464 passed、46 skipped、5 deselected（232.13 秒）。一个失败来自配置模板漏列两个新设置，已补齐并用原配置覆盖断言验证；另一个是测试更新期间收集的旧 bare-key 夹具，新专项已按 dotenv 真正语法错误验证。第三个旧代理用例期望连接 `127.0.0.1:1` 立即被拒，但本机 WSL 该端口实际超时。原用例隔离重跑仍失败；相同原用例在独立网络命名空间 1 passed（0.11 秒），直接 socket 对照分别为超时和 `ECONNREFUSED`。未改代理实现、宿主网络、时限或原断言，保留原环境失败。

第一次命名空间全量实验将整个文件系统设为只读，但遗漏 CLI 的默认工作区和 `/var/tmp`：5 failed、2471 passed、46 skipped、5 deselected（186.35 秒），均为测试目录不可写。随后显式设置 `SANDBOX_DIR`、`AGENT_STATE_DIR`、审计、RAG 到临时路径，并提供临时 `/var/tmp`，完整通过。测试使用锁定同步后的环境，原命令为 `uv run --no-sync pytest -m 'not integration' -q`，附加 `-p no:cacheprovider` 避免向只读源码目录写 pytest 缓存。最初宿主全量可能创建随机测试工作区；临时状态已被 pytest retention 清理，无法安全追溯，故未按空目录猜测删除任何既有工作区。

## 实际成品与依赖

### 正式 0.6.0 发布

[PR #52](https://github.com/vukencc/Channel-Agent/pull/52) 的合并提交及注解标签 `v0.6.0` 指向 `65d082eaf212107144466499f24be30ac5ae53c6`。[正式 Release](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.0) 于 2026-11-13 03:16:03 UTC 发布，非预发布；[发布运行 43356195115](https://github.com/vukencc/Channel-Agent/actions/runs/43356195115) 六项任务均成功。资产共六项：Windows 独立 exe、ZIP、Linux tar.gz 及各自 SHA256 侧车。

| 正式资产 | 字节 | SHA256 |
| --- | ---: | --- |
| `ChannelAgent-0.6.0-windows-x64.exe` | 331,163,213 | `31ea39fc78da36e5672bb4aaa2818d3976d53aeef8021c04c0b8e3c26b73d19f` |
| `ChannelAgent-0.6.0-windows-x64-20261113-031426.zip` | 329,415,765 | `0432c9414765072e913ab5dab9b38f5e7446b49f61b85cd29f0788f25bd255a6` |
| `ChannelAgent-0.6.0-linux-x64-20261113-031239.tar.gz` | 502,754,869 | `90978cd8c3f17a7fc70d470b8ac0e6f55af6572eb3b4da99631ef2b6f23ac69d` |

最终 [PR CI 43355569316](https://github.com/vukencc/Channel-Agent/actions/runs/43355569316) Linux 2475 passed、60 skipped、5 deselected（212.00 秒），Windows 955 passed、48 skipped（158.43 秒）。正式标签首次 Windows job `131658698016` 失败：`test_runtime_models` 的权限用例辅助 PowerShell 冷启动进程在 15 秒超时，尚未触发权限断言。原用例本机 1 passed（2.62 秒）；未改代码或 15 秒期限，一次有界重跑为 955 passed、48 skipped（135.85 秒），随后双平台正式包构建成功。

正式资产独立审计无发现。实际下载的 Windows ZIP SHA256 与上表一致，228 条目 CRC 和安全路径通过，manifest 所列 227 个文件的大小和 SHA256 全部匹配；manifest 记录版本 0.6.0、上述来源、干净工作树及仅 exe 默认根自检，ZIP 内 CI 自检记录为 15/15、无错误。128 应用代码模块、12 静态文件、91 WSL 源码和 224 公开资料与源码一致，三个包的内嵌元数据均为 0.6.0。ZIP 内 exe 的大小与 SHA256 与独立 exe 的 API 摘要及实际下载的侧车一致；没有重复下载独立 exe。

三份 SHA256 侧车均实际下载并与各自服务端摘要核对。Linux tar.gz 仅核对成功的构建任务、API 摘要和实际侧车，未下载、解包或独立运行。此次审计未运行 exe、GUI 或模型；记录位于原生 `.cache/reports/release-v0.6.0-official-audit-20261113-110548-7064175eada6/audit-result.json`，同目录保留从 ZIP 提取的 exe。

第一份真实 onefile 来源 `ac200c5`，331,131,146 字节，构建离线自检 15/15。独立审计确认 128 应用模块、12 静态文件、91 WSL Core 源码及 224 份公开资料一致，无 `_internal` 或外部归档依赖；未嵌入实际 `.env`、会话、项目或凭据。完整 ZIP 和清单校验通过，但**此包不能作为首启验收通过的交付**。

仅复制该 exe 到普通可写目录后，首次 `--portable-info` 运行 37.153 秒，退出 1，仅留下 `.portable.lock`、没有初始化目录或报告。原始失败保存在本轮独立报告目录。独立 tiny console 冻结程序与 Native Python 都定位到 `protect_private_path`：新文件已经归当前用户所有，继承 ACL 允许 Modify，但没有 WRITE_OWNER；函数强制重复设置 owner 与私有 DACL，返回 WinError 5。同一新文件仅设置受保护 DACL 成功，重复 owner 或 owner+DACL 失败。构建自检使用的私有临时目录具有更充分的继承权限，不能覆盖这个普通目录场景。

真实 Windows 新回归先为 3 failed（4.17 秒）。`da6b22b` 在安全句柄持有期间查询 owner：已是当前用户则只设置受保护 DACL，否则保留原单次 owner+DACL 及权限要求，操作后再核验 owner。无查询失败降级、无先提权再改 owner 的两阶段处理，已有非私有 `.env` 仍拒绝且不修改。修复后 Windows 相关专项 104 passed、21 skipped（87.43 秒），包含三项新 ACL 用例。独立安全审查无新增发现。

`077bb74` 进一步调整构建验收：先复制单 exe 到独立目录，移除预设的数据根及路径变量，从无关工作目录执行原完整离线自检并断言 exe 旁目录确实生成；保留 600 秒原时限、15 项能力及必需依赖，无需安装模型权重。没有把源目录包或指定根的通过追认为默认便携首启成功。

`077bb74` 的 [CI 43347227648](https://github.com/vukencc/Channel-Agent/actions/runs/43347227648) Windows 为 942 passed、48 skipped、3 errors：管理员运行器的默认 token owner 是 Administrators，测试夹具不能假定新文件已归 token user。`9fa4e7f` 仅修复夹具：复制线程令牌，将副本的默认 owner 设为当前用户并禁用副本特权，结束时恢复原线程身份，保留 OWNER 重写必须被拒绝等断言；没有修改原进程令牌、系统安全设置或产品权限。Native 3 passed（4.33 秒），管理员 CI 随后通过。

旧修复版 `ChannelAgent.exe` 为 331,131,590 字节，构建自检从仅含 exe 的默认根运行，离线 15/15；128 应用模块一致。实际复制单 exe 的首次启动、重复保留配置与中文空格路径搬移通过；其六项 CLI 正例通过，两项只读用例出错，见下文。中断前可见 GUI 未完成。此前 onedir 成品及第一份 onefile 的构建通过均不能代替它们。

### NTFS 拒绝创建与回退修复

继续验证 `020226` 成品时，在真实 NTFS 根对当前用户 SID 拒绝创建权限（`0x6`）：界面只显示初始化锁安全检查错误，没有出现回退选择。命令行复现退出 1、耗时 41.143 秒，报告 `error.type=unsafe`；独立写入权限检查确认目录确实不可写。证据分别位于本轮 `portable-fallback-resume-20261113-d5e1a047` 和 `portable-cli-remaining-20261113-d9030196` 独立报告目录。该成品的回退验证因此失败。

`7873f4d` 修复初始化锁处理：直接由操作系统返回的 `EACCES`、`EPERM` 或 `EROFS` 按不可写处理；没有 errno 的手工安全检查仍判为不安全，不放宽原边界。有效 WSL 红测后转绿，两文件 82 passed、8 skipped；Windows 原生修复后 70 passed、20 skipped（79.55 秒），包含真实 ACL 拒绝创建回归。最初红测夹具的 monkeypatch 曾误触 `os.supports_dir_fd`，纠正后才得到有效复现，不能把夹具错误记作产品红测。

第一次 0.6.0 构建为加入此修复，在 Analysis 阶段主动停止，未形成成品。上述自动回归不能代替新成品验证。

### 0.6.0 候选成品

新冻结来源 `7873f4d`，实际 exe 为 331,131,240 字节；三个模块内嵌元数据和随包说明均为 0.6.0。128 个应用代码模块及 327 项资源（12 静态、91 WSL 源码、224 公开资料）核对一致；实际仅 exe、默认根的离线自检 15/15。manifest 标记工作树干净，227 个文件哈希通过。第三方索引无 userinfo，包内没有实际 `.env`、会话、凭据或模型权重。

独立 exe 位于原生构建目录 `dist/ChannelAgent-0.6.0-windows-x64.exe`，旁有 `.sha256`。独立 exe、ZIP 内 exe、输入及仅 exe 自检副本的 SHA256 均为 `98392f3ed981c9b9907c3d9c5b55f61fc735c05f23983a1d7725a5ae8746e54e`。ZIP `ChannelAgent-0.6.0-windows-x64-20261113-024134.zip` 为 329,384,863 字节，228 项 CRC 与逐字节核对通过，SHA256 为 `42542a67501eccb5676393aad411d904af096f384bc68b5ffd513ce9c98c7167`。

真实 NTFS 只读负例使用两个实际 exe 进程：默认根退出 1（58.940 秒），显式只读根退出 1（58.476 秒），均报告 `unwritable` 且未初始化其他根；10/10 断言通过、无超时，finally 恢复 ACL 并以实际写入验证。报告位于 `portable-cli-readonly-060-20261113-62871b25`。这是 CLI 负例，不证明 GUI 回退选择已通过。

用户再次按 Esc 停止 Computer Use 后，已停止本轮所属 GUI 测试进程并恢复 ACL；没有继续桌面输入。可见 GUI 交互未完成，不能记为通过。随后在独立原生源码测试中，以离屏 Qt 和真实 NTFS 拒绝创建，驱动实际消息框按钮完成五项受控组件检查；手选目录仅替换系统选择器的返回值，取消及不安全现有配置仍拒绝且不修改原文。五项 9.50 秒通过，不能追认为实际 exe 可见交互通过。

正式发布由合并后的注解标签触发双平台回归、镜像及成品构建。发布后的实际状态与资产摘要以 [0.6.0 Release](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.0) 及对应工作流为准，不能把本机候选重命名成正式构建。

不依赖 .NET、外部 Windows Python、uv 或 Node 才能打开 GUI。修复版所选 PE 导入审计未发现缺失导入或 CLR，Python、Qt 与所需 VC 运行库随包提供；这不是干净虚拟机测试，不能据此宣称所有系统兼容。隔离命令仍可选依赖 WSL2 及 Linux 执行依赖，模型连接和本地检索权重由用户自行配置。

单文件与资源路径机制参照 [PyInstaller 运行模式](https://pyinstaller.org/en/stable/operating-mode.html)及[运行信息](https://pyinstaller.org/en/stable/runtime-information.html)。Windows VC 运行库的系统前提参照 [Microsoft 官方说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。
