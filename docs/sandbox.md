# 工具执行与文件任务

Agent 的默认提示词将文件操作视为执行任务：查看目录用 `list_files`，新建用 `create_file`，
读取用 `read_file`，修改前读取再 `edit_file`，删除用 `delete_file`。
路径相对于 `SANDBOX_DIR`，不添加 `crud_tests/` 前缀。
写入、删除和命令执行直接发起工具调用，由工具提示确认；拒绝或超时后停止，不换工具绕过。
工具结果决定是否报告成功。提示词改善默认行为，但不保证每个模型每次都选择正确工具。

## 命令隔离

Linux 上需要系统安装 **Bubblewrap**（`bwrap`，Debian/Ubuntu 包名 `bubblewrap`）。
Python 依赖仍由 `uv sync --locked` 安装。没有 bwrap、命名空间受系统策略限制或隔离启动失败时，
命令不会退回主机执行；CRUD 工具仍可独立使用。

`run_command` 不再按程序名禁止 Python、Shell、rm、mkdir、cp、mv 等命令：

- 只有 `SANDBOX_DIR` 映射到可写的 `/workspace`；系统 `/usr` 和运行库只读。
- 独立网络、PID 等命名空间，移除 capabilities，清空继承环境变量。
- 不挂载主机家目录、项目目录、密钥、项目 `.venv` 或宿主 `/etc`。
- `/tmp`、`/dev`、`/proc` 使用隔离实例；Shell 子进程超时后一起终止。
- 仍保留每次命令的用户确认、超时、输出展示截断和审计。

命令使用系统安装的工具；不自动带入项目第三方 Python 包。网络关闭，因此 `curl` 等即使系统已安装，
也不能访问外部网络。沙箱内文件删除仍真实生效，执行前应核对确认提示。
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
uv run pytest dev/tests/test_command.py dev/tests/test_file_crud.py dev/tests/test_agent_tools.py -q
```

命令测试需要允许创建 Linux 用户、进程和网络命名空间；在禁止嵌套隔离的 CI/容器中需要调整运行器。
测试验证真实隔离边界、Python/Shell、确认取消、超时子进程回收及 CRUD 结果反馈。

最新验证与失败样本见 [系统诊断报告](system-diagnostics-2026-09-27.md)。

## 长文件读取与修改

`read_file(path, offset=0, limit=6000, search="")` 按字符分页。返回 `next_offset` 时应继续读取所需部分；`EOF` 表示末页。
偏移不是 UTF-8 字节数，换行保持原样。可用 `search="</style>"` 或函数/选择器名称直接定位，从匹配处返回内容，避免猜测偏移。
小文件首次完整读取仍返回原文；分页输出有 `[内容开始]` / `[内容结束]` 边界，它们不是文件内容。

`edit_file(path, old_text, new_text, reason)` 只替换一个唯一匹配的片段。必须先读取真实原文；空锚点、不匹配或多处匹配均拒绝。
插入时把唯一相邻文本保留在新片段中；删除时传空 `new_text`。写入仍需确认，确认期间文件改变会拒绝操作。
替换通过同目录临时文件、fsync 和原子替换完成；失败不留下半写文件，其他内容与 CRLF 换行保留。

`update_file` 只用于不超过 `FILE_READ_CHARS` 的小文件。大文件全文覆盖会被拦截，改用 `edit_file` 分步修改，避免拿单页内容覆盖全文。
新建较大项目时可分离 HTML/CSS 等资源，单步生成较小内容。命令工具保持可用，执行隔离规则不变。

## 资源限制

执行前探测 prlimit 支持，失败即拒绝；默认单进程地址空间 512 MiB、CPU 5 秒、单文件 32 MiB、同 UID 进程数 128。
工作区默认 512 MiB，执行前、执行中约 100 ms 一次及结束后检查，不删除超额文件。命令总时限仍有效。
这不是文件系统硬配额：轮询存在超量窗口，RLIMIT_AS/CPU 按进程计，RLIMIT_NPROC 按宿主 UID 计且不限制 root。
可信单用户环境可用这些兜底；不可信多租户部署仍需 cgroup 和独立配额卷，不应以本配置宣称硬隔离。
