# Windows 便携单文件交付记录（2026-11-13）

> 文档类型：本轮验证记录 · [Windows 使用指南](windows.md) · [维护交接](session-handoff.md)

本轮将 Windows 冻结入口改为默认使用实际 exe 所在目录，而不是开发 checkout、当前工作目录或旧版用户目录。目标为 Windows 10/11 x64；Linux 与源码运行的默认布局保持。技术栈为 CPython 3.12、PySide6/Qt、现有 AgentCore、FastAPI/Web 资源及 CPU 检索依赖，采用 PyInstaller onefile，保留显式 onedir 选项。

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
| Linux 完整回归 | 最新修复后 2476 passed、49 skipped、5 deselected（187.10 秒），独立网络命名空间、临时数据路径，未改变测试断言；先前轮为 2476 passed、46 skipped（185.79 秒） |
| 双平台 CI | [43348181678](https://github.com/vukencc/Channel-Agent/actions/runs/43348181678)，来源 `9fa4e7f`，Windows 945 passed、48 skipped，两任务通过；此前 `ac200c5` 的 [43344439705](https://github.com/vukencc/Channel-Agent/actions/runs/43344439705) 也通过 |
| 只读审查 | 最终实现无新增确定缺陷；原先两项审查发现已有红测及修复 |
| 实际单 exe | 修复版构建自检 15/15；默认根首启、重复保留配置和中文空格路径搬移通过。显式根、只读回退与可见 GUI 的最终结果待补 |

Linux 首轮完整回归为 3 failed、2464 passed、46 skipped、5 deselected（232.13 秒）。一个失败来自配置模板漏列两个新设置，已补齐并用原配置覆盖断言验证；另一个是测试更新期间收集的旧 bare-key 夹具，新专项已按 dotenv 真正语法错误验证。第三个旧代理用例期望连接 `127.0.0.1:1` 立即被拒，但本机 WSL 该端口实际超时。原用例隔离重跑仍失败；相同原用例在独立网络命名空间 1 passed（0.11 秒），直接 socket 对照分别为超时和 `ECONNREFUSED`。未改代理实现、宿主网络、时限或原断言，保留原环境失败。

第一次命名空间全量实验将整个文件系统设为只读，但遗漏 CLI 的默认工作区和 `/var/tmp`：5 failed、2471 passed、46 skipped、5 deselected（186.35 秒），均为测试目录不可写。随后显式设置 `SANDBOX_DIR`、`AGENT_STATE_DIR`、审计、RAG 到临时路径，并提供临时 `/var/tmp`，完整通过。测试使用锁定同步后的环境，原命令为 `uv run --no-sync pytest -m 'not integration' -q`，附加 `-p no:cacheprovider` 避免向只读源码目录写 pytest 缓存。最初宿主全量可能创建随机测试工作区；临时状态已被 pytest retention 清理，无法安全追溯，故未按空目录猜测删除任何既有工作区。

## 实际成品与依赖

第一份真实 onefile 来源 `ac200c5`，331,131,146 字节，构建离线自检 15/15。独立审计确认 128 应用模块、12 静态文件、91 WSL Core 源码及 224 份公开资料一致，无 `_internal` 或外部归档依赖；未嵌入实际 `.env`、会话、项目或凭据。完整 ZIP 和清单校验通过，但**此包不能作为首启验收通过的交付**。

仅复制该 exe 到普通可写目录后，首次 `--portable-info` 运行 37.153 秒，退出 1，仅留下 `.portable.lock`、没有初始化目录或报告。原始失败保存在本轮独立报告目录。独立 tiny console 冻结程序与 Native Python 都定位到 `protect_private_path`：新文件已经归当前用户所有，继承 ACL 允许 Modify，但没有 WRITE_OWNER；函数强制重复设置 owner 与私有 DACL，返回 WinError 5。同一新文件仅设置受保护 DACL 成功，重复 owner 或 owner+DACL 失败。构建自检使用的私有临时目录具有更充分的继承权限，不能覆盖这个普通目录场景。

真实 Windows 新回归先为 3 failed（4.17 秒）。`da6b22b` 在安全句柄持有期间查询 owner：已是当前用户则只设置受保护 DACL，否则保留原单次 owner+DACL 及权限要求，操作后再核验 owner。无查询失败降级、无先提权再改 owner 的两阶段处理，已有非私有 `.env` 仍拒绝且不修改。修复后 Windows 相关专项 104 passed、21 skipped（87.43 秒），包含三项新 ACL 用例。独立安全审查无新增发现。

`077bb74` 进一步调整构建验收：先复制单 exe 到独立目录，移除预设的数据根及路径变量，从无关工作目录执行原完整离线自检并断言 exe 旁目录确实生成；保留 600 秒原时限、15 项能力及必需依赖，无需安装模型权重。没有把源目录包或指定根的通过追认为默认便携首启成功。

`077bb74` 的 [CI 43347227648](https://github.com/vukencc/Channel-Agent/actions/runs/43347227648) Windows 为 942 passed、48 skipped、3 errors：管理员运行器的默认 token owner 是 Administrators，测试夹具不能假定新文件已归 token user。`9fa4e7f` 仅修复夹具：复制线程令牌，将副本的默认 owner 设为当前用户并禁用副本特权，结束时恢复原线程身份，保留 OWNER 重写必须被拒绝等断言；没有修改原进程令牌、系统安全设置或产品权限。Native 3 passed（4.33 秒），管理员 CI 随后通过。

修复版 `ChannelAgent.exe` 为 331,131,590 字节，构建自检从仅含 exe 的默认根运行，离线 15/15；128 应用模块一致。实际复制单 exe 的首次启动、重复保留配置与中文空格路径搬移通过。显式根、只读回退和可见 GUI 在中断前未完成，不能记为通过；最终成品来源、SHA256 与后续结果待补。此前 onedir 成品及第一份 onefile 的构建通过均不能代替它们。

不依赖 .NET、外部 Windows Python、uv 或 Node 才能打开 GUI。修复版所选 PE 导入审计未发现缺失导入或 CLR，Python、Qt 与所需 VC 运行库随包提供；这不是干净虚拟机测试，不能据此宣称所有系统兼容。隔离命令仍可选依赖 WSL2 及 Linux 执行依赖，模型连接和本地检索权重由用户自行配置。

单文件与资源路径机制参照 [PyInstaller 运行模式](https://pyinstaller.org/en/stable/operating-mode.html)及[运行信息](https://pyinstaller.org/en/stable/runtime-information.html)。Windows VC 运行库的系统前提参照 [Microsoft 官方说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)。
