# ChannelAgent

ChannelAgent 是一个在本机运行的 AI 工作台。你可以让一个 Agent 阅读和修改项目文件，也可以把网站开发拆成接口设计、前端、后端和验收，让各节点把结果交给下一步。桌面和 Web 都能编辑流程，终端可用于经典会话。

- **Agent 会话**适合边聊边做。模型从当前权限允许的工具中选择下一步，也可以把子任务委派给其他 Agent。
- **Flow** 适合重复使用同一种分工。可以在画布连出流程，也可以用一句需求让设计助手生成模板。给每个节点设置岗位指令、模型和可用工具，再把模板导入项目运行。

## 第一次使用

桌面成品和各自的 SHA256 校验文件都在 [v0.6.3 发布页](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.3)。

- **Windows 10/11 x64** 下载独立 exe，校验后放入本地可写 NTFS 目录并双击运行。无需安装 Windows Python，配置和数据默认保存在 exe 所在目录。需要执行隔离命令时再配置 WSL2。[下载 Windows 单文件 EXE](https://github.com/vukencc/Channel-Agent/releases/download/v0.6.3/ChannelAgent-0.6.3-windows-x64.exe)
- **Linux x86_64** 下载 tar.gz，校验并解压后，在图形会话中运行 `./ChannelAgent`，保留 `_internal` 和附带文件。系统需要兼容的 glibc 和 Qt 库。

校验命令、离线自检及系统准备见[快速上手](docs/getting-started.md)、[Windows 指南](docs/windows.md)和 [Linux 指南](docs/linux.md)。离线自检不调用模型。

在「模型管理」添加一个兼容 OpenAI 接口的模型服务，填写服务地址、模型名和密钥。创建 Agent 会话，选择有 `README.md` 的项目目录和刚配置的模型，发送“读取 README.md，用一句话说明这个项目做什么”。在对话中可以核对文件读取记录和回答。模型服务由你提供，费用取决于服务商。[模型配置](docs/getting-started.md#配置模型并发送消息)

想体验 Flow，可以在桌面或 Web 打开设计助手，选择模型并输入“设计一个网站开发 Flow，先定架构，再分别实现前端和后端，最后测试、修订并复测”。检查画布中的节点和连线后，新建 Flow 项目，选择模板和已有工作区。确认节点的模型和工具后运行，在「Working」查看节点状态与交接结果，最后到工作区检查网站产物。[Flow 入门](docs/getting-started.md#创建-flow-项目)

### 从源码启动 Web

Linux 或 WSL 需要 Git、Python 3.12+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
git clone https://github.com/vukencc/Channel-Agent.git
cd Channel-Agent
uv sync --locked --extra web
if [ ! -f .env ]; then cp .env.example .env; fi
```

打开仓库根目录的 `.env`，填好服务密钥 `OPENCODE_API_KEY`、接口地址 `BASE_URL` 和模型名 `MODEL`。已有这三个进程环境变量时可直接启动。Web 会在监听端口前校验配置。

```bash
uv run --no-sync ai-agent-web
```

在同一台机器打开 `http://127.0.0.1:8765`。Web 无需 Node 构建，也可在界面的「模型管理」里添加其他连接。[配置说明](docs/configuration.md)

## 实现与验证

| 模块 | 职责 |
| --- | --- |
| [AgentCore](src/agent_core/) | 驱动模型与工具的对话循环、调度 Flow、检查权限，并通过 RAG 从知识库检索相关片段 |
| [Frontend](src/agent_frontend/) | 桌面、Web、CLI 和应用启动组装 |
| [AgentTest](src/agent_test/) | 诊断适配、Debug 和回归测试 |

桌面和 Web 复用同一套内核。诊断模块通过观察器记录运行事件，所以同一个 Flow 在两个界面遵循相同的调度和权限规则。[架构说明](docs/architecture.md)

**节点如何传递上下文？** 同一项目的节点可读取同一工作区的文件和项目摘要，各自保留私有对话历史。上游把本轮结果显式交给下游，工具仍按设置检查权限和确认。[工作区记忆](docs/workspace-memory.md) · [会话实现](src/agent_core/core/sessions.py) · [沙箱边界](docs/sandbox.md)

**为什么前端和后端画成两个分支？** 连线决定谁需要谁的结果。调度器一次运行一个就绪节点，等上游的工具执行完毕、输出通过所配置的格式检查，再把结果交给下游。模型连接中断或超时并留下可恢复检查点时，可从原节点继续。[Flow 指南](docs/agent-flow.md) · [调度源码](src/agent_core/core/flows.py) · [交接校验](src/agent_core/core/flow_handoff.py)

一次真实网站案例中，七节点 Flow 在保留已有网站产物的新实例完成测试、修订和交付。独立验收在 Windows 和 Linux 各通过 29 项网站测试与 48 项 HTTP 检查，真实 Chrome 在桌面、窄屏两种尺寸通过 25 项操作检查。该案例使用单独配置的 32k 输入窗口，报告也保留了早期失败和中断。[网站实测及完整条件](docs/flow-designer-fullstack-validation-2026-11-19.md)

[文档索引](docs/README.md) · [新对话交接](docs/session-handoff.md) · [MIT 许可证](LICENSE)
