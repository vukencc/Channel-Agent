# 快速上手

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

本指南面向 v0.6.2。选择 [v0.6.2 发布页](https://github.com/vukencc/Channel-Agent/releases/tag/v0.6.2)上的成品，或从对应标签运行源码。桌面需要图形会话；WSL 中运行 Linux GUI 需要 WSLg。

## 下载桌面成品

发布工作流提供六项文件：Windows 独立 exe、Windows ZIP、Linux x64 tar.gz，及各自同名的 `.sha256` 校验文件。ZIP 和 tar 文件名中的 `<构建时间>` 以发布页实际名称为准。模型服务和本地 RAG 模型权重不随成品提供。

### Windows 10/11 x64

下载 `ChannelAgent-0.6.2-windows-x64.exe` 和 `ChannelAgent-0.6.2-windows-x64.exe.sha256` 到同一目录。在该目录打开 PowerShell，核对输出为 `True`：

```powershell
$expected = (Get-Content .\ChannelAgent-0.6.2-windows-x64.exe.sha256 -Raw).Trim().Split(' ')[0]
(Get-FileHash .\ChannelAgent-0.6.2-windows-x64.exe -Algorithm SHA256).Hash -eq $expected
```

将 exe 放到本地可写 NTFS 目录并双击，无需改名或安装 Python。默认数据保存在 exe 所在目录；已有文件不会覆盖。也可下载带构建时间的 ZIP 及其校验文件，解压后运行内部目录的 `ChannelAgent.exe`。

可先运行免模型离线自检。以下命令使用新的报告文件，等待窗口程序结束，再读取 JSON；退出码为 0 且 `ok` 为 `true` 表示本次自检通过：

```powershell
$report = Join-Path $env:TEMP ("ChannelAgent-check-" + [guid]::NewGuid().ToString() + ".json")
$process = Start-Process -FilePath .\ChannelAgent-0.6.2-windows-x64.exe -ArgumentList '--self-check', ('"' + $report + '"') -Wait -PassThru
$process.ExitCode
Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
```

自检会初始化便携数据根，但不调用模型或下载权重。没有 WSL 仍可使用桌面和文件功能；隔离命令需另行准备 WSL2 和 Linux 环境，无法隔离时会拒绝执行。数据根及 WSL 步骤见 [Windows 指南](windows.md)。

### Linux x86_64

下载 `ChannelAgent-0.6.2-linux-x64-<构建时间>.tar.gz` 及其同名 `.sha256` 到同一目录，将下面的文件名替换为实际名称：

```bash
archive='ChannelAgent-0.6.2-linux-x64-<构建时间>.tar.gz'
sha256sum -c "$archive.sha256"
tar -xzf "$archive"
cd "${archive%.tar.gz}"
./ChannelAgent
```

校验应显示 `OK`；解压后保留 `ChannelAgent`、`_internal` 和附带文件。需要兼容构建包所用 glibc 的图形系统及 Qt 系统库。默认数据在 `$XDG_DATA_HOME/ChannelAgent`，未设置有效绝对路径时为 `~/.local/share/ChannelAgent`。系统依赖和隔离命令准备见 [Linux 指南](linux.md)。

可从解压目录运行离线自检，使用不存在的报告路径；检查退出码及报告中的 `ok`：

```bash
report="/tmp/channel-agent-check-$(date +%s)-$$.json"
./ChannelAgent --self-check "$report"
echo $?
cat "$report"
```

自检不调用模型服务或下载权重；它检查本机能力，不能代替真实模型业务验收。

## 从源码运行

Linux 或 WSL 需要 Git、Python 3.12 以上和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。从指定标签克隆：

```bash
git clone --branch v0.6.2 https://github.com/vukencc/Channel-Agent.git
cd Channel-Agent
uv sync --locked --extra desktop --extra web
```

以上同时安装桌面和 Web 依赖；只用桌面可省略 `--extra web`，只用 Web 可省略 `--extra desktop`，只用 CLI 可省略两个 extra。在仓库根目录选择入口：

```bash
uv run --no-sync ai-agent-gui      # 原生窗口
uv run --no-sync ai-agent-web      # 浏览器
uv run --no-sync ai-agent-startup  # 交互终端
```

Web 默认地址为 `http://127.0.0.1:8765`，仅供本机访问，无需 Node 构建。GUI 和 Web 提供 Flow；CLI 提供经典 Agent 会话。默认状态目录由一个进程独占，同时启动时为另一进程指定不同的 `--state-dir`。Windows 原生源码环境及 WSL 环境须分开，具体步骤见 [Windows 指南](windows.md)。详见[桌面](desktop.md)、[Web](web-ui.md)和 [CLI](cli.md)指南。

## 配置模型并发送消息

在 GUI 或 Web 的“模型管理”添加 OpenAI-compatible 模型连接，填写服务地址、模型名和服务所需密钥；在会话设置中选择连接。上下文容量请根据服务商实际容量填写，留空采用保守预算。模型服务须自行提供，调用可能产生费用。

也可在仓库根目录仅当 `.env` 不存在时创建配置，然后编辑其中的 `OPENCODE_API_KEY`、`BASE_URL` 和 `MODEL`：

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
```

已有 `.env` 不会被此命令覆盖。进程环境变量优先于 `.env`；不要把密钥提交或粘贴到问题报告中。完整设置和优先级见[配置说明](configuration.md)。

创建经典 Agent 会话，选择模型并发送简单任务，例如“介绍你能使用的工具”。操作文件前检查项目目录和权限，并按提示处理确认。CLI 的 `/models` 可选择模型，`/project /绝对路径` 可选择已有项目目录；详见 [CLI 指南](cli.md)。

## 创建 Flow 项目

1. 在 GUI 或 Web 左栏创建 Flow 模板，添加节点、填写节点指令并连接端口；也可使用右侧设计助手生成新模板。助手设计时不执行节点。
2. 新建 Flow 项目，选择模板副本或空白流程，再选择已有的有效工作区目录。模板可不绑定项目，但运行必须显式选择项目和有效目录。
3. 检查节点指令、模型和工具，保存并运行。节点拥有独立会话，共享项目目录；上游正常结束后的正文沿连线传递，节点按拓扑串行调度。
4. 查看运行状态、交接和产物并自行验收。节点完成不保证业务结果正确；权限、确认和部署工具上限仍生效。

可导出便携 JSON 并导入为新的未绑定模板；文件不包含项目目录、凭据或知识库内容。节点配置、停止和恢复等详见 [Agent Flow](agent-flow.md)。

## 知识库与环境

普通对话不要求先准备 RAG 模型。使用知识库检索时需准备资料及嵌入、重排模型，首次使用可能需要额外下载；路径、支持的文件类型和准备方式见 [RAG 指南](rag.md)。PDF 和 Word 解析是可选依赖 `documents`。

Linux 和 Windows 的 WSL 隔离命令需要在 Linux 中安装 Bubblewrap（`bwrap`）和 `prlimit`；Debian/Ubuntu 对应的软件包是 `bubblewrap` 和 `util-linux`。此外系统必须允许所需命名空间；能力不可用时命令会被拒绝，不会自动退回宿主执行。文件工具仍可独立使用。配置、检查与本机模式边界见[沙箱说明](sandbox.md)和[环境准备](environment-preparation.md)。
