# AI Agent Startup

一个基于 Python 3.12+ 的本地 Agent 工作区，提供对话、工具执行、多 Agent 协作、Flow 画布和混合 RAG。可选择原生桌面 GUI、浏览器 WebUI 或终端 CLI；模型请求需要自行配置可用的模型服务。

支持 Linux、WSL 和 Windows 原生应用。Windows 的桌面、Web、文件和 Flow 在本机运行，隔离命令使用 WSL2 的 Linux 执行器。Linux 原生界面需要图形会话，WSL 使用 WSLg，CLI 需要交互终端。[快速上手](docs/getting-started.md)介绍模型与首个项目，[Windows 指南](docs/windows.md)说明两套运行环境的安装和当前边界。

## 安装与启动

Windows 和 Linux 桌面成品通过 [Releases](https://github.com/vukencc/Channel-Agent/releases) 分发；Windows 选择 ZIP，Linux x64 选择 tar.gz。运行与系统依赖见 [Windows 指南](docs/windows.md)和 [Linux 成品指南](docs/linux.md)。桌面、Web 与 Debug 使用平直的深色工作台、细分隔线和小圆角控件，Flow 状态点及连接端口保留圆形。

Windows 成品为免安装目录：解压后双击 `ChannelAgent.exe`，保留同目录的 `_internal`，无需安装 Windows Python。首次使用在「模型管理」添加连接；用户数据默认保存在 `%LOCALAPPDATA%\ChannelAgent`。成品构建、WSL 命令准备与自检见 [Windows 指南](docs/windows.md)。

```bash
uv sync --locked --extra desktop --extra web
if [ ! -f .env ]; then cp .env.example .env; fi
uv run --no-sync ai-agent-gui
```

`.env` 不存在时才从模板创建，可填写模型服务配置；桌面和 Web 也可通过“模型管理”添加连接。已有 `.env` 保持不变。原生 GUI 直接连接内核，不启动浏览器。CLI 使用 `uv run --no-sync ai-agent-startup`。`--extra web` 保留 WebUI 依赖；启动 WebUI：

```bash
uv run --no-sync ai-agent-web
```

WebUI 默认监听本机 `127.0.0.1:8765`。默认权限策略为 `smart`；无交互的 headless 任务默认为 `readonly`。权限模式、确认和沙箱边界见[配置说明](docs/configuration.md)与[沙箱说明](docs/sandbox.md)。

## 主要概念与文档

- **Agent 会话**：与模型对话并使用工具；子 Agent 可共享父会话的项目工作区。
- **Flow 模板**：用节点和连线描述任务及交接，可独立编辑和分享。设计助手生成模板但不执行节点。
- **Flow 项目**：选择已有工作区并使用模板副本；运行前必须选择有效目录。节点各有会话，共享项目，按拓扑串行执行。

[阶段状态](docs/project-status.md)说明已验证内容与质量边界。

- 原生桌面和 WebUI 均可使用经典 Agent 与可视化 Flow：[桌面 GUI](docs/desktop.md)、[Agent Flow](docs/agent-flow.md)
- 经典 Agent 工具自动管理、Flow 节点工具配置与重复入口简化：[工具设计](docs/tool-design.md)

- 多会话 CLI、子 Agent 与父子消息：[CLI 指南](docs/cli.md)、[会话工具](docs/session-tools.md)
- 本机浏览器界面与只读运行调试：[WebUI 指南](docs/web-ui.md)、[DEBUG 运行调试](docs/debug-runtime.md)
- 混合检索、索引与质量验证：[RAG 指南](docs/rag.md)、[RAG 质量校准](docs/rag-quality.md)
- 结构化历史与可选监督器：[上下文管理](docs/context-management.md)
- 完整环境变量说明：[配置说明](docs/configuration.md)
- 新会话入口：[交接说明](docs/session-handoff.md)；全部指南、设计边界与历史记录：[文档索引](docs/README.md)

## 开发验证

```bash
COMMAND_EXECUTION_ENVIRONMENT=sandbox uv run --no-sync pytest -m 'not integration' -q
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

## 项目布局

项目分为 `src/agent_core/`（运行内核）、`src/agent_test/`（Debug、回归与验证脚本）、`src/agent_frontend/`（原生桌面、CLI 与 WebUI），由根 uv workspace 管理。模块依赖、独立构建和入口见[架构说明](docs/architecture.md)。Docker 资产位于 `docker/`，知识库原文位于 `data/raw/`；运行数据位置不变。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
