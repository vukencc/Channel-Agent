"""在会话明确选择的沙箱或 Linux 本机环境中执行命令。

默认 Bubblewrap 隔离；本机模式须显式选择，隔离失败不会触发回退。
两种模式均沿用权限确认、根目录身份、超时、输出长度与审计。
"""
import subprocess
import os
import signal
import time
import selectors
import json
import math
from contextlib import nullcontext
from typing import Literal

from agent_core import config
from pydantic import BaseModel, Field

from agent_core.tools.base import register_tool
from agent_core.tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    check_command,
    check_workspace_quota,
    cancellation_requested,
    command_memory_limit_mb,
    execution_environment,
    isolated_command,
    sandbox_root,
    truncate,
)


class RunCommandArgs(BaseModel):
    """
    在会话选定的 sandbox/local 环境执行命令，支持 Python、Shell 等系统工具。
    sandbox 工作区映射为 /workspace，默认断网；local 使用真实 Linux 项目路径、运行时和本机网络含 localhost。
    local 不施加沙箱白名单或资源限额，仍有本机超时、输出上限和当前权限确认。
    简单文件增删改查优先用 CRUD 工具。命令自动发起执行确认，无需先口头询问。
    使用隔离环境内的 POSIX sh，支持管道、重定向和 heredoc 多行脚本。
    非交互运行，标准输入关闭；Python 多行代码使用 python3 - <<'PY' ... PY。
    """
    command: str = Field(
        description="要执行的命令，例如 ls -la、python3 -c 'print(1+1)' 或 sh -c 'ls | head'"
    )
    reason: str = Field(default="", description="说明执行目的，会显示在确认提示里")
    network: bool = Field(default=False, description="sandbox 中显式请求白名单 HTTPS 网络；local 使用本机网络及 localhost，此参数不关闭本机网络")
    runtime_profile: Literal['system', 'node'] = Field(default='system', description='仅 sandbox：普通命令用 system，已配置 Node 用 node；local 使用 Linux 本机运行时')
    resource_profile: Literal['standard', 'development'] = Field(
        default='standard', description='仅 sandbox：standard 使用普通命令额度，development 复用 ENVIRONMENT_PREPARE_* 额度并确认；local 使用本机资源')


def development_resource_limits() -> dict[str, int | float]:
    """冻结开发/安装共用的有限额度；零值分别继承通用命令配置。"""
    return {
        'network_max_bytes': config.ENVIRONMENT_PREPARE_NETWORK_BYTES or config.COMMAND_NETWORK_MAX_BYTES,
        'network_max_connections': config.ENVIRONMENT_PREPARE_NETWORK_CONNECTIONS or config.COMMAND_NETWORK_MAX_CONNECTIONS,
        'network_timeout_seconds': config.ENVIRONMENT_PREPARE_NETWORK_TIMEOUT or config.COMMAND_NETWORK_TIMEOUT,
        'file_limit_mb': config.ENVIRONMENT_PREPARE_FILE_MB or config.COMMAND_FILE_MB,
        'cpu_seconds': config.ENVIRONMENT_PREPARE_CPU_SECONDS or config.COMMAND_CPU_SECONDS,
    }


@register_tool(RunCommandArgs, name="run_command",
               timeout_s=max(config.COMMAND_TIMEOUT, config.ENVIRONMENT_PREPARE_TIMEOUT,
                             config.LOCAL_COMMAND_TIMEOUT) + config.CONFIRM_TIMEOUT + 10)
def run_command(command: str, reason: str = "", network: bool = False,
                runtime_profile: str = 'system', resource_profile: str = 'standard') -> str:
    """
    在会话选择的执行环境内运行一条命令。
    """
    try:
        if execution_environment() == 'local':
            return _run_local_command(command, reason, network, runtime_profile, resource_profile)
        if resource_profile not in {'standard', 'development'}:
            raise SandboxError('未知命令资源档位，仅支持 standard/development')
        limits = development_resource_limits() if resource_profile == 'development' else None
        timeout = config.ENVIRONMENT_PREPARE_TIMEOUT if limits is not None else config.COMMAND_TIMEOUT
        isolation_options = {'runtime_profile': runtime_profile}
        if limits is not None:
            isolation_options.update(file_limit_mb=limits['file_limit_mb'], cpu_seconds=limits['cpu_seconds'])
        proxy_options = ({'max_bytes': limits['network_max_bytes'],
                          'max_connections': limits['network_max_connections'],
                          'timeout': limits['network_timeout_seconds']} if limits is not None else {})
        network_proxy = None
        if network:
            from agent_core.tools.network_proxy import NetworkProxy, validate_network_config
            if config.COMMAND_NETWORK != 'allowlist':
                raise SandboxError('联网命令未启用')
            try:
                validate_network_config()
            except ValueError as exc:
                raise SandboxError(str(exc)) from exc
            # 代理构造只验证额度，确认通过后才创建套接字。
            network_proxy = NetworkProxy(**proxy_options)
        check_workspace_quota()
        argv = check_command(command)
        isolated = isolated_command(argv, **isolation_options)
    except (SandboxError, ValueError) as exc:
        audit("blocked", action="run_command", command=command, reason=str(exc))
        return f"[已拦截] {exc}"

    if limits is not None:
        detail = json.dumps({'command': command, 'resource_profile': resource_profile,
            'runtime_profile': runtime_profile, 'memory_limit_mb': command_memory_limit_mb(runtime_profile),
            'timeout_seconds': timeout, 'network': network,
            'domains': config.COMMAND_NETWORK_ALLOWLIST if network else [],
            **limits, 'process_limit': config.COMMAND_PROCESSES,
            'workspace_limit_mb': config.WORKSPACE_LIMIT_MB}, ensure_ascii=False)
    else:
        detail = command + (f'\n联网白名单：{config.COMMAND_NETWORK_ALLOWLIST}' if network else '')
    if runtime_profile != 'system' and limits is None:
        detail += f'\n运行时档位：{runtime_profile}，虚拟地址空间上限：{command_memory_limit_mb(runtime_profile)} MiB'
    if not ask_permission("run_command", detail, reason, command=command,
                          force_confirmation=network or limits is not None):
        return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"

    try:
        with network_proxy if network else nullcontext() as proxy:
            if proxy is not None:
                isolated = isolated_command(argv, network_socket=proxy.path, **isolation_options)
            execute_options = ({'timeout': timeout, 'file_limit_mb': limits['file_limit_mb']}
                               if limits is not None else {})
            audit('command_started', resource_profile=resource_profile, runtime_profile=runtime_profile,
                  timeout_seconds=timeout, network=network, **(limits or {}))
            result = _execute(command, reason, argv, isolated, **execute_options)
            result += _network_diagnostics(proxy)
            return result
    except (OSError, ValueError, SandboxError) as exc:
        audit('blocked', action='run_command', reason=str(exc))
        return f'[已拦截] 命令隔离初始化失败：{exc}'


def _run_local_command(command: str, reason: str, network: bool,
                       runtime_profile: str, resource_profile: str) -> str:
    """用户显式选择本机后执行；绝不由隔离失败触发此分支。"""
    from agent_core.tools.execution_environment import (
        local_process_environment, validate_local_execution,
    )
    try:
        if runtime_profile not in {'system', 'node'} or resource_profile not in {'standard', 'development'}:
            raise SandboxError('未知命令运行时或资源档位')
        root = validate_local_execution()
        argv = check_command(command)
        timeout = config.LOCAL_COMMAND_TIMEOUT
        plan = json.dumps({'command': command, 'execution_environment': 'local',
            'working_directory': str(root), 'timeout_seconds': timeout,
            'network': '本机网络，包含 localhost；network 参数只控制 sandbox',
            'runtime_profile': runtime_profile, 'resource_profile': resource_profile,
            'resources': '本机资源，不施加 sandbox 的内存/CPU/文件/工作区限额'}, ensure_ascii=False)
        if not ask_permission('run_command', plan, reason, command=command, force_confirmation=True):
            return '[已取消] 用户未确认（拒绝或确认超时），命令未执行。'
        validate_local_execution()
        audit('command_started', execution_environment='local', timeout_seconds=timeout,
              working_directory=str(root), network='host')
        return _execute(command, reason, argv, argv, timeout=timeout,
                        environment=local_process_environment(), check_quota=False,
                        execution_mode='local')
    except (OSError, ValueError, SandboxError) as exc:
        audit('blocked', action='run_command', execution_environment='local', reason=str(exc))
        return f'[已拦截] {exc}'


def _network_diagnostics(proxy) -> str:
    """Append only local boundary evidence, including for persisted command jobs."""
    summary_fn = getattr(proxy, 'denial_summary', None)
    summary = summary_fn() if summary_fn is not None else ''
    if not summary:
        return ''
    advice = (' 构建/测试可用 run_command 的 resource_profile="development" 申请已配置的开发额度；'
              '不要据此改写依赖版本或绕过依赖解析。')
    return '\n[网络边界诊断] ' + summary + '；这是本地网关拒绝，不代表包仓库或依赖不存在。' + advice


def _execute(command, reason, argv, isolated, *, timeout=None, on_output=None,
             file_limit_mb=None, environment=None, check_quota=True,
             execution_mode='sandbox') -> str:
    """共享执行器；调用方必须先完成确认。后台任务也使用相同配额及进程组清理。"""
    timeout = config.COMMAND_TIMEOUT if timeout is None else timeout
    file_limit_mb = config.COMMAND_FILE_MB if file_limit_mb is None else file_limit_mb

    try:
        if not math.isfinite(timeout) or timeout <= 0:
            raise SandboxError('命令超时必须为有限正数')
        if check_quota:
            check_workspace_quota()
        # 审批期间根目录被替换时仍关闭执行；本机模式也不能悄悄跟随新路径。
        root = sandbox_root()
        process_options = {'env': environment} if environment is not None else {}
        tree = None
        proc = subprocess.Popen(
            isolated,
            shell=False,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            **process_options,
        )
        try:
            # Popen 成功后立即进入清理范围，/proc 追踪初始化失败也不能泄漏宿主进程。
            if execution_mode == 'local':
                from agent_core.tools.execution_environment import LocalProcessTree
                tree = LocalProcessTree(proc.pid)
            deadline = time.monotonic() + timeout
            next_quota_check = time.monotonic() + config.COMMAND_QUOTA_INTERVAL
            buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
            clipped = set()
            # 持续排空两个管道，只保留有限的 UTF-8 前缀。
            limit = max(1, config.TOOL_MAX_OUTPUT) * 4
            with selectors.DefaultSelector() as selector:
                for pipe in buffers:
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map() or proc.poll() is None:
                    if tree is not None:
                        tree.update()
                    if cancellation_requested():
                        audit("cancelled", action="run_command", command=command)
                        return ('[已取消] 命令已终止，已尝试回收本机子进程。'
                                if tree is not None else '[已取消] 命令及其子进程已终止。')
                    if check_quota and time.monotonic() >= next_quota_check:
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
            # 后代追踪尽力完成；失败不能覆盖取消/超时结果或跳过基础资源回收。
            try:
                if tree is not None:
                    try:
                        tree.terminate()
                    except OSError as exc:
                        audit('process_tree_cleanup_failed', action='run_command',
                              execution_environment=execution_mode, error=type(exc).__name__)
            finally:
                # 即使追踪器被 KeyboardInterrupt/SystemExit 打断，也回收后再原样传播。
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    try:
                        proc.wait()
                    finally:
                        try:
                            proc.stdout.close()
                        finally:
                            proc.stderr.close()
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
        if check_quota:
            check_workspace_quota()
    except SandboxError as exc:
        audit("quota_exceeded", action="run_command", reason=str(exc))
        return f"[配额超限] {exc}；已保留文件，请检查部分输出。"
    audit("executed", action="run_command", command=command,
          exit_code=proc.returncode, reason=reason, execution_environment=execution_mode)

    stdout = truncate(stdout)
    stderr = truncate(stderr)

    parts = [f"[退出码] {proc.returncode}"]
    parts.append(f"[stdout]\n{stdout}" if stdout else "[stdout] （空）")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if execution_mode == 'sandbox' and (proc.returncode == -getattr(signal, 'SIGXFSZ', 25)
            or any(term in (stdout + '\n' + stderr).lower()
                   for term in ('efbig', 'file too large', 'file size limit exceeded'))):
        parts.append(f'[资源边界诊断] 单个文件大小上限为 {file_limit_mb} MiB（RLIMIT_FSIZE），'
                     '这是本地文件配额证据，不能据此判定依赖冲突或仓库故障。'
                     ' 构建/测试可用 run_command 的 resource_profile="development" 申请已配置并确认的开发额度。')
    if proc.returncode == 127 and execution_mode == 'local':
        parts.append('[环境诊断] Linux 本机 PATH 中未找到命令；先调用 inspect_environment 检查实际运行时。')
    elif proc.returncode == 127:
        parts.append('[环境诊断] 命令在沙箱内未找到，不代表宿主未安装。先调用 inspect_environment；依赖缺失可用 prepare_project_environment，不要将静态检查报告为完整测试。')
    elif execution_mode == 'sandbox' and any(term in stderr.lower() for term in ('failed to reserve virtual memory', 'fatal process out of memory', 'segmentedtable::initializetable')):
        parts.append('[环境诊断] 运行时已找到，但虚拟地址空间不足。Node 可使用显式配置的 runtime_profile="node"；不得取消资源限制。')
    return "\n".join(parts)
