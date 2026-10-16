> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前调试与上下文指南](debug-runtime.md)。

# 博客故障修复与真实模型多 Agent 验证

## 范围与结论

本报告分开记录原始博客任务、依赖安装复验和后续 WorkBoard 多 Agent 验证。原始博客会话没有创建子 Agent：当时 `ENABLE_AGENT_TASKS` 为关闭状态。WorkBoard 是另一个独立实验，才启用了子 Agent。原始会话没有完成博客；WorkBoard 第一轮也因子 Agent 轮数上限停在检查点，不能把它写成博客任务完成或完整验收通过。

依赖修复后的 npm 安装和生产构建通过。WorkBoard 第一轮因子 Agent B 进入 `tool_budget` 检查点而正确报告 `runtime_completed=false`；第二轮从全新运行开始，父子 Agent 均完成，真实模型及独立验收数据见下文。模型工具层已针对无限轮数父会话收紧子 Agent 的 `max_rounds` 参数。

## 原始博客会话

真实博客会话 ID 为 `8a50290d28914b8d82c910b92b8cc224`。该会话的最新完整运行包含 7 次服务商模型调用，耗时 303.168 秒，累计输入 442,958 tokens、输出 2,533 tokens。运行没有发生 context compact；单次输入大致在 62k–65k tokens 区间，约为所用上下文容量的 68%。当时 `ENABLE_AGENT_TASKS=false`，因此该次运行没有子 Agent。

执行在两次连续的非行动回复后触发 `no_execution_progress` 停止保护。会话仍留有未完成计划，因此结论是博客任务未完成。该保护阻止模型重复输出没有带来文件或验证进展的回复；它不代表博客已写成或验收。

## 安装故障与修复验证

| 阶段 | 结果 | 证据 |
| --- | --- | --- |
| 修复前 | 同一 Vue 3.4 / Vite 5.2 / `@vitejs/plugin-vue` 5.0.4 依赖集合安装失败；npm 输出 `vite` 版本 `undefined` 的 `ERESOLVE`，并在约 4.219 秒触发 8 MiB 网络字节限额。 | 两个累计连接合计传输 8,384,254 字节；依赖未准备完成。 |
| 修复后 | 使用隔离的显式安装档位，未改变依赖版本，也未放宽依赖解析，安装和生产构建均成功。 | 62 个包安装用时 15.94 秒；生产构建用时 673 毫秒；合计 17.184 秒。累计两个连接传输 28,341,223 字节。`dist/index.html` 与构建 bundle 已生成。 |

此前的 `ERESOLVE` 与 `vite undefined` 是网络额度耗尽时出现的次级诊断，不能单独作为依赖版本冲突证据。`EFBIG` 对应单文件大小限额。联网拒绝会给出固定错误码、原因及已用/允许额度；连接数和隧道超时触顶也会返回诊断。这里的连接数是单次命令生命周期内累计打开的 CONNECT 连接数，不是并发连接数；超时是已建立 CONNECT 隧道的绝对生命周期上限，不是每个 HTTP 请求的时限。安装成功和产物存在只证明依赖安装及生产构建通过。

本次安装权限计划记录的有效档位为网络 128 MiB、32 个累计连接、每条隧道 30 秒、单文件 128 MiB、CPU 30 秒。`ENVIRONMENT_PREPARE_*` 五个资源值设为 `0` 时会继承通用命令限额；此次安装显式使用独立额度。联网白名单、公网地址校验、数值地址固定、确认和审计仍生效。没有使用 `--force` 或 `--legacy-peer-deps`。

## WorkBoard 第一轮真实模型验证

这是与博客会话分开的隔离实验。该实验显式启用了 Agent Tasks、session budgets 和环境准备，并使用 `full_access` 权限策略；自动确认没有绕过沙箱、资源限制或审计。

父 Agent 记录 65 次模型调用，子 Agent A 记录 25 次，子 Agent B 记录 40 次，总计 130 次。父 Agent 完成 9 个计划步骤；子 Agent A 完成，子 Agent B 因父 Agent 的 40 轮上限进入 `tool_budget` 检查点。该状态是执行额度结果，不是模型 API 故障。父 Agent 第 24/65 次模型调用以 `finish_reason=length` 结束：输入 30,323 tokens，输出达到 4,096 token 上限，工具参数 17,405 字符，耗时 23.02 秒；没有 provider exception，未完成的调用没有执行工具。随后的 40 次模型调用成功恢复，父 Agent 最终进入 idle。全程未发生 Compact，父 Agent 峰值上下文利用率为 72.7%。

运行发送 2 条消息并读取消息 3 次：一条由子 Agent A 发给父 Agent，另一条由父 Agent 发给子 Agent B。因此记录里两个方向都有消息，但不是同一对子 Agent 的派发与回传。由于子 Agent B 停在检查点，运行报告的 `runtime_completed=false` 符合记录状态，不应描述成监控器误报。

第一轮独立验收中，项目自身测试为 82 passed；外部验收 32 项中，`test_bad_import`、`test_bad_project` 和 `test_dependency_missing` 以异常结束。`ImportErrorWorkBoard` 与 `NotFoundError` 没有按原始契约继承 `ValueError`。第二轮由真实模型任务按原始契约修复后，在全新运行中通过 64 项项目测试和 32 项外部验收；项目自身测试包括 `test_error_hierarchy_is_valueerror`、`test_all_errors_are_valueerror` 和 `test_raised_errors_are_instances_of_valueerror`，三项均通过。没有手工改写实验产物。

## 修复边界与第二轮

无限轮数父会话的模型可见 `create_session` / `delegate` schema 现在只允许 `max_rounds=0`；正数调用会在权限确认前拒绝。有限轮数父会话仍可使用不超过父上限的正数，`0` 则继承父会话有效上限。模型恢复额度在每一轮模型调用开始时重新计数。命令工具以真实退出码判定结果，非零退出会标为失败，不计为执行进展。

网络拒绝以固定原因码和已用/上限额度进入审计；相同原因去重，最多记录 8 类，避免反复重试耗尽额度时写入无界审计记录。公网单播白名单、DNS 数值地址固定、确认与沙箱边界继续生效。

## 实现与回归对应

| 根因或需求 | 实现 | 回归证据 |
| --- | --- | --- |
| 安装任务需要可确认、可审计的独立网络/CPU/文件额度；累计连接、绝对隧道时限及拒绝原因需透明。 | `src/ai_agent_startup/tools/network_proxy.py`、`src/ai_agent_startup/tools/environment.py`、`src/ai_agent_startup/tools/command.py`、`src/ai_agent_startup/tools/command_jobs.py`、`src/ai_agent_startup/core/command_jobs.py`、`src/ai_agent_startup/tools/sandbox.py`、`src/ai_agent_startup/config.py`。 | 资源档位回归：`tests/test_install_resource_profile.py::test_prepare_approval_and_execution_use_same_explicit_limits`、`test_prepare_zero_profile_inherits_command_limits`、`test_full_access_still_audits_exact_approved_prepare_profile`；额度诊断回归：`tests/test_network_quota_diagnostics.py::test_lifetime_connection_rejection_is_visible_and_audited`、`test_tunnel_byte_and_absolute_timeout_denials_have_fixed_evidence`、`test_background_job_persists_gateway_diagnostics`。 |
| 非零命令退出不得计为执行进展；恢复额度应按每个完整模型请求重置。 | `src/ai_agent_startup/core/sessions.py`。 | `tests/test_execution_progress_outcomes.py`：`test_failed_real_command_is_not_successful_execution_progress`、`test_recovery_allowance_resets_after_completed_model_round`。 |
| 无限轮数父会话不能让模型为子 Agent 选择正数上限；有限轮数父会话仍能声明有界上限。 | `src/ai_agent_startup/core/tool_settings.py`、`src/ai_agent_startup/tools/agent_tasks.py`、`src/ai_agent_startup/tools/session_tools.py`。 | `tests/test_delegation_unlimited_contract.py`：无限父 schema/调用拒绝和有限父 schema；另有 `tests/test_unlimited_execution.py`、`tests/test_free11_agent_tasks.py`。 |
| 时间线需区分主线阶段/结果与工具行为，并如实显示聚合事件、错误计数、空闲压缩及活动动画。 | `src/ai_agent_startup/web/static/debug.js`、`debug.css`、`debug-view.js`、`debug.html`。 | `tests/test_debug_timeline_arrows.py` 覆盖异常标签与合并活动计数、方向箭头、空闲压缩和动画状态；最终 UI 50 项 focused tests 通过。 |
| 真实模型任务需保留运行状态、provider usage、工具进展、消息和 trace 证据，避免重复计数。 | `scripts/real_multiagent_validation.py`。 | `tests/test_real_multiagent_validation.py` 覆盖隔离目录、安全限制、用量聚合、去重和通信/压缩统计；两轮独立真实模型验收记录已归档。 |


## WorkBoard 第二轮全新真实模型验证

第二轮在隔离根 `agent-real-multi-validation-a0d7ff996d8e4a37abc069e619c40bae` 中从新运行开始，没有复用第一轮生成代码，也没有人工修改产物。单次提交后运行 740.174 秒，`runtime_completed=true`、`timed_out=false`；父 Agent 与两个子 Agent 均以 `finished` 结束，没有人工续跑。 执行器与独立监督器均使用本机配置的 `deepseek-v4.1-flash`，经现有兼容网关调用；未记录或公开网关 URL 与凭据。模型预算配置为 context 100,000 tokens、response reserve 4,096 tokens、输入预算 95,904 tokens。

| Agent | 模型调用 | 工具调用 | 轮数上限 |
| --- | ---: | ---: | --- |
| 父 Agent | 54 | 80 | `null`，有效 `MAX_TOOL_ROUNDS=0` |
| 子 Agent A | 23 | 25 | `null`，有效 `MAX_TOOL_ROUNDS=0` |
| 子 Agent B | 48 | 53 | `null`，有效 `MAX_TOOL_ROUNDS=0` |
| 合计 | 125 | 158 | 全部无限轮数 |

125 次模型调用均为 provider usage、outcome `ok`；跨请求累计输入 4,234,368 tokens（含请求间重复前缀），输出累计 57,805 tokens，没有 API 错误。累计输入量不能与单次窗口占用直接比较；本轮父 Agent 瞬时上下文峰值为 64.4895%。价格未配置，125 次调用的费用状态均为 unknown，不报告为零费用。158 次工具调用中 152 次完成、5 次错误、1 次拒绝。错误和恢复过程保留在记录中：子 Agent A 有两次追加超限后拆分继续；父 Agent 有两次计划状态/依赖错误；子 Agent B 有一次命令非零退出后修复；一次大文件覆盖被拒绝后改用局部编辑完成。6 条消息发送、9 次读取；父 Agent 分别向两个子 Agent 派发任务并收到回传，满足双向交接。计划记录 8 步完成，但计划自身的 verification 标记为 `unverified`，因此单独记为未验证；独立验收结果见下文。

运行中上下文峰值为父 Agent 64.4895%、子 Agent A 42.2641%、子 Agent B 56.8412%；没有触发 Compact，也没有监督器调用，均符合低于 90% 软阈值时不评分/压缩的策略。DEBUG trace 共 4,460 条，丢弃 0 条。运行结束后父子 Agent 均为空闲完成状态，活动流线与游标停止。浏览器实际显示 3 条轨道、462 px 总高和 6 条通信连线。

独立验收中，项目自身 64 项测试通过，用时 6.471 秒；测试进程出现 HTTPError 资源清理 `ResourceWarning`，不是测试失败。外部固定验收 32 项，0 fail、0 error。此次验证报告的是这些实际结果，不以计划中的 `unverified` 自评替代独立测试。

## 监督器 thinking 修复后的真实 history 压力复测

同一真实 history 在 `SUPERVISOR_THINKING_MODE=disabled` 下复测，三条路径结果如下：

| 路径 | 输入利用率 | 监督与压缩结果 |
| --- | ---: | --- |
| normal | 63.3321% | 无监督调用、无压缩。 |
| soft | 97.4006% | 监督器生成 22 项评分；方差 0.0093492263，低于 0.75 阈值。结果为 `evaluated`，不压缩；inspect 用时 3.304 秒。 |
| hard | 109.9681% | 硬预算触发一次压缩，归档 26 块，利用率降至 12.3690%。一页模型摘要成功，另一页返回无效 JSON，第三页因共享 20 秒截止时间取消，最终使用 `extractive_fallback`。 |

对同一 history 紧接着重试时，0 次压缩、0 次监督调用。原始消息仍保存在不可变本地归档中；summary 部分失败由有界摘录安全回退，不会触发无界重试。只有 soft 场景的监督评分成功不能推导所有 hard 摘要页都成功。

## 最终验证

本次隔离实验启用的开关与安装限额如下；模板默认值来自 `.env.example`，实际档位来自本机安装权限计划。`0` 表示继承通用命令限额。

| 配置 | 模板默认值 | 本次实验值 |
| --- | --- | --- |
| `ENABLE_ENVIRONMENT_PREPARE` | `false` | `true` |
| `ENABLE_AGENT_TASKS` | `false` | `true` |
| `ENABLE_SESSION_BUDGETS` | `false` | `true` |
| `ENVIRONMENT_PREPARE_NETWORK_BYTES` | `0` | `134217728`（128 MiB） |
| `ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS` | `0` | `32` 个生命周期累计连接 |
| `ENVIRONMENT_PREPARE_NETWORK_TIMEOUT` | `0` | `30` 秒已建立隧道生命周期 |
| `ENVIRONMENT_PREPARE_FILE_MB` | `0` | `128` MiB |
| `ENVIRONMENT_PREPARE_CPU_SECONDS` | `0` | `30` 秒 |

通用联网仍执行 allowlist、公网单播校验、数值地址固定、权限确认与审计。 `.env` 或 `.env.supervisor` 配置变更后，重启对应 Agent/WebUI 进程使其生效。

最终 UI 验收覆盖 1500 px 桌面宽度与 450 px 窄屏，均无横向溢出；三条 Agent 轨道共 462 px，筛选父子轨道为 308 px。1,835 个事件可分页（1–20、21–40），按需详情 API 返回 1,099 字符；筛选 Agent 后保留父轨通信线，6 条通信连线可见。执行时 active flow/cursor 动画运行，暂停或系统减少动态效果时停止；全部 Agent 完成后无活动线。浏览器控制台 0 warning、0 error。计数文案已修正，分别显示异常/拒绝事件条数和合并后的总事件数。50 项 focused UI tests 通过，用时 2.24 秒。最终截图：[完整调试时间线](../.cache/reports/blog-debug-real-model-20261015-b7e2/debug-timeline-complete.png)；测量见 [browser-validation.json](../.cache/reports/blog-debug-real-model-20261015-b7e2/browser-validation.json)。

最终非集成回归为 1,224 passed、2 skipped（`hnswlib` ANN 可选依赖未安装）、5 deselected，43.03 秒。Bubblewrap + prlimit 真实沙箱探针通过；26 个归档块的 `RawHistory` 连同元数据均与真实来源逐项相符；校验比较原文与来源记录，不是 `_public` 脱敏投影。Java 和 Maven 未安装，Spring 构建没有运行，不计为通过。


## 真实监督器 thinking 诊断

使用 WorkBoard 第一轮的真实父会话 history 做相同输入 A/B，保留了原始网关请求头。最初一组不带网关会话 header 的手工 client 请求返回 HTTP 400；它不代表产品路径，也不纳入结论。有效 A/B 对照均使用应用的真实 header 和服务商端点。

`SUPERVISOR_THINKING_MODE=auto`（默认）不发送 thinking 扩展参数。此配置下，inspect 与 summary 都以 `finish_reason=length` 结束，输出预算 1200 tokens 全部计入 `reasoning_tokens`，可见正文字符数为 0；耗时分别为 12.404 秒和 11.975 秒。同一 snapshot/history 显式设置 `SUPERVISOR_THINKING_MODE=disabled` 后，inspect 以 `stop` 结束，219 tokens、0 reasoning tokens、5.356 秒，产出 22 个有效评分；summary 以 `stop` 结束，765 tokens、0 reasoning tokens、6.692 秒，生成 1,786 字符摘要。

这表明该监督模型与端点在本次小额度 JSON 请求中需要显式关闭 thinking，才能将输出额度用于结构化结果。仅对本机忽略的 `.env.supervisor` 配置做了 `SUPERVISOR_THINKING_MODE=disabled` 调整，凭据未改；仓库默认 `auto` 保持不变。样例与配置文档建议需要严格 JSON 且额度较小时针对目标端点实测并显式选择 `disabled`，不能据此推断其他模型都应关闭 thinking。完整 A/B 证据见下方[证据目录](#证据与复核)。

## 调试时间线

WebUI 每条 Agent 轨道高 154 px：阶段与结果在主时间线上方，工具行为在主线下方；每条主/子 Agent 轨道都保留各自事件。只压缩可由停止与后续开始事件证明的全局空闲段，并显示真实时长；模型或工具执行中的等待不折叠。详情仍需显式选择后读取。

## 证据与复核

本报告汇总证据位于 [2026-10-15 博客与 WorkBoard 验收证据目录](../.cache/reports/blog-debug-real-model-20261015-b7e2/)。其中包括 [第一轮记录](../.cache/reports/blog-debug-real-model-20261015-b7e2/experiment1.json) 与其 [外部验收](../.cache/reports/blog-debug-real-model-20261015-b7e2/experiment1-external-acceptance.json)、[第二轮记录](../.cache/reports/blog-debug-real-model-20261015-b7e2/experiment2.json) 与其 [外部验收](../.cache/reports/blog-debug-real-model-20261015-b7e2/experiment2-external-acceptance.json)、[安装前](../.cache/reports/blog-debug-real-model-20261015-b7e2/install-before.json)/[安装后](../.cache/reports/blog-debug-real-model-20261015-b7e2/install-after.json)、[监督器 thinking A/B](../.cache/reports/blog-debug-real-model-20261015-b7e2/supervisor-thinking-ab.json)、[上下文压力修复前](../.cache/reports/blog-debug-real-model-20261015-b7e2/context-pressure-before.json)/[修复后](../.cache/reports/blog-debug-real-model-20261015-b7e2/context-pressure-after.json)及[证据清单](../.cache/reports/blog-debug-real-model-20261015-b7e2/evidence-manifest.json)。证据目录不包含原始 session 或凭据。

本次同步的文档和示例配置包括 `.env.example`、`.env.supervisor.example`、`README.md`、`docs/README.md`、`docs/configuration.md`、`docs/environment-preparation.md`、`docs/debug-runtime.md`、`docs/debug-timeline-design.md`、`docs/context-management.md` 和本文。真实多 Agent 实验由[隔离验证脚本](../scripts/real_multiagent_validation.py)驱动。最终非集成回归命令 `uv run pytest -m 'not integration' -q -rs --tb=short` 的结果为 1,224 passed、2 skipped、5 deselected，用时 43.03 秒；对应输出见 [repository-pytest.txt](../.cache/reports/blog-debug-real-model-20261015-b7e2/repository-pytest.txt)。50 项 focused UI tests 另以 2.24 秒通过。沙箱验证命令 `uv run python dev/ci_sandbox_probe.py` 以退出码 0 通过 Bubblewrap + prlimit 实际隔离，见 [sandbox-probe.txt](../.cache/reports/blog-debug-real-model-20261015-b7e2/sandbox-probe.txt)。项目内验证命令 `python3 -m unittest discover -s tests -v` 通过 64 项，用时 6.471 秒；外部固定验收 32 项，0 fail、0 error。依赖同步命令 `uv sync --locked` 与 `uv sync --locked --extra web` 均以 88 resolved 成功；前一命令在临时 base 环境移除 4 个已有 web 包，后一命令恢复，依赖文件未改、没有增加可选依赖。

26 个归档块及 metadata/source 一致性由 [archive-integrity.json](../.cache/reports/blog-debug-real-model-20261015-b7e2/archive-integrity.json) 记录；最终浏览器截图未使用修正文案前的旧截图。配置键与默认值已核对 `.env.example`、`.env.supervisor.example`、`src/ai_agent_startup/config.py` 和 `src/ai_agent_startup/core/context_supervisor.py`；联网行为已核对 `src/ai_agent_startup/tools/network_proxy.py`。
配置和网络资源说明见[隔离命令环境准备](environment-preparation.md)；调试字段和时间线行为见[Agent 运行调试](debug-runtime.md)及[DEBUG 因果时间线](debug-timeline-design.md)。
