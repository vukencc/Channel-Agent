# CLI 与会话工具交付记录（2026-09-29）

本轮加入会话创建和父子 Agent 持久通信，并降低长历史浏览对 CLI 事件循环的阻塞。新会话入口（CLI 初次启动、`/new`、headless 新建）各调用已注册的 `create_session` 一次，不额外请求模型；模型通过同一工具创建子 Agent 时仍须用户确认。

## 实现与原因

原先的新会话入口绕开注册工具，模型无法沿用同一受控入口创建独立子 Agent；父子 Agent 也没有持久通信信箱。CLI 在长历史页面整理时同步处理大量消息，会占住 UI 事件循环，延迟状态刷新和键盘响应。控制类会话工具还可能从工具线程等待事件循环任务，而该任务再次等待已被占用的默认线程池，形成线程池耗尽死锁。

现在 `create_session`、`send_session_message`、`read_session_messages` 默认注册，不依赖 `ENABLE_AGENT_TASKS`。子会话使用独立上下文与文件工作区，继承父权限、预算所有者和工具子集；创建要求逐次确认，readonly 会拒绝，子会话不能递归创建。`ENABLE_AGENT_TASKS` 仅控制旧版 `delegate` 注册和启动时任务队列预载；`/tasks` 可读取按需创建的队列，没有已加载子任务时会提示可使用 `create_session`。

父子通信只允许直接关系，发送者身份取自运行上下文。消息写入持久信箱，空闲收件会话不会被唤醒；每个模型轮次自动读取最多一条，并在工具调用及其结果完整配对后作为带来源标记的参考消息注入。它不是用户审批，也不会自动重放工具。显式 `read_session_messages` 默认每页 20 条、最多 100 条，响应总字符数受 `TOOL_MAX_OUTPUT` 限制，故一页可能少于请求条数；消息不会截断或跳过，`next_seq` 指向本页最后一条已返回消息。若单条超过上限，工具返回可恢复错误并保留信件，调高 `TOOL_MAX_OUTPUT` 后可重试。单条消息和单个信箱分别受 `SESSION_MESSAGE_MAX_CHARS=4000` 与 `SESSION_INBOX_MAX_BYTES=1048576` 限制，数值须为正整数；写满时拒绝新消息并保留历史，不自动清理。审计只记录消息元数据。

CLI 的历史页面整理移到后台线程，惰性读取长记录；状态刷新合并，切换会话时保存草稿。控制工具初始化改走专用写线程，避免嵌套等待默认线程池。内部信件带有来源标记，在 `/retry`、`/resend`、记忆查询、上下文裁剪与摘要选轮时不会被当成真实用户请求；模型接口请求会移除该本地标记，CLI 历史则显示发送者来源。实现细节与交互限制见 [CLI 指南](cli.md) 和 [会话工具说明](session-tools.md)。

涉及实现文件：`src/ai_agent_startup/core/session_service.py`、`src/ai_agent_startup/core/communication.py`、`src/ai_agent_startup/core/sessions.py`、`src/ai_agent_startup/core/headless.py`、`src/ai_agent_startup/core/cli.py`、`src/ai_agent_startup/core/transcript.py`、`src/ai_agent_startup/core/agent_tasks.py`、`src/ai_agent_startup/core/session_limits.py`、`src/ai_agent_startup/tools/session_tools.py`、`src/ai_agent_startup/tools/__init__.py`；配置定义在 `src/ai_agent_startup/config.py`，示例值见 [`.env.example`](../.env.example)。

## 验证

- `uv sync --locked` 成功；本轮未更改依赖声明或 `uv.lock`。
- `uv run pytest -m 'not integration' -q`：最终 **347 passed、2 skipped、5 deselected，11.58 秒**。两项跳过需要显式启用本地 RAG 模型；五项集成测试未运行。测试数据、审计和缓存重定向到 `/tmp/agent-cli-validation/`。
- `uv run pytest tests/test_session_tool_pool.py -q`：两个控制工具死锁回归用例在修复前均失败、修复后均通过。会话工具、消息通道与恢复行为测试使用模型 stub 验证，无真实 LLM 请求。
- `timeout 25s .venv/bin/python -m pytest tests/test_session_message_retry.py -q`：5 passed；`timeout 25s .venv/bin/python -m pytest tests/test_inbox_batch_limit.py -q`：3 passed。后者覆盖每轮单条自动注入、受输出上限约束的无跳号分页，以及小工具输出预算不会阻塞内部收信和历史显示。
- `uv run python dev/ci_sandbox_probe.py` 在受限沙箱内因缺少 `NETLINK_ROUTE` 权限无法完成 Bubblewrap 探测；获准在沙箱外执行后通过。
- `timeout 60s .venv/bin/python dev/perf/benchmark_cli.py --baseline-ref v0.2.1 --output /tmp/cli-benchmark.json` 和 `timeout 60s .venv/bin/python dev/perf/benchmark_cli.py --baseline-ref v0.2.1 --reverse-order --output /tmp/cli-benchmark-reverse.json` 均完成。参数已对照脚本 argparse 定义核实。
- 未运行真实模型诊断；本报告不据此推断服务端模型速度或真实终端帧率。

受限执行环境还会阻断异步工作线程的事件循环唤醒，导致最小异步探针及部分 pytest 超时；上述最终异步回归和性能测量均在获准的沙箱外环境执行。没有将环境超时记作通过，也没有为此放宽应用沙箱。

| 新增测试文件 | 核心覆盖 |
|---|---|
| `tests/test_cli_async_transcript.py` | 长历史不阻塞输入、流式增量缓存、切换丢弃旧页面、草稿保留、状态区别 |
| `tests/test_session_tools_channels.py` | 注册工具单次启动、确认拒绝、权限与预算继承、父子双向持久通信、配对、取消及重启 |
| `tests/test_session_tool_pool.py` | 创建和发送工具在默认线程池饱和时仍能完成 |
| `tests/test_session_message_retry.py` | 真人任务与 Agent 来信区分、上下文裁剪、模型请求字段、来信标签 |
| `tests/test_inbox_batch_limit.py` | 有界自动投递、完整 JSON 分页及内部投递与工具响应预算隔离 |

## 配置与使用边界

| 新增配置 | 默认值 | 含义 |
|---|---:|---|
| `SESSION_MESSAGE_MAX_CHARS` | 4000 | 单条 Agent 信件字符上限 |
| `SESSION_INBOX_MAX_BYTES` | 1048576 | 单个持久信箱 UTF-8 字节上限，满时拒绝、不删除历史 |

没有新增模型、可选依赖或依赖声明。创建子 Agent 始终要求确认并审计；已建立父子关系后的内部通信记录审计，不另作工作区写操作确认。信箱位于 `<state-dir>/<session-id>/inbox.json`。空闲 Agent 不会因收信启动模型，可切换到该会话继续；工具读信不推进自动投递游标，因此工具读取和后续自动投递可能展示同一序号。

## 合成 CLI 基准

`dev/perf/benchmark_cli.py` 使用固定 tag `v0.2.1`（解析到 `7bf13592c6581856e66501db7435a87cae11b543`）中的历史 `AgentCLI` 类与工作树版本对比。它从 tag 载入旧 CLI 类，但复用当前 `SessionManager` 和 `Transcript`，不是两个完整版本依赖树的端到端对比。测试使用临时状态目录、`DummyOutput`、`PipeInput` 和 50,001 条共 1,250,034 字符的合成历史；每项 1 次预热、9 次计时，0 次模型调用。原始 JSON 保存在 `/tmp/cli-benchmark.json` 与 `/tmp/cli-benchmark-reverse.json`，未纳入仓库。

页面完成时间使用中位数；心跳行使用实际最大间隔。

| 次序与指标 | 历史 CLI | 当前 CLI |
|---|---:|---:|
| 正序：首次完整页面完成 | 36.732 ms | 40.749 ms |
| 正序：切换会话完成 | 43.054 ms | 39.743 ms |
| 正序：连续切换完成 | 39.737 ms | 37.730 ms |
| 正序：切换时 5 ms 心跳最大间隔 | 121.969 ms | 11.420 ms |
| 反序：首次完整页面完成 | 56.815 ms | 66.122 ms |
| 反序：切换会话完成 | 74.514 ms | 64.303 ms |
| 反序：连续切换完成 | 66.747 ms | 62.707 ms |
| 反序：切换时 5 ms 心跳最大间隔 | 201.998 ms | 15.570 ms |

首次页面完成在两组顺序中都略慢，显示整理被移出事件循环需要额外调度；切换期间事件循环心跳最大间隔缩短。该合成基准说明长历史处理对本地 UI 事件循环的影响，不衡量模型生成速度、真实终端帧率或一般硬件上的绝对耗时。
