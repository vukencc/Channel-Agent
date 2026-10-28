# 三模块架构

仓库由一个 uv workspace 管理三个可独立构建的 Python 包，版本和锁文件集中维护。命令仍从仓库根运行，已有 `.env`、`.agent/`、`crud_tests/`、日志和知识库位置不变。

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

Flow 定义、校验、持久化和节点执行位于 Core；原生和 Web 画布位于 Frontend，Qt 与 Web 框架均不进入 Core。Web 状态流只携带流程摘要和修订号，选择流程后读取完整定义及运行状态。每个节点使用 Core 会话，并沿有向边传递项目与交接；可视化入口和使用限制见 [Agent Flow](agent-flow.md)。

Debug 路由由 AgentTest 接收会话和存储服务后创建，沿用 `/debug`、`/api/debug` 与调试静态资源 URL。普通 WebUI 和诊断共享 Frontend 的鉴权、来源检查及请求限制。会话范围、因果标识、脱敏和按需读取语义不变。

## 安装与入口

```bash
uv sync --locked --extra web
uv run --no-sync ai-agent-startup
uv run --no-sync ai-agent-web
```

根项目是开发聚合配置，三个模块各有 `pyproject.toml`。`Frontend` 提供 `ai-agent-gui`、`ai-agent-startup`、`ai-agent-web` 三个 console entrypoint；Core 不依赖终端或 Web 框架。直接调用内核时从 `agent_core.api` 获取会话入口，原来的 `ai_agent_startup.*` Python 导入路径已迁移；保存的 JSON 会话格式不变。

```python
from agent_core.api import SessionManager, SessionStore

store = SessionStore(state_directory)
manager = SessionManager(store)
```

`uv build --package ai-agent-core`、`uv build --package ai-agent-test` 和 `uv build --package ai-agent-frontend` 分别构建三个模块；WebUI 和 Debug 静态文件随各自 wheel 分发。对源码树执行验证时：

```bash
COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

运行时工作区仍为 `crud_tests/` 或用户选择的外部 Linux 项目目录。`src/agent_test/` 是工程代码目录，不是 Agent 执行任务时的工作区。用户状态与凭据不应移入任何模块目录。

原生桌面启动与交互见[桌面指南](desktop.md)，工具目录与框架职责见[工具设计](tool-design.md)。
