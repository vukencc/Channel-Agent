> 历史报告，不作为当前使用规范；保留当时的问题描述和实测结果。当前说明见[当前调试与上下文指南](debug-runtime.md)。

# 上下文、执行与调试界面交付说明

## 原因与修复

模型网关的上下文容量不能从模型名称可靠推断。现在容量作为本地元数据，由 `MODEL_CONTEXT_TOKENS`、静态模型预设的 `context_tokens` 或运行时模型连接的 `context_tokens` 显式提供。`MODEL_RESPONSE_RESERVE_TOKENS` 默认 4096；实际回复预留取 `max(response_reserve_tokens, parameters.max_tokens)`，两值不相加。已知容量时，若没有显式 `max_tokens`，请求会发送有效回复预留作为 `max_tokens`；`MODEL_INPUT_TOKENS=0` 自动按容量扣除该预留，未知容量则使用 16000 token。`MODEL_INPUT_CHARS=0` 按输入预算的 4 倍计算，未知容量用 64000 字符。静态预设和运行时模型连接都可配置 `response_reserve_tokens`。配置的是声明值，不证明网关实际支持该窗口。

运行时模型管理界面可编辑容量与回复预留，发送给服务商的请求仍只包含模型采样参数。每轮开始时解析一次不可变运行时端点快照；上下文准备和后续模型请求共用该快照，避免估算窗口与请求端点在轮次中途不一致。运行时模型初始化或预算绑定失败会将任务标记为错误并显示安全提示，不会永久停留在 `queued`。上下文摘要应用后会刷新输入预算百分比。界面分别展示估算输入预算、声明容量、含回复预留的工作预算，以及服务商最近一次输入量，避免把本地估算当成服务商 usage。

`MODEL_FALLBACKS` 的每个备用项可选提供 `context_tokens` 与 `response_reserve_tokens`。已知容量时，备用请求也使用有效回复预留作为 `max_tokens`；未声明容量的备用项只接收不超过 16000 token 的实际公开输入。若请求对某备用项过大，程序跳过该端点并继续检查后续备用项；没有可容纳请求的端点时明确失败，不会静默截断输入。备用项不会继承主模型的容量声明。

长任务默认 `MAX_TOOL_ROUNDS=0`，即不设置固定模型交互次数上限；显式正数仍有效。子 Agent 的 `max_rounds=0` 继承父会话策略。默认无限轮数仍受取消、超时、费用与速率预算、模型恢复限制、工具限制及连续 16 轮无执行进展保护。旧配置 `AUTO_CONTINUE_TASKS` 与 `TASK_MAX_CONTINUATIONS` 只兼容正数轮数下的续阶段上限。

公开权限模式统一为完全访问（`full_access`）、智能申请（`smart`）和只读（`readonly`），默认 `smart`；headless 默认只读。已保存的 `standard`/`trusted` 旧会话继续采用严格确认逻辑，界面将其映射显示为智能申请，不会静默授予权限。

调试概览中过高的 Agent 轨道占用会挤压事件细节。每条 Agent 轨道现为 94 px，阶段标记和工具条更小并按阶段着色；通信链接可按需打开并可从事件或消息数定位。Agent 数量和共同时间尺度的事件密度趋势用于浏览事件分布，趋势只表示事件数量，不是 CPU 率或任务耗时。父 Agent 与运行状态也显示在概览中，既有详情检查器和敏感信息规则保持有效。

## 配置与边界

| 配置或字段 | 当前行为 |
| --- | --- |
| `MODEL_CONTEXT_TOKENS` | 默认 `0`，表示没有主模型容量声明；范围 512–2097152。 |
| `MODEL_RESPONSE_RESERVE_TOKENS` | 默认 `4096` token；有效回复预留还会考虑静态/运行时模型字段和 `MODEL_PARAMETERS.max_tokens`。 |
| `MODEL_INPUT_TOKENS` | 默认 `0` 自动计算；未知容量时保守使用 16000。 |
| `MODEL_INPUT_CHARS` | 默认 `0` 自动计算；未知容量时保守使用 64000 字符。 |
| `MAX_TOOL_ROUNDS` | 默认 `0` 表示不设固定模型轮数上限；正数设上限。 |
| `AUTO_CONTINUE_TASKS`、`TASK_MAX_CONTINUATIONS` | 仅兼容使用正数轮数上限的续阶段路径。 |
| 静态预设/运行时连接字段 | `context_tokens` 与 `response_reserve_tokens` 为本地预算元数据，不会作为模型参数发送。 |

容量声明是保守配置选择，不是对上游网关容量的验证。应用程序在启动时读取 `.env`，配置修改后须重启相应进程。采用未知容量时的保守窗口可以减少超限风险，但可能低估服务端支持的窗口；更改容量值不会探测服务商。环境预算、静态预设和运行时连接可能不同，应在各自的模型来源中核对。调试事件密度也不能替代性能或资源使用测量。

## 验收记录

上下文预算对照来自 `.cache/reports/context-budget-comparison.json`。本机声明容量为 100000 token、回复预留 4096；这只是保守声明，未验证网关实际提供的 1M 窗口。根 Agent 的实际输入估算为 12789 token，另为用户和搜索输入预留 2989 token：旧 16000/64000 本地预算显示 98.6125% 占用；新 95904 token 输入上限下，工作预算（实际输入加用户/搜索预留）占用为 16.4519%，实际输入估算相对声明容量为 12.789%。两个子 Agent 各自实际输入估算为 5259 token：旧预算显示 51.55%，新输入上限下工作预算显示 8.6003%，实际输入估算相对声明容量为 5.259%。工作预算分母已经扣除 4096 token 输出预留。这些是分母与展示口径调整后的预算百分比，实际输入 token 没有因此减少，也不能据此推断任务加速。

浏览器验收使用本地工具和注入模型回调，创建父 Agent 加两个子 Agent，执行 12 个受控轮次，记录 2815 个事件和 48 条通信消息。三条 Agent 轨道总高 282 px（每条 94 px，旧版总高 450 px）；背景仅绘制 3 个 SVG 矩形，标记更紧凑。工具批次内联展开两次调用时，时间线轨道总高为 378 px，检查器每页 20 行。筛选子 Agent 后保留父子两条轨道；点击消息可为所选子 Agent 显示 24 条原始消息对应的 19 条合并连线，以及 47 个发送/读取事件。模型管理的容量字段通过 128000-token 容量和 8192-token 回复预留的新增、编辑、保存及重载往返检查；权限快选与设置页均只列出三个公开模式。450 px 宽视口下页面外层宽度 435 px，没有页面级溢出；固定标签在横向平移前后均保持左侧 20 px。没有 JavaScript 异常，唯一的 favicon 404 与本次修改无关。截图：[桌面](../.cache/reports/debug-trends-desktop-final.png)、[展开工具调用](../.cache/reports/debug-trends-expanded-final.png)、[移动视口](../.cache/reports/debug-trends-mobile-final.png)。

该浏览器场景不调用收费模型，不代表真实模型质量，也不测量 CPU 利用率、实际输入 token 节省或任务耗时。

持续执行回归另以受控模型回调连续完成 121 次模型交互和 120 次真实文件读取，跨越旧版 96 轮上限，保留同一条用户请求并正常结束。加强该用例后，`uv run pytest tests/test_unlimited_execution.py -q` 为 13 passed（0.97 秒）；取消、费用、无进展、空回复和显式正数上限的断言仍保留。

本轮验证使用 `uv sync --locked` 和 `uv sync --locked --extra web`；Bubblewrap + prlimit 沙箱探针通过。最终回归命令 `UV_NO_SYNC=1 uv run pytest -m 'not integration' -q -rs --tb=short` 结果为 1177 passed、2 skipped（未安装可选 hnswlib）、5 deselected（外部集成测试），耗时 38.62 秒。`config.validate_runtime_config()` 对当前运行配置校验通过。测试夹具显式固定 `MODEL_CONTEXT_TOKENS=0`、`MODEL_INPUT_TOKENS=16000` 和 `MODEL_INPUT_CHARS=64000`，避免本机 `.env` 覆盖测试默认预算；该测试隔离修复后回归通过。受控浏览器 fixture 已关闭，其临时文件已清理；用户会话、工作区与 `.env` 未修改。没有新增依赖；`git diff --check` 通过。回归测试使用受控模型边界，不表示付费服务的模型能力或服务窗口验证。
