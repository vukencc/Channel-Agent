"""沙箱内的命令执行工具。

Bubblewrap 隔离主机文件与网络，工作目录 /workspace 映射沙箱目录；
执行前确认，限制超时与输出长度。隔离不可用时不退回主机执行。
"""
import subprocess
import os
import signal
import time
import selectors
from contextlib import nullcontext
from typing import Literal

from ai_agent_startup import config
from pydantic import BaseModel, Field

from ai_agent_startup.tools.base import register_tool
from ai_agent_startup.tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    check_command,
    check_workspace_quota,
    cancellation_requested,
    command_memory_limit_mb,
    isolated_command,
    sandbox_root,
    truncate,
)


class RunCommandArgs(BaseModel):
    """
    在隔离工作区执行命令，支持 Python、Shell、mkdir/cp/mv/rm 等系统工具。
    主机仅沙箱目录映射为 /workspace 并可写；网络关闭，主机密钥和虚拟环境不可见。
    简单文件增删改查优先用 CRUD 工具。命令自动发起执行确认，无需先口头询问。
    使用隔离环境内的 POSIX sh，支持管道、重定向和 heredoc 多行脚本。
    非交互运行，标准输入关闭；Python 多行代码使用 python3 - <<'PY' ... PY。
    """
    command: str = Field(
        description="要执行的命令，例如 ls -la、python3 -c 'print(1+1)' 或 sh -c 'ls | head'"
    )
    reason: str = Field(default="", description="说明执行目的，会显示在确认提示里")
    network: bool = Field(default=False, description="显式请求白名单 HTTPS 网络，遵循当前权限模式；默认断网")
    runtime_profile: Literal['system', 'node'] = Field(default='system', description='普通命令用 system；已配置 Node 的 JS/npm/build/test 用 node，仍有资源上限')


@register_tool(RunCommandArgs, name="run_command")
def run_command(command: str, reason: str = "", network: bool = False,
                runtime_profile: str = 'system') -> str:
    """
    在沙箱目录内执行一条命令。
    """
    try:
        if network:
            from ai_agent_startup.tools.network_proxy import validate_network_config
            if config.COMMAND_NETWORK != 'allowlist':
                raise SandboxError('联网命令未启用')
            try:
                validate_network_config()
            except ValueError as exc:
                raise SandboxError(str(exc)) from exc
        check_workspace_quota()
        argv = check_command(command)
        isolated = isolated_command(argv, runtime_profile=runtime_profile)
    except SandboxError as exc:
        audit("blocked", action="run_command", command=command, reason=str(exc))
        return f"[已拦截] {exc}"

    detail = command + (f'\n联网白名单：{config.COMMAND_NETWORK_ALLOWLIST}' if network else '')
    if runtime_profile != 'system':
        detail += f'\n运行时档位：{runtime_profile}，虚拟地址空间上限：{command_memory_limit_mb(runtime_profile)} MiB'
    if not ask_permission("run_command", detail, reason, command=command, force_confirmation=network):
        return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"

    from ai_agent_startup.tools.network_proxy import NetworkProxy
    try:
        with NetworkProxy() if network else nullcontext() as proxy:
            if proxy is not None:
                isolated = isolated_command(argv, network_socket=proxy.path, runtime_profile=runtime_profile)
            result = _execute(command, reason, argv, isolated)
            result += _network_diagnostics(proxy)
            return result
    except (OSError, ValueError, SandboxError) as exc:
        audit('blocked', action='run_command', reason=str(exc))
        return f'[已拦截] 命令隔离初始化失败：{exc}'


def _network_diagnostics(proxy) -> str:
    """Append only local boundary evidence, including for persisted command jobs."""
    summary_fn = getattr(proxy, 'denial_summary', None)
    summary = summary_fn() if summary_fn is not None else ''
    if not summary:
        return ''
    advice = (' 依赖安装可调用 prepare_project_environment 申请已配置的安装资源档位；'
              '不要据此改写依赖版本或绕过依赖解析。') if config.ENABLE_ENVIRONMENT_PREPARE else ''
    return '\n[网络边界诊断] ' + summary + '；这是本地网关拒绝，不代表包仓库或依赖不存在。' + advice


def _execute(command, reason, argv, isolated, *, timeout=None, on_output=None,
             file_limit_mb=None) -> str:
    """共享执行器；调用方必须先完成确认。后台任务也使用相同配额及进程组清理。"""
    timeout = config.COMMAND_TIMEOUT if timeout is None else timeout
    file_limit_mb = config.COMMAND_FILE_MB if file_limit_mb is None else file_limit_mb

    try:
        check_workspace_quota()
        proc = subprocess.Popen(
            isolated,
            shell=False,
            cwd=sandbox_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        next_quota_check = time.monotonic() + config.COMMAND_QUOTA_INTERVAL
        buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
        clipped = set()
        # Drain both pipes continuously, retaining only a bounded UTF-8 prefix.
        limit = max(1, config.TOOL_MAX_OUTPUT) * 4
        try:
            with selectors.DefaultSelector() as selector:
                for pipe in buffers:
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map() or proc.poll() is None:
                    if cancellation_requested():
                        audit("cancelled", action="run_command", command=command)
                        return "[已取消] 命令及其子进程已终止。"
                    if time.monotonic() >= next_quota_check:
                        check_workspace_quota()
                        next_quota_check = time.monotonic() + config.COMMAND_QUOTA_INTERVAL
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(isolated, timeout)
                    for key, _ in selector.select(min(.1, remaining)):
                        data = os.read(key.fileobj.fileno(), 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if on_output is not None:
                            on_output(data)
                        buffer = buffers[key.fileobj]
                        room = max(0, limit - len(buffer))
                        buffer.extend(data[:room])
                        if len(data) > room:
                            clipped.add(key.fileobj)
            stdout, stderr = [
                truncate(data.decode('utf-8', errors='replace'))
                + ('\n[输出已截断]' if pipe in clipped else '')
                for pipe, data in buffers.items()
            ]
        finally:
            # Also reap descendants holding a pipe after their parent exits.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for pipe in buffers:
                pipe.close()
    except SandboxError as exc:
        audit("blocked", action="run_command", command=command, reason=str(exc))
        return f"[已拦截] {exc}"
    except subprocess.TimeoutExpired:
        audit("timeout", action="run_command", command=command,
              timeout=timeout)
        return f"[超时] 命令超过 {timeout:g} 秒未结束，已终止。请检查死循环或拆分长任务。"
    except FileNotFoundError:
        audit("failed", action="run_command", command=command, error="命令不存在")
        return f"[失败] 找不到命令：{argv[0]}"
    except OSError as exc:
        audit("failed", action="run_command", command=command, error=str(exc))
        return f"[失败] 命令无法执行：{exc}"

    try:
        check_workspace_quota()
    except SandboxError as exc:
        audit("quota_exceeded", action="run_command", reason=str(exc))
        return f"[配额超限] {exc}；已保留文件，请检查部分输出。"
    audit("executed", action="run_command", command=command,
          exit_code=proc.returncode, reason=reason)

    stdout = truncate(stdout)
    stderr = truncate(stderr)

    parts = [f"[退出码] {proc.returncode}"]
    parts.append(f"[stdout]\n{stdout}" if stdout else "[stdout] （空）")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if (proc.returncode == -getattr(signal, 'SIGXFSZ', 25)
            or any(term in (stdout + '\n' + stderr).lower()
                   for term in ('efbig', 'file too large', 'file size limit exceeded'))):
        parts.append(f'[资源边界诊断] 单个文件大小上限为 {file_limit_mb} MiB（RLIMIT_FSIZE），'
                     '这是本地文件配额证据，不能据此判定依赖冲突或仓库故障。'
                     + (' 依赖安装可调用 prepare_project_environment 使用已配置并确认的安装档位。'
                        if config.ENABLE_ENVIRONMENT_PREPARE else ''))
    if proc.returncode == 127:
        parts.append('[环境诊断] 命令在沙箱内未找到，不代表宿主未安装。先调用 inspect_environment；依赖缺失可用 prepare_project_environment，不要将静态检查报告为完整测试。')
    elif any(term in stderr.lower() for term in ('failed to reserve virtual memory', 'fatal process out of memory', 'segmentedtable::initializetable')):
        parts.append('[环境诊断] 运行时已找到，但虚拟地址空间不足。Node 可使用显式配置的 runtime_profile="node"；不得取消资源限制。')
    return "\n".join(parts)
