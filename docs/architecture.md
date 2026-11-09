# 三模块架构

> 文档类型：开发与设计参考 · [文档导航](README.md) · [三模块架构](architecture.md)

仓库由一个 uv workspace 管理三个可独立构建的 Python 包，版本和锁文件集中维护。命令仍从仓库根运行，已有 `.env`、`.agent/`、`crud_tests/`、日志和知识库位置不变。

新用户从 [快速上手](getting-started.md) 开始；本页面向扩展内核、调试器或前端的开发者。阶段功能及尚未覆盖的场景见 [阶段状态](project-status.md)。

| 模块 | Python 包 / 发行包 | 职责 |
| --- | --- | --- |
| `src/agent_core/` | `agent_core` / `ai-agent-core` | Agent Loop、模型调用、会话、上下文、任务与通信、存储、权限、工具和 RAG |
| `src/agent_test/` | `agent_test` / `ai-agent-test` | Debug 记录器、诊断 API、调试页面；`tests/` 为回归套件，`scripts/` 为可安装的验证与工程脚本 |
| `src/agent_frontend/` | `agent_frontend` / `ai-agent-frontend` | 原生 Qt GUI、CLI、WebUI、普通 HTTP API、静态资源和应用启动组装 |

```text
src/
  agent_core/      # Python 包及 Core uv 项目根
    __init__.py
    api.py
    config.py
    core/
    tools/
    rag/
    pyproject.toml
  agent_test/      # Python 包及 Test uv 项目根
    __init__.py
    runtime.py
    web/
    static/
    scripts/
    tests/          # pytest 回归套件，不进入安装用的 wheel
    pyproject.toml
  agent_frontend/  # Python 包及 Frontend uv 项目根
    __init__.py
    main.py
    cli/
    web/
    desktop/
    pyproject.toml
docs/
  dev/            # 本地开发归档，继续忽略上传与镜像构建
```

三个模块概念仍称 AgentCore、AgentTest 和 Frontend，但物理目录采用 Python 导入包名；每个目录同时是包根和独立 uv 项目根。发行名仍为 `ai-agent-core`、`ai-agent-test`、`ai-agent-frontend`，Python 导入名仍为 `agent_core`、`agent_test`、`agent_frontend`。AgentTest 的 `tests/` 位于 `src/agent_test/` 下供开发回归使用，不进入安装用的 wheel；源码分发 sdist 可以包含测试。工程脚本统一通过 `python -m agent_test.scripts.<名称>` 执行；RAG 输入资料随 AgentTest 包分发。发布前的纯标准库版本检查仍可直接运行源码文件。

## 依赖方向

原生 GUI 直接连接 Core，不经过 Web 服务；Qt 依赖仅在 Frontend 的 `desktop` extra 中。`Frontend → AgentCore`；`AgentTest` 的运行时适配器也只依赖 `AgentCore`。Frontend 启动组装层在启用 Debug 时加载 AgentTest。AgentCore 不导入 Frontend、AgentTest、FastAPI 或 prompt_toolkit，不包含页面、终端渲染或调试事件存储。AgentTest 的跨模块回归与验收脚本可以测试 Frontend，但其可安装的运行时适配器不反向导入 Frontend。

内核的中性 instrumentation 接口负责事件作用域、span 和无操作观察器；`SessionManager(observer=...)` 接受注入的实现。默认内核不采集 Debug，即使环境变量 `DEBUG=true` 也是如此。Frontend 根据配置注入 AgentTest 的记录器。这样直接使用内核不会隐式启动诊断，关闭 Debug 的 Frontend 也能在没有 AgentTest 的安装环境中运行。

Flow 定义、便携模板与设计对话校验、持久化和节点执行位于 Core；原生和 Web 画布位于 Frontend，Qt 与 Web 框架均不进入 Core。Web 状态流只携带流程摘要和修订号，选择流程后读取完整定义及运行状态。每个节点使用 Core 会话，项目副本中的节点共用项目工作区，并沿有向边传递交接；可视化入口和使用限制见 [Agent Flow](agent-flow.md)。

节点配置可携带结构化交接契约。Core 在会话正常退出并排空工具后，对本轮末条助手正文做严格 JSON 校验。校验失败沿用节点失败和队列暂停路径。通过校验的交接在传给下游时标明上游来源和自报属性。状态记录保存验收结果，便携配置只保存约定。

会话接收输入时提交 `queued` 快照。每个会话在同一串行队列先同步消息日志，再原子发布会话索引。Windows 将不同会话分配到三条队列，Linux 使用一条队列。模型等待输入提交成功，随后在内存中进入 `running`。排队保存失败时终止本轮，并尝试保存失败记录。回复在执行工具或保存终态之前单独提交。进程异常退出时，磁盘上的 `queued` 或 `running` 都按中断恢复。会话任务包含终态保存，Web 状态流在任务真正退出后再次推送快照，同一会话此时可以安全接收下一轮。

修改功能时可从下表定位核心服务；路径相对 `src/agent_core/`：

| 行为 | 服务入口 | 前端职责 |
| --- | --- | --- |
| 会话运行与工具调度 | `core/sessions.py`、`core/agent_tasks.py` | 收集输入、展示状态与确认，不实现 Agent Loop |
| 项目与外部工作区身份 | `core/projects.py`、`core/workspaces.py` | 显式选目录并展示项目，不为未选目录回退到源码 |
| Flow 校验、串行执行与排空 | `core/flows.py` | 节点画布、连线、保存与运行详情 |
| 无工具设计及便携模板 | `core/flow_designer.py`、`core/flow_templates.py` | 多轮设计对话、导入新副本和文件选择 |
| 工具能力、模型与上下文 | `core/tool_settings.py`、`core/model_settings.py`、`core/history_context.py` | 展示有效配置；不扩大能力或预算 |

定义和用户项目分别保存在状态目录的 `flows/<id>.json` 与 `projects/<id>.json`；项目创建时复制模板定义，运行结果继续关联独立节点会话。`--state-dir` 选择状态目录，工作区选择项目产物目录，两者用途不同。项目文件、凭据和运行状态不进入便携模板；跨进程共用同一状态目录由独占锁拒绝。

桌面操作确认在审计收尾后重新读取 Flow 权威快照，避免旧启动响应覆盖较新的运行状态。运行状态发布与操作确认分别校验，不能以界面确认早晚替代 Core 完成和排空判定。

Debug 路由由 AgentTest 接收会话和存储服务后创建，沿用 `/debug`、`/api/debug` 与调试静态资源 URL。普通 WebUI 和诊断共享 Frontend 的鉴权、来源检查及请求限制。会话范围、因果标识、脱敏和按需读取语义不变。

## 安装与入口

```bash
uv sync --locked --extra web --extra desktop
uv run --no-sync ai-agent-startup
uv run --no-sync ai-agent-web
uv run --no-sync ai-agent-gui
```

根项目是开发聚合配置，三个模块各有 `pyproject.toml`。`Frontend` 提供 `ai-agent-gui`、`ai-agent-startup`、`ai-agent-web` 三个 console entrypoint；Core 不依赖终端或 Web 框架。直接调用内核时从 `agent_core.api` 获取会话入口，原来的 `ai_agent_startup.*` Python 导入路径已迁移；保存的 JSON 会话格式不变。

```python
from pathlib import Path
from agent_core.api import SessionManager, SessionStore

state_directory = Path("/path/to/separate-state")
store = SessionStore(state_directory)
manager = SessionManager(store)
```

此片段只展示组装关系。嵌入应用还需管理异步生命周期、保存队列与关闭；观察器装配参考 `src/agent_frontend/bootstrap.py`，完整生命周期参考 `desktop/runtime.py` 或 `web/app.py`。不要让其他 GUI/CLI 进程同时写这个状态目录。

`uv build --package ai-agent-core`、`uv build --package ai-agent-test` 和 `uv build --package ai-agent-frontend` 分别构建三个模块；WebUI 和 Debug 静态文件随各自 wheel 分发。对源码树执行验证时：

```bash
QT_QPA_PLATFORM=offscreen COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

运行时工作区仍为 `crud_tests/` 或用户选择的外部 Linux 项目目录。`src/agent_test/` 是工程代码目录，不是 Agent 执行任务时的工作区。用户状态与凭据不应移入任何模块目录。

Node 用于 JavaScript 回归，无需用它构建 WebUI。原生 GUI 回归使用 `QT_QPA_PLATFORM=offscreen` 和系统 Qt 库。离线测试、受控界面 fixture、真实浏览器操作与真实模型业务执行分别记录；包构建通过也不能代替业务验收。

原生桌面启动与交互见[桌面指南](desktop.md)，工具目录与框架职责见[工具设计](tool-design.md)。
