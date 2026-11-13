# Windows 原生使用指南

> 文档类型：当前使用指南 · [文档导航](README.md) · [快速上手](getting-started.md)

Windows 可运行原生 Qt 桌面、Web 界面及 Core，Flow 和会话状态支持保存及节点会话复用。隔离命令通过 WSL2 中的 Linux 执行器运行。当前验证范围见本文末尾。

## 使用免安装成品

成品目标为 Windows 10/11 x64。将 `ChannelAgent.exe` 放在本地可写目录并双击；首次在「模型管理」添加连接，再创建项目或 Flow 模板。默认构建为 PyInstaller 单 exe：Python、Qt、Core、文档/图片和本地检索运行依赖均打包，无需另装 Windows Python、uv、Node 或 .NET。启动时 bootloader 将运行库解包到系统临时目录，持久数据仍在选定的数据根。模型权重不随包分发，本地 RAG 首次使用可能下载模型，也可配置已有模型或云端 Embedding。系统 Visual C++ 运行库依赖需以实际 exe 审计为准，不能据此假定所有干净系统均无额外前提；当前包未进行商业代码签名。

### 数据根与首次启动

默认数据根是 exe 所在目录，与启动工作目录无关。首次启动自动创建下列目录及最小 `.env`；已有文件和凭据不会覆盖，也不会自动迁移旧版用户目录数据：

```text
数据根/
  .env                         应用环境配置
  config/                      模型连接等配置
  data/state/                  会话状态
  data/workspaces/             默认工作区
  data/projects/default/       默认项目
  data/raw/                    原始资料
  cache/rag/                   RAG 缓存
  cache/huggingface/           Hugging Face 缓存（含 hub、modules）
  cache/sentence-transformers/、cache/fastembed/、cache/torch/、cache/tmp/
  logs/                        日志
```

启动时还会将内嵌的 `configuration.example.env`、`使用说明.txt`、许可证及 `WSL/requirements.txt` 复制到数据根，仅创建缺失文件。`.env` 已自动生成，请参考模板按需编辑它，不要覆盖现有文件。部分模板路径是源码默认值；显式填写后会覆盖便携默认路径。搬移或备份时保留所选数据根及用户项目。`--state-dir` 只选择独立会话状态目录，不改变整个数据根；独立状态目录使用独立配置，不共享默认便携根的凭据。

数据根优先级为 `--data-dir`、进程环境变量 `AI_AGENT_PROJECT_ROOT`、exe 所在目录。也可显式使用 `--use-user-data` 选择 `%LOCALAPPDATA%\ChannelAgentPortable`（未设置 `LOCALAPPDATA` 时为用户目录下 `AppData\Local\ChannelAgentPortable`）；它与 `--data-dir` 互斥。示例：

```powershell
& .\ChannelAgent.exe --data-dir "D:\ChannelAgentData"
& .\ChannelAgent.exe --use-user-data
```

默认目录无法写入时，界面提供用户目录、手选目录或取消；显式指定的数据根失败时显示错误，不自动转移到其他目录。安全检查或其他 I/O 错误也会报错。使用非交互初始化诊断可输出新建的 JSON 文件，不启动界面或覆盖已有报告：

```powershell
Start-Process -FilePath .\ChannelAgent.exe -ArgumentList '--portable-info', 'D:\portable-info.json' -Wait
```

报告包含实际数据根和各路径，失败时记录错误；输出文件的父目录必须已存在。诊断会初始化选定的数据根并释放内嵌资料，不调用模型。源码运行和 Linux 成品的默认目录不受此便携规则影响。

### 可选隔离命令

没有 WSL 也可使用桌面界面和文件功能；隔离命令需要 WSL2、Linux Python 及依赖，环境不可用时拒绝执行。成品内 `使用说明.txt` 提供独立 Linux 虚拟环境步骤，`WSL/requirements.txt` 从锁文件导出，可用 Linux pip 的 `--require-hashes` 安装。Linux 执行器源码内嵌于包，运行时使用解包资源，不引用开发 checkout；其依赖环境与 Windows 运行库独立。

成品支持离线自检，不调用模型或下载模型，报告仅记录能力通过状态和异常类型：

```powershell
& .\ChannelAgent.exe --self-check "$env:TEMP\ChannelAgent-check.json"
```

该命令是窗口程序；在 PowerShell 需要等待完成时使用 `Start-Process -Wait`，随后查看报告的 `ok`。报告路径必须不存在，自检也会先初始化所选便携数据根。配置好 WSL 后，可先设置 `$env:CHANNEL_AGENT_SELF_CHECK_WSL = "1"` 再运行，附加验证真实隔离写入。`manifest.json` 给出成品文件 SHA256，ZIP 旁的 `.sha256` 给出压缩包校验值。测试使用独立临时目录。

## 从源码构建成品

在 Windows 本地盘源码根目录中执行，不能使用 WSL Python 构建 Windows exe：

```powershell
uv sync --locked --group packaging --extra desktop --extra web --extra documents --extra vision --python 3.12
uv run --no-sync python -m agent_test.scripts.build_windows
```

构建使用锁定的 PyInstaller，默认 `--layout onefile`；需要目录版时传入 `--layout onedir`，该版须保留 exe 同级的 `_internal`。窗口包带 Python、Qt、Core、Web/Debug 资源及 CPU 推理依赖。因包含 PyTorch/ONNX 运行库，体积较大。`dist/` 保存免安装目录、ZIP 和校验值；每次构建使用独立名称，不覆盖已有成品。构建和自检记录放在新建的 `.cache/reports/windows-package-*`。构建逐一核对 exe 内的应用字节码与当前源码，从其他工作目录启动实际 exe，移除开发 Python 的路径并执行自检，通过后才生成分发 ZIP。`--skip-self-check` 仅用于定位构建问题，不能据此宣称成品验收通过。

构建子进程收窄 PATH，避免借用宿主开发工具的 DLL。Qt 依赖 Windows 的系统 ICU，成品排除不兼容的同名 `icuuc.dll` 及其孤立数据 DLL；不要从其他工具目录手动复制这些库进成品。

## 准备原生环境

将仓库放在本地 NTFS 磁盘，例如 `D:\AI\AgentSrcCode\ChannelAgent`。安装 Python 3.12 或以上、Git 和 Windows 版 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在仓库目录打开 PowerShell：

```powershell
$env:PYTHONUTF8 = "1"
uv sync --locked --extra desktop --extra web --python 3.12
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
uv run --no-sync ai-agent-gui
```

仅使用 Web 时可省略 `--extra desktop`，并以 `uv run --no-sync ai-agent-web` 启动。CLI 入口为 `uv run --no-sync ai-agent-startup`。Web 和 CLI 启动前须填写有效的模型服务配置，桌面也可通过模型管理添加连接。已有 `.env` 会保留。同一状态目录由一个进程独占，同时运行多个界面时为其他进程指定独立的 `--state-dir`。配置方式见 [快速上手](getting-started.md)。

桌面加载系统已有的中文字体。精简 Windows 镜像缺少中文字体时，需要补齐系统字体组件。

## 模型连接与代理

默认继续使用 Python HTTP 客户端的环境代理规则。用户环境中的 `ALL_PROXY`、`HTTP_PROXY` 或 `HTTPS_PROXY` 可能优先于 Windows 系统代理；因此密钥、模型名称正确，也可能被失效的环境代理阻断。设计助手会区分首段响应前连接失败和生成途中断流，不把二者统一判断成密钥错误。

若要使用已启用的 Windows 固定系统代理，在「模型管理 → 编辑连接 → 高级参数 → 代理来源」明确选择「Windows 系统代理」后保存。此选项同时用于该模型的正常会话、Flow 节点和设计助手，不需要重新填写原密钥；其它模型保持原策略。Web 模型管理也可设置，依据的是服务端操作系统。

系统模式覆盖该连接的环境代理，保留系统排除规则、`NO_PROXY`、TLS 校验和 `SSL_CERT_FILE`／`SSL_CERT_DIR`。目前支持 HTTP/HTTPS 固定代理，不解析 PAC、自动发现或仅 SOCKS 配置；没有可用固定手动代理时会报错，不自动切回环境代理或直连。应用不会改写用户或系统环境变量，也不会自动重写已有模型记录。连通性和请求参数支持性应分别验证，连接失败不会自动降低所选推理强度。

## 准备 WSL 隔离命令

安装并启用 WSL2 和 Ubuntu，确保 Windows 盘已在 WSL 挂载。下例假设仓库路径为上述 D 盘目录，WSL 中的路径为 `/mnt/d/AI/AgentSrcCode/ChannelAgent`；使用其他目录时请按实际挂载修改。进入 Ubuntu 终端，安装 Linux 版 uv 后执行：

```bash
sudo apt update
sudo apt install bubblewrap util-linux
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/ChannelAgent/wsl-venv"
uv sync --project /mnt/d/AI/AgentSrcCode/ChannelAgent --locked --python 3.12
```

该命令将 Linux 环境放在 WSL 用户目录，不覆盖仓库中 Windows 的 `.venv`。两套环境分别安装依赖；不要用 Windows uv 操作 Linux 虚拟环境，也不要用 Linux uv 重建 Windows `.venv`。WSL 的 Linux Python 必须能加载项目依赖，系统还须允许 Bubblewrap 所需命名空间，单纯安装软件包不保证隔离可用。

在 Windows 应用的 `.env` 中按实际用户路径设置：

```dotenv
WINDOWS_WSL_DISTRIBUTION=Ubuntu
WINDOWS_WSL_PYTHON=/home/<Linux用户名>/.local/share/ChannelAgent/wsl-venv/bin/python
COMMAND_EXECUTION_ENVIRONMENT=sandbox
```

发行版默认值为 `Ubuntu`，Python 默认值为 `python3`；默认 Python 也必须安装项目依赖。改完重启应用。WSL 启动器使用受限环境，禁用 `.env` 加载，不继承用户 shell 配置或模型密钥。命令、环境探测和可启用的依赖准备使用原有 Linux 沙箱；依赖准备默认关闭，联网仍受配置、权限和确认约束。配置详见 [沙箱](sandbox.md)和[环境准备](environment-preparation.md)。

在 Windows 的项目目录执行实际隔离探针。

```powershell
uv run --no-sync python -m agent_test.scripts.ci_sandbox_probe
```

## 路径与功能边界

选择本地盘已有的项目目录，建议使用 NTFS。文件访问会拒绝重解析点（包括 junction）、备用数据流、UNC 和设备路径；路径或工作区身份不符合要求时操作会被拒绝。隔离执行还要求 Windows 路径能映射到所选 WSL 发行版并映射回原路径。

Windows 的 `local` 命令执行、托管后台命令任务和维护清理当前不可用；隔离环境或映射不可用时命令拒绝执行，不回退到宿主。文件工具与隔离命令仍受项目边界、权限策略、确认和审计约束。实际模型执行、已有轨迹回放和受控界面夹具属于不同验证，不能互相视为验收结果。其他平台的当前边界见[平台能力](platform-design.md)。

状态目录和审计使用当前用户与 SYSTEM 的私有 ACL，所有者显式设为当前用户；管理员身份启动也遵循同一规则。已有模型密钥文件出现非私有所有者或额外授权时拒绝读取，不能通过重新保存自动放宽权限。Windows 状态发布保留文件刷新和句柄重命名。Windows 没有与 POSIX 目录 `fsync` 完全相同的持久化保证，发布后刷新失败时需要重新读取磁盘记录确认结果。

## 历史验证范围（2026-11-07）

以下为历史验证，不能代表本轮单 exe 的实际验证。2026-11-07 在 Windows 原生 CPython 3.12 和 Ubuntu WSL 中验证。所有项目、会话与审计均使用独立临时目录。

- Windows 原生回归覆盖安全文件、项目持久化、会话复用、模型设置 ACL、Qt、Web、CLI 和上下文归档。新 Windows CI 运行同一类原生用例，真实 WSL 桥接测试通过环境变量显式启用。
- Windows 到 WSL 的实际测试包含项目写入、拒绝确认、取消后子进程收尾、外部硬链接和 junction 边界，以及原生审计写入失败后阻止执行。
- Chrome 中完成两节点 Flow，WSL 节点实际写文件，Windows 节点实际读取。另检查 416px 窄窗、父子 Agent 异常、会话树隔离和详情读取。
- Qt 在 Windows 离屏模式完成真实文件读取和两轮会话复用，检查宽窗、窄窗与系统中文字体。界面模型使用受控回调，本轮没有请求真实模型服务。

Linux 全量回归与真实隔离探针也须通过。测试入口见 [CI 配置](../.github/workflows/tests.yml)，最新执行结果在 [维护交接](session-handoff.md) 中记录。
