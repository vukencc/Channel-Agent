# 工具执行与文件任务

Agent 的默认提示词将文件操作视为执行任务：查看目录用 `list_files`，新建用 `create_file`，
读取用 `read_file`，修改前读取再 `update_file`，删除用 `delete_file`。
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
这不是面向不可信多租户的资源隔离服务：没有内存/磁盘配额，捕获输出后才截断显示。

普通命令通过 `shlex` 分词后直接执行，不解释 Shell 语法：

```text
python3 -c 'print(1 + 1)'
mkdir -p notes
sh -c 'ls -la | head'
```

需要管道、重定向或多个步骤时显式调用 `sh -c`；简单文件读写仍优先使用 CRUD。
`/bin/ls` 等绝对可执行路径指隔离环境内的路径，不是放行任意主机路径。

## 验证

```bash
uv run pytest dev/tests/test_command.py dev/tests/test_file_crud.py dev/tests/test_agent_tools.py -q
```

命令测试需要允许创建 Linux 用户、进程和网络命名空间；在禁止嵌套隔离的 CI/容器中需要调整运行器。
测试验证真实隔离边界、Python/Shell、确认取消、超时子进程回收及 CRUD 结果反馈。

本次验证：工程回归 95 项通过，3 项 RAG 集成/阈值相关测试未执行。
另外对当前配置的真实模型发送两条独立请求：保存 hello 到指定文件、读取指定文件，
分别返回 `create_file`、`read_file` 调用。该探测仅观察模型选择，不执行其调用；
不是跨模型统计评测，也不保证所有措辞下都能正确选择工具。
