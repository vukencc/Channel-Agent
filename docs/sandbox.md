# 工具执行与文件任务

## 命令执行环境

命令执行环境可在应用配置 `COMMAND_EXECUTION_ENVIRONMENT=sandbox|local` 中设置全局默认值（默认 `sandbox`），也可按根会话在新建会话/会话设置中选择；CLI 使用 `/environment`、`/environment sandbox`、`/environment local` 或 `/environment default`。`default` 恢复应用默认值。根会话所属项目的 Agent、工具与后台任务全部空闲后才能切换；子 Agent 始终跟随根会话选择，手动分支继承分支创建时来源的有效环境。模型工具不能自行选择或提升到 `local`，失败也不会自动改用另一环境。

`local` 仅支持 Linux，命令由 Linux 宿主直接执行，可访问宿主文件、已安装工具、网络和 localhost 服务。它不使用 Bubblewrap、网络 allowlist、prlimit 或工作区配额，也不受 `ENABLE_ENVIRONMENT_PREPARE` 和 `ENABLE_COMMAND_JOBS` 开关约束；本机模式下后台命令工具会注册，但仍须在会话工具子集中可用。因此不能获得沙箱提供的隔离保证。安装 JDK/Maven 等操作仍须 Agent 实际执行，选择本机模式不会自动安装工具。

本机执行仍保留已选择的权限策略与确认、会话工具身份和审计、命令 cwd 的工作区根身份校验、输出保留上限、墙钟超时、取消和进程树清理。文件工具继续限制相对路径在已选工作区内；本机命令可以访问同一 Linux 用户有权访问的其他宿主路径。子进程使用最小环境，不继承模型 API 密钥；会保留标准大小写形式的 HTTP/HTTPS/SOCKS 代理设置，并合并已有代理绕过项和 localhost 地址，以支持本机代理及开发服务直连。后台 local job 的整个生命周期不持有工作区锁，因此可启动服务后再调用其他工具；由此需自行处理本机程序对项目文件的并发修改。`LOCAL_COMMAND_TIMEOUT` 默认 `600` 秒，允许 `1..86400` 秒；普通 `sandbox` 命令继续使用原沙箱时限和限制。

本机工作区的 `mkdir`、`move`、`copy` 也不使用沙箱工作区配额，避免安装工具链后无法整理项目文件；仍保留路径边界、权限确认、根与源文件身份复核、取消、不覆盖目标和原子发布。沙箱环境的配额检查保持不变。本机进程树回收属于尽力清理；即使 `/proc` 追踪初始化或清理失败，也会执行进程组终止、等待和管道关闭，但不提供恶意守护进程隔离保证。

Agent 的默认提示词将文件操作视为执行任务：查看目录用 `list_files`，新建用 `create_file`，
读取用 `read_file`，修改前读取再 `edit_file`，删除用 `delete_file`。
路径相对于当前根会话工作区 `SANDBOX_DIR/<根会话 ID>/`（默认 `crud_tests/<根会话 ID>/`），不添加 `crud_tests/` 前缀。
Linux 用户也可在会话设置或 CLI `/project /home/user/project` 中选择已有项目目录；这时相对路径以所选目录为根，命令映射仍为 `/workspace`。子 Agent 继承父根目录，模型不能通过工具改变这项授权。切换不移动文件，删除会话不删除外部项目；目录身份变化后拒绝访问并要求用户重新选择。
写入、删除和命令执行按当前权限模式处理确认：`smart` 可按规则自动批准低风险操作，`full_access` 自动批准，`readonly` 拒绝写入；需要确认但被拒绝或超时后停止，不换工具绕过。详见 [CLI 命令与权限模式](cli-command-modes.md)。
工具结果决定是否报告成功。提示词改善默认行为，但不保证每个模型每次都选择正确工具。

## 命令隔离

Linux 上需要系统安装 **Bubblewrap**（`bwrap`，Debian/Ubuntu 包名 `bubblewrap`）。
Python 依赖仍由 `uv sync --locked` 安装。没有 bwrap、命名空间受系统策略限制或隔离启动失败时，
命令不会退回主机执行；CRUD 工具仍可独立使用。

`run_command` 不再按程序名禁止 Python、Shell、rm、mkdir、cp、mv 等命令：

- 只有当前根会话工作区（默认 `SANDBOX_DIR/<根会话 ID>/`，或用户所选 Linux 项目目录）映射到可写的 `/workspace`；系统 `/usr` 和运行库只读。
- 独立网络、PID 等命名空间，移除 capabilities，清空继承环境变量。
- 不挂载主机家目录、项目目录、密钥、项目 `.venv` 或宿主 `/etc`。
- `/tmp`、`/dev`、`/proc` 使用隔离实例；Shell 子进程超时后一起终止。
- 仍保留当前权限模式的确认规则、超时、输出展示截断和审计。

命令使用系统安装的工具；不自动带入项目第三方 Python 包。沙箱网络默认关闭；只有显式启用 `network=true`，并将 `COMMAND_NETWORK=allowlist` 配置为允许列表模式、目标符合白名单且通过当前权限/确认与沙箱条件时，网络访问才可能放行。本机环境直接使用宿主网络，`network` 参数只影响沙箱。详见 [环境准备](environment-preparation.md)。沙箱内文件删除仍真实生效，执行前应核对当前模式下的确认提示。
这不是面向不可信多租户的资源隔离服务：命令继承 prlimit 的单进程内存、CPU、单文件尺寸和按 UID 进程数限制；stdout/stderr 持续排空，但仅保留受限前缀，避免大输出撑满 Agent 内存。

命令由隔离环境内的 `/bin/sh -c` 执行，宿主仍以 `shell=False` 启动 Bubblewrap。支持 POSIX Shell 语法：

```text
python3 -c 'print(1 + 1)'
mkdir -p notes
ls -la | head
```

支持管道、重定向、多个步骤和 heredoc；简单文件读写仍优先使用 CRUD。
命令的标准输入连接 `/dev/null`，不会抢占 CLI 键盘输入。多行 Python 使用 `python3 - <<'PY'` 加换行脚本与结尾 `PY`，而不是等待交互输入。
`/bin/ls` 等绝对可执行路径指隔离环境内的路径，不是放行任意主机路径。

## 验证

```bash
uv run pytest src/agent_test/tests/test_command.py src/agent_test/tests/test_file_crud.py src/agent_test/tests/test_agent_tools.py -q
```

命令测试需要允许创建 Linux 用户、进程和网络命名空间；在禁止嵌套隔离的 CI/容器中需要调整运行器。
测试验证真实隔离边界、Python/Shell、确认取消、超时子进程回收及 CRUD 结果反馈。

2026-09-27 的[系统诊断报告](system-diagnostics-2026-09-27.md)是历史验证记录；最新验证见[新会话交接说明](session-handoff.md)。

## 长文件读取与修改

`read_file(path, offset=0, limit=6000, search="")` 按字符分页。返回 `next_offset` 时应继续读取所需部分；`EOF` 表示末页。
偏移不是 UTF-8 字节数，换行保持原样。可用 `search="</style>"` 或函数/选择器名称直接定位，从匹配处返回内容，避免猜测偏移。
小文件首次完整读取仍返回原文；分页输出有 `[内容开始]` / `[内容结束]` 边界，它们不是文件内容。

`edit_file(path, old_text, new_text, reason)` 只替换一个唯一匹配的片段。必须先读取真实原文；空锚点、不匹配或多处匹配均拒绝。
插入时把唯一相邻文本保留在新片段中；删除时传空 `new_text`。写入按当前权限模式处理确认；确认期间文件改变会拒绝操作。
替换通过同目录临时文件、fsync 和原子替换完成；失败不留下半写文件，其他内容与 CRLF 换行保留。

`update_file` 只用于不超过 `FILE_READ_CHARS` 的小文件。大文件全文覆盖会被拦截，改用 `edit_file` 分步修改，避免拿单页内容覆盖全文。
新建较大项目时可分离 HTML/CSS 等资源，单步生成较小内容。命令工具保持可用，执行隔离规则不变。

## 资源限制

执行前探测 prlimit 支持，失败即拒绝；默认单进程地址空间 512 MiB、CPU 5 秒、单文件 32 MiB、同 UID 进程数 128。
工作区默认 512 MiB，执行前、执行中约 100 ms 一次及结束后检查，不删除超额文件。命令总时限仍有效。
这不是文件系统硬配额：轮询存在超量窗口，RLIMIT_AS/CPU 按进程计，RLIMIT_NPROC 按宿主 UID 计且不限制 root。
可信单用户环境可用这些兜底；不可信多租户部署仍需 cgroup 和独立配额卷，不应以本配置宣称硬隔离。

`run_command(resource_profile="development")` 可显式申请已配置的安装/开发额度，用于构建和测试，详见[环境准备](environment-preparation.md)。不会自动取消内存、CPU、文件、网络或工作区上限；默认 `standard` 档位不变。

长输出可先 `create_file`，再 `append_file(path, content, expected_chars)` 分段追加，每段最多 4000 字符。偏移是文件字符数；每段按当前权限模式处理确认并原子替换，返回 next_offset。重复旧偏移会拒绝，确认期间变化也拒绝。不完整模型工具参数仍不执行。此协议支持长输出，但完成速度和实际拆分行为仍取决于模型。
