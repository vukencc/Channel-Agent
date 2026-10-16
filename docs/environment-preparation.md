# 隔离命令环境准备

当 `run_command` 在沙箱里报告找不到运行时或项目依赖时，可先用 `inspect_environment` 检查沙箱实际可见的 Node、npm、Python、uv、Java 和 Maven。它只运行固定版本/算术探测，不执行项目脚本；宿主候选路径只供排查，不能证明沙箱可运行。错误和受资源限制的结果应如实处理，不能把静态检查或依赖安装当作构建、测试已经通过。

`prepare_project_environment` 可按工作区项目中的 manifest/lockfile 准备 npm 或 uv 依赖。它默认关闭；只有显式设置 `ENABLE_ENVIRONMENT_PREPARE=true` 后才可调用。操作会显示权限计划并按当前权限策略请求确认，成功结果仍要求再用 `run_command` 实际运行项目构建和测试。

## 最短调用流程

先诊断：

```text
inspect_environment()
```

若项目已有 npm lockfile，准备 Node 依赖（`project_dir` 是工作区相对路径）：

```text
prepare_project_environment(manager="npm", project_dir="frontend", reason="准备前端依赖以运行项目测试")
```

若需要从白名单联网下载，必须明确申请：

```text
prepare_project_environment(manager="npm", project_dir="frontend", network=true, reason="从已配置的 HTTPS 域名白名单下载锁定依赖")
```

随后使用 `run_command` 在 `frontend` 中实际运行项目声明的 build/test 命令；需要 Node 时指定 `runtime_profile="node"`。准备工具不会替项目猜测或执行测试命令。

## 配置

| 配置项 | 默认值 | 作用与约束 |
| --- | --- | --- |
| `ENABLE_ENVIRONMENT_PREPARE` | `false` | 总开关。关闭时 `prepare_project_environment` 明确拒绝；只读 `inspect_environment` 不受此开关影响。 |
| `ENVIRONMENT_PREPARE_TIMEOUT` | `120` | 依赖准备执行超时，范围 `1..600` 秒。 |
| `ENVIRONMENT_PREPARE_NETWORK_BYTES` | `0` | 联网准备的总传输字节上限；`0` 继承 `COMMAND_NETWORK_MAX_BYTES`（默认 8 MiB），正数可单独配置，最大 1 GiB。 |
| `ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS` | `0` | 单次命令生命周期内允许的累计 CONNECT 连接数（不是并发数）；`0` 继承 `COMMAND_NETWORK_MAX_CONNECTIONS`（默认 4），正数可单独配置，最大 256。 |
| `ENVIRONMENT_PREPARE_NETWORK_TIMEOUT` | `0` | 每条已建立 CONNECT 隧道的绝对生命周期上限（秒），不是每个 HTTP 请求的时限；`0` 继承 `COMMAND_NETWORK_TIMEOUT`（默认 5 秒），正数可单独配置，最大 600 秒。 |
| `ENVIRONMENT_PREPARE_FILE_MB` | `0` | 安装写入单文件的 MiB 上限；`0` 继承 `COMMAND_FILE_MB`（默认 32 MiB），正数可单独配置，最大 1024 MiB。 |
| `ENVIRONMENT_PREPARE_CPU_SECONDS` | `0` | 安装进程的 CPU 秒数上限；`0` 继承 `COMMAND_CPU_SECONDS`（默认 5 秒），正数可单独配置，最大 3600 秒。 |
| `COMMAND_RUNTIME_PATHS` | `{}` | JSON 对象，只支持 `node` 和 `uv`。`node` 值是 Linux Node 发行目录前缀，目录须含 `bin/node` 与 `lib/node_modules/npm`；`uv` 值是独立 Linux ELF 可执行文件路径。绑定只读挂载到沙箱固定位置。 |
| `COMMAND_NODE_MEMORY_MB` | `0` | Node profile 的虚拟地址空间上限，范围 `1..8192` MiB；`0` 禁用 Node profile。启用须同时配置 `COMMAND_RUNTIME_PATHS.node`。 |

示例（路径仅示意，须替换为当前 Linux/WSL2 中真实、可读且安全的安装路径）：

```dotenv
COMMAND_RUNTIME_PATHS={"node":"/opt/node-v24","uv":"/opt/uv/uv"}
COMMAND_NODE_MEMORY_MB=2048
ENABLE_ENVIRONMENT_PREPARE=false
ENVIRONMENT_PREPARE_TIMEOUT=120
ENVIRONMENT_PREPARE_NETWORK_BYTES=0
ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS=0
ENVIRONMENT_PREPARE_NETWORK_TIMEOUT=0
ENVIRONMENT_PREPARE_FILE_MB=0
ENVIRONMENT_PREPARE_CPU_SECONDS=0
```

`COMMAND_MEMORY_MB=512` 的 system profile 保持不变。Node profile 是显式的独立限额，不会取消 CPU、文件大小、进程数、工作区配额或超时限制。当前一组 Node 24 环境实测中，512 MiB 与 1024 MiB 虚拟地址空间无法启动，2048 MiB 可启动；这是虚拟地址空间限制（`RLIMIT_AS`），不是进程 RSS 实测值，也不保证其他系统、Node 发行版或项目一定适用。

## 安装行为

- npm 需要项目 `package.json`，有 `package-lock.json` 或 `npm-shrinkwrap.json` 时执行 `npm ci`，否则执行 `npm install`。默认加 `--ignore-scripts`、使用项目内 `.agent-env/npm-cache` 并离线运行；只有显式 `allow_scripts=true` 才允许 npm 生命周期脚本。CLI 固定本地模式、安装前缀和日志位置，防止项目 `.npmrc` 重定向到其他项目；清单、锁文件和 `.npmrc` 的摘要均列入确认计划并在执行前复核。
- uv 优先使用 `pyproject.toml` 与 `uv.lock`，执行 locked、wheel-only、no-install-project 的同步；没有 `pyproject.toml` 时使用 `requirements.txt` 和 wheel-only 安装。uv 不允许启用源码构建脚本。
- Python 环境放在项目 `.agent-env/python`，缓存位于 `.agent-env/uv-cache`。不会覆盖既有 `.venv`；目标环境已存在但不是本工具标记的受管环境时会拒绝覆盖。
- uv 安装/下载并发固定为 1，避免高核数 WSL 在 512 MiB 地址空间下为安装线程耗尽内存；普通命令额度不增加。该设置采用 [uv 官方并发配置](https://docs.astral.sh/uv/reference/environment/)。项目声明的源仍须满足 wheel-only 和网络白名单；需要源码构建的依赖会明确失败，不能改称测试通过。
- 工具只支持 Node/npm 与 uv/Python 这两类项目依赖准备。不会自动下载或安装 JDK、Maven、操作系统包或其他运行时；需由维护者配置安全的 Linux 运行时绑定或另行准备环境。它不会把宿主 home、Windows 程序目录或整个用户目录挂进沙箱。
- 安装依赖不等于运行项目 build/test。准备结果末尾会提示继续调用 `run_command`。若失败或超时，可能已经写入部分依赖，不会自动删除项目文件；请检查输出和工作区配额后再决定如何处理。

## 网络与权限

默认 `network=false`，命令在隔离网络命名空间内运行。联网准备必须同时满足 `COMMAND_NETWORK=allowlist`、配置精确 HTTPS 域名白名单，并在工具调用中明确设置 `network=true`；代理仍受安装专用累计连接数、字节数、隧道生命周期时限和工作区配额限制，值为 `0` 时各自继承对应通用命令限额；白名单配置不保证上游可用或安装必然成功。联网请求会进入权限计划并强制确认；`readonly` 会拒绝，`full_access` 会按既有权限策略自动批准，其他模式依现行规则处理。

Node 与 uv 绑定会校验 Linux ELF 类型及架构、拒绝符号链接和私有/受保护路径，并以只读方式挂入固定沙箱位置。工作区是唯一可写挂载；宿主凭据、状态目录、审计文件和 `.env` 不会因运行时绑定而暴露。环境准备仍走既有命令沙箱、确认、审计和配额路径，不扩展项目工具权限。
