# 新对话交接

在仓库根目录开启新对话时，可以直接发送这一句。

> 请先读 `AGENTS.md` 和 `docs/session-handoff.md`，检查当前分支和工作区，保留已有改动。接下来我要完成〈具体任务〉。

## 先确定手里的代码

```bash
git status --short --branch
git branch --show-current
git log --oneline -5
```

当前分支可能是待审 PR，或含有尚未提交的用户改动。发布行为以对应 Git 标签的源码和文档为准。了解产品从首页开始，按任务找指南用文档索引。[项目首页](../README.md) · [文档索引](README.md) · [仓库约定](../AGENTS.md)

## 从一次输入找到实现

ChannelAgent 把用户选定的工作目录交给 Agent 处理。经典会话由模型决定下一步调用哪个可用工具。Flow 把职责拆成有向图上的节点，每个节点都通过同一套会话运行机制完成工作。

| 要定位的问题 | 从哪里读起 |
| --- | --- |
| 对话如何变成工具调用？ | `SessionManager` 准备模型输入，接收回复，执行获准的工具，把回执带到下一轮并保存会话。新工具在 Core 工具目录注册。[会话循环](../src/agent_core/core/sessions.py) · [工具入口](../src/agent_core/tools/__init__.py) |
| 怎样从设计走到下一节点？ | 设计助手生成可导入的模板。用户创建项目、选定工作区后，`FlowService` 按依赖顺序逐个运行节点。当前节点的工具收尾、最终输出和可选交接校验完成后，下游才取得交接。节点内部可以委派子 Agent。[设计器](../src/agent_core/core/flow_designer.py) · [Flow 调度](../src/agent_core/core/flows.py) · [Flow 指南](agent-flow.md) |
| 桌面、Web 和调试器在哪里接入？ | Qt 桌面和 Web 都调用 Core 服务。界面负责收集操作、展示确认和状态。Debug 通过观察器记录运行事件，记录器与回归测试位于 AgentTest。[桌面桥接](../src/agent_frontend/desktop/runtime.py) · [Web 入口](../src/agent_frontend/web/app.py) · [Debug 适配器](../src/agent_test/runtime.py) |
| 模型在哪里找资料和项目上下文？ | RAG 在配置的知识库中检索资料。会话保留自己的历史。工作区可以用 `.channelagent/memory.json` 共享项目说明、进度和经验。Flow 节点默认每轮新建会话，可按同一项目的同一节点启用复用。[检索指南](rag.md) · [工作区记忆](workspace-memory.md) · [三模块架构](architecture.md) |

## 运行和验收

源码使用 Python 3.12+ 和 uv。在 Linux 或 WSL 的仓库根目录安装依赖。首次从源码启动 Web 前，先在根目录的 `.env` 或进程环境中配置 `OPENCODE_API_KEY`、`BASE_URL`、`MODEL`。桌面成品可直接打开「模型管理」添加连接。[首次配置](getting-started.md#从源码运行)

```bash
uv sync --locked --extra web --extra desktop
uv run --no-sync ai-agent-web
```

默认地址为 `http://127.0.0.1:8765`。验证代码时先跑改动对应的用例，再跑离线套件。工具或沙箱有变化时另运行下面的真实隔离探针。[快速上手](getting-started.md) · [贡献指南](../CONTRIBUTING.md)

```bash
QT_QPA_PLATFORM=offscreen COMMAND_EXECUTION_ENVIRONMENT=sandbox PYTHON_DOTENV_DISABLED=1 \
  uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

在 Windows 打开 WSL 仓库时，从该发行版的 Linux shell 运行以上命令，例如由 PowerShell 执行 `wsl -d Ubuntu` 后进入仓库根目录。用 `command -v uv`、`command -v node` 检查 Linux 工具。若 Node 由 nvm 安装，先加载 `"${NVM_DIR:-$HOME/.nvm}/nvm.sh"`。Windows 原生 checkout 使用自己的 Python 环境。交互式桌面、Web、命令隔离及数据目录的安装差异见平台指南。[Windows](windows.md) · [Linux](linux.md)

测试使用独立状态目录和工作区。已有的 `.env`、会话、知识库原文、工作区记忆、正式模板及已下载模型应保留。`crud_tests/` 是默认运行工作区，`.cache/` 里既有验证产物也可能有检索索引和权重。本地 `docs/dev/` 可含作者的面试报告和基准材料。修改工具时要保留工作区边界、确认、工具与回执的配对，以及原子写盘语义。[配置与路径](configuration.md) · [沙箱](sandbox.md)

## 发布与待办（2026-11-20）

- **发布基线**为 `v0.6.3`（`9b8ca82`）。下载的 Windows 单文件 EXE 和 Linux 成品分别通过 16/16、17/17 项离线自检。七节点网站用例使用真实模型，在保留已有产物的工作区完成。业务运行和成品检查各自有独立证据。[正式发布](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.3) · [网站实测](flow-designer-fullstack-validation-2026-11-19.md)
- **仍待定位**的现象是 Windows CI 曾在 Qt 处理事件时出现一次原生访问违规。同一提交重跑及本机三轮桌面回归均通过。下一次出现时，优先收集原生堆栈和当时的用例顺序。[原失败任务](https://github.com/vukencc/Channel-Agent/actions/runs/44665103174/job/136052362587)
- **单独开发的工作**包括客服反馈证据复核实验。PR #62 暂缓合并，继续该实验时先核对其分支和最新验证记录。[PR #62](https://github.com/vukencc/Channel-Agent/pull/62)

版本、任务或验证结论变化时，更新上面三项，把详细测量放进对应报告。需要追溯早期环境和逐轮结果时，可查历史索引和交接旧版。[历史会话交接](session-history.md) · [v0.6.3 标签中的交接旧版](https://github.com/vukencc/Channel-Agent/blob/v0.6.3/docs/session-handoff.md)
