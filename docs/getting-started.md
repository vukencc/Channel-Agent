# 快速上手

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

在 Linux 或 WSL 中使用 Python 3.12 及以上、Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)；uv 可按安装指南安装 Python。原生 GUI 需要图形会话，WSL 需要 WSLg。Windows 用户请在 WSL 的 Linux 终端运行命令，避免用 Windows uv 操作 Linux 虚拟环境。

## 安装与启动

```bash
git clone https://github.com/vukencc/Channel-Agent.git
cd Channel-Agent
uv sync --locked --extra desktop --extra web
```

以上同时安装桌面和 Web 依赖；只用桌面可省略 `--extra web`，只用 Web 可省略 `--extra desktop`，只用 CLI 可省略两个 extra。在仓库根目录选择入口：

```bash
uv run --no-sync ai-agent-gui      # 原生窗口
uv run --no-sync ai-agent-web      # 浏览器
uv run --no-sync ai-agent-startup  # 交互终端
```

Web 默认地址为 `http://127.0.0.1:8765`，仅供本机访问，无需 Node 构建。GUI 和 Web 提供 Flow；CLI 提供经典 Agent 会话。默认状态目录由一个进程独占，同时启动时为另一进程指定不同的 `--state-dir`。详见[桌面](desktop.md)、[Web](web-ui.md)和 [CLI](cli.md)指南。

## 配置模型并发送消息

在 GUI 或 Web 的“模型管理”添加 OpenAI-compatible 模型连接，填写服务地址、模型名和服务所需密钥；在会话设置中选择连接。上下文容量请根据服务商实际容量填写，留空采用保守预算。模型服务须自行提供，调用可能产生费用。

也可在仓库根目录仅当 `.env` 不存在时创建配置，然后编辑其中的 `OPENCODE_API_KEY`、`BASE_URL` 和 `MODEL`：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

已有 `.env` 不会被此命令覆盖。进程环境变量优先于 `.env`；不要把密钥提交或粘贴到问题报告中。完整设置和优先级见[配置说明](configuration.md)。

创建经典 Agent 会话，选择模型并发送简单任务，例如“介绍你能使用的工具”。操作文件前检查项目目录和权限，并按提示处理确认。CLI 的 `/models` 可选择模型，`/project /绝对路径` 可选择已有 Linux 项目目录；详见 [CLI 指南](cli.md)。

## 创建 Flow 项目

1. 在 GUI 或 Web 左栏创建 Flow 模板，添加节点、填写节点指令并连接端口；也可使用右侧设计助手生成新模板。助手设计时不执行节点。
2. 新建 Flow 项目，选择模板副本或空白流程，再选择已有的有效工作区目录。模板可不绑定项目，但运行必须显式选择项目和有效目录。
3. 检查节点指令、模型和工具，保存并运行。节点拥有独立会话，共享项目目录；上游正常结束后的正文沿连线传递，节点按拓扑串行调度。
4. 查看运行状态、交接和产物并自行验收。节点完成不保证业务结果正确；权限、确认和部署工具上限仍生效。

可导出便携 JSON 并导入为新的未绑定模板；文件不包含项目目录、凭据或知识库内容。节点配置、停止和恢复等详见 [Agent Flow](agent-flow.md)。

## 知识库与环境

普通对话不要求先准备 RAG 模型。使用知识库检索时需准备资料及嵌入、重排模型，首次使用可能需要额外下载；路径、支持的文件类型和准备方式见 [RAG 指南](rag.md)。PDF 和 Word 解析是可选依赖 `documents`。

Linux 隔离命令需要系统安装 Bubblewrap（`bwrap`）和 `prlimit`；Debian/Ubuntu 对应的软件包是 `bubblewrap` 和 `util-linux`。此外系统必须允许所需命名空间；能力不可用时命令会被拒绝，不会自动退回宿主执行。文件工具仍可独立使用。配置、检查与本机模式边界见[沙箱说明](sandbox.md)和[环境准备](environment-preparation.md)。
