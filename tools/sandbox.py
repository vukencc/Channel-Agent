"""工具沙箱：路径约束、风险确认、命令隔离、审计日志。

安全原则（fail-closed）：任何异常、无法确认、用户拒绝、确认超时，一律按「不允许」处理。
"""
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event

import datetime
import json
import os
import select
import sys
from pathlib import Path
from typing import Callable
from functools import lru_cache
import time

import config
from core.log import get_logger
from core.audit_writer import append_audit

logger = get_logger(__name__)
_audit_encoder = json.JSONEncoder(ensure_ascii=False)
_audit_keys = frozenset({'session_id', 'turn_id', 'tool_call_id', 'ts', 'event'})


@lru_cache(maxsize=64)
def _audit_second(second):
    return datetime.datetime.fromtimestamp(second).isoformat(timespec='seconds')


@lru_cache(maxsize=64)
def _audit_prefix(session_id, turn_id, tool_call_id, stamp, event):
    return _audit_encoder.encode({'session_id': session_id, 'turn_id': turn_id,
        'tool_call_id': tool_call_id, 'ts': stamp, 'event': event})

# 确认实现可被替换（测试或自定义 UI）；返回 True 表示允许执行
confirmer: Callable[[str, float], bool] | None = None


class CancellationFlag:
    """同时观察会话停止和本次工具超时，不取消同批其他工具。"""
    def __init__(self, parent):
        self.parent = parent
        self.local = Event()

    def is_set(self):
        return self.local.is_set() or self.parent.is_set()

    def set(self):
        self.local.set()


@dataclass
class ToolContext:
    root: Path
    audit_path: Path
    confirm: Callable[[str, float], bool]
    cancelled: Event
    turn_id: str = ""
    tool_call_id: str = ""
    permission_policy: str | None = None
    job_manager: object | None = None
    agent_tasks: object | None = None
    event_loop: object | None = None


_context: ContextVar[ToolContext | None] = ContextVar("tool_context", default=None)


@contextmanager
def tool_context(context: ToolContext):
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def cancellation_requested() -> bool:
    context = _context.get()
    return bool(context and context.cancelled.is_set())


class SandboxError(Exception):
    """沙箱约束被违反。"""


def sandbox_root() -> Path:
    """沙箱根目录（从 config 读取），不存在则创建。"""
    context = _context.get()
    root = context.root if context else Path(config.SANDBOX_DIR)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def read_only_root(name: str) -> Path:
    entry = config.TOOL_ROOTS.get(name)
    if not entry or entry.get('read_only') is not True:
        raise SandboxError(f'未配置只读根：{name}')
    root = Path(entry['path']).absolute()
    if root.resolve() != root or not root.is_dir():
        raise SandboxError(f'只读根不存在或根路径发生变化：{name}')
    return root


def path_scope(user_path: str, *, write: bool = False) -> tuple[Path, Path, str]:
    """额外根显式使用 @name/；未配置时完全沿用相对工作区路径。"""
    if not user_path or not user_path.strip():
        raise SandboxError('路径不能为空')
    if write and current_policy() == 'readonly':
        raise SandboxError('当前权限档位为 readonly，只允许读取')
    candidate = Path(user_path)
    if candidate.is_absolute():
        raise SandboxError(f'不允许绝对路径：{user_path}')
    if config.TOOL_ROOTS and candidate.parts and candidate.parts[0].startswith('@'):
        name = candidate.parts[0][1:]
        if name == 'workspace':
            return sandbox_root(), Path(*candidate.parts[1:]), '@workspace/'
        root = read_only_root(name)
        if write:
            raise SandboxError(f'只读根禁止写入：{name}')
        return root, Path(*candidate.parts[1:]), f'@{name}/'
    return sandbox_root(), candidate, ''


def resolve_path(user_path: str, *, write: bool = False) -> Path:
    """
    把用户给的相对路径解析成沙箱内的绝对路径。

    拒绝：空路径、绝对路径、以及解析后落在沙箱外的路径
    （`..` 上跳与符号链接都会在 resolve() 时展开，因此一并拦住）。
    """
    root, candidate, _ = path_scope(user_path, write=write)
    target = (root / candidate).resolve()   # 展开 .. 与符号链接

    if target != root and not target.is_relative_to(root):
        raise SandboxError(f"路径越出沙箱：{user_path}")

    return target


def truncate(text: str) -> str:
    """按 config.TOOL_MAX_OUTPUT 截断工具输出。"""
    limit = config.TOOL_MAX_OUTPUT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（输出超过 {limit} 字符，已截断）"


def _audit_path() -> Path:
    context = _context.get()
    path = context.audit_path if context else config.AUDIT_LOG
    return path


def audit(event: str, **fields) -> None:
    """写一条审计记录（文件 + 日志）。审计写入失败不影响主流程。"""
    context = _context.get()
    prefix = _audit_prefix(context.root.name if context else None,
        context.turn_id if context else None, context.tool_call_id if context else None,
        _audit_second(int(time.time())), event)
    if not _audit_keys.isdisjoint(fields):
        # 兼容既有调用方显式覆盖公共字段的行为。
        line = _audit_encoder.encode({**json.loads(prefix), **fields})
    else:
        line = prefix[:-1] + ', ' + _audit_encoder.encode(fields)[1:] if fields else prefix
    logger.info("[审计] %s", line)

    try:
        append_audit(context.audit_path if context else config.AUDIT_LOG, line, sync=config.AUDIT_SYNC)
    except OSError as exc:
        logger.warning("审计日志写入失败：%s", exc)


def _console_confirm(prompt: str, timeout: float) -> bool:
    """终端确认：等待用户输入 y/yes；超时或读取异常一律拒绝。"""
    print(prompt)
    sys.stdout.write("确认执行？(y/N) ")
    sys.stdout.flush()

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return False

    if not ready:
        print("（确认超时，已中止）")
        return False

    try:
        answer = sys.stdin.readline().strip().lower()
    except (OSError, ValueError):
        return False

    return answer in ("y", "yes")


def confirm(prompt: str, timeout: float) -> bool:
    """请求确认；任何异常都按拒绝处理。"""
    context = _context.get()
    if cancellation_requested():
        return False
    impl = context.confirm if context else (confirmer or _console_confirm)
    try:
        return bool(impl(prompt, timeout)) and not cancellation_requested()
    except Exception:
        logger.exception("确认过程出错，按拒绝处理")
        return False


def current_policy() -> str:
    context = _context.get()
    policy = context.permission_policy if context and context.permission_policy is not None else config.TOOL_PERMISSION_POLICY
    if policy not in {'readonly', 'standard', 'trusted'}:
        raise SandboxError('无效权限策略，拒绝执行')
    return policy


def _trusted_rule(action: str, paths: list[str] | None, command: str | None) -> int | None:
    from core.permissions import PermissionRule, simple_command_tokens
    for index, value in enumerate(config.TOOL_PERMISSION_RULES):
        rule = PermissionRule.model_validate(value)
        if rule.tool != action:
            continue
        if action == 'run_command' and command is not None:
            tokens = simple_command_tokens(command)
            if tokens is not None and tokens[:len(rule.command_prefix)] == rule.command_prefix:
                return index
        elif paths:
            root = sandbox_root()
            targets = [resolve_path(path, write=True).relative_to(root) for path in paths]
            if all(target.is_relative_to(Path(rule.path_prefix)) for target in targets):
                return index
    return None


def ask_permission(action: str, detail: str, reason: str = "", *, paths: list[str] | None = None,
                   command: str | None = None, force_confirmation: bool = False) -> bool:
    """
    风险操作确认：由策略判定为风险后调用。

    拒绝、超时、异常一律返回 False（fail-closed）。确认过程记审计。
    """
    prompt = f"[风险操作] {action}：{detail}"
    if reason:
        prompt += f"\n  理由：{reason}"

    policy = current_policy()
    if policy == 'readonly' or cancellation_requested():
        audit('confirm', action=action, detail=detail, reason=reason, allowed=False,
              decision_source='readonly_policy' if policy == 'readonly' else 'cancelled')
        return False
    if policy == 'trusted' and not force_confirmation:
        try:
            rule = _trusted_rule(action, paths, command)
        except (ValueError, SandboxError):
            audit('confirm', action=action, detail=detail, reason=reason, allowed=False, decision_source='invalid_rule')
            return False
        if rule is not None:
            audit('confirm', action=action, detail=detail, reason=reason, allowed=True,
                  decision_source='trusted_rule', rule=rule)
            return True

    allowed = confirm(prompt, config.CONFIRM_TIMEOUT)
    audit("confirm", action=action, detail=detail, reason=reason, allowed=allowed)
    return allowed


def check_command(command: str) -> list[str]:
    """Shell syntax is interpreted only inside isolated_command, never on the host."""
    if not command or not command.strip():
        raise SandboxError("命令不能为空")
    if "\x00" in command:
        raise SandboxError("命令不能为空或包含 NUL 字符")
    return ["/bin/sh", "-c", command]


def isolated_command(argv: list[str], *, network_socket: Path | None = None) -> list[str]:
    """Minimal Linux filesystem, private processes/network, writable workspace only.

    Never fall back to executing on the host if isolation is unavailable.
    """
    if sys.platform != 'linux':
        raise SandboxError('隔离命令需要 Linux/WSL2 的 bubblewrap 与 prlimit；当前平台仅使用文件工具或 headless 文本，不回退宿主执行')
    import shutil
    executable = shutil.which("bwrap", path="/usr/bin:/bin")
    if not executable:
        raise SandboxError("需要安装 bubblewrap 才能执行命令；文件操作仍可使用 CRUD 工具")
    command = [executable, "--unshare-all", "--die-with-parent", "--new-session",
               "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64"):
        path = Path("/") / name
        if path.is_symlink():
            command += ["--symlink", os.readlink(path), str(path)]
        elif path.is_dir():
            command += ["--ro-bind", str(path), str(path)]
    if network_socket is not None:
        relay = Path(__file__).with_name('network_relay.py')
        command += ['--dir', '/run/agent-network', '--ro-bind', str(network_socket),
                    '/run/agent-network/proxy.sock', '--ro-bind', str(relay), '/run/agent-network/relay.py']
        certificates = Path('/etc/ssl/certs')
        if certificates.is_dir():
            command += ['--ro-bind', str(certificates), str(certificates)]
        argv = ['/usr/bin/python3', '/run/agent-network/relay.py', *argv]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(sandbox_root()), "/workspace", "--chdir", "/workspace",
                "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
                "--setenv", "LANG", "C.UTF-8"]
    if network_socket is not None:
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            command += ['--setenv', name, 'http://127.0.0.1:8877']
    command += ['--', *argv]
    launcher = shutil.which('prlimit', path='/usr/bin:/bin')
    if not launcher:
        raise SandboxError('需要 prlimit 资源限制；拒绝无配额执行')
    limits = [f'--as={config.COMMAND_MEMORY_MB * 1024 * 1024}',
              f'--cpu={config.COMMAND_CPU_SECONDS}',
              f'--fsize={config.COMMAND_FILE_MB * 1024 * 1024}',
              f'--nproc={config.COMMAND_PROCESSES}']
    # 独立进程设置 rlimit，避免多线程程序中使用不安全的 preexec_fn。
    import resource
    try:
        for name in ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NPROC'):
            resource.getrlimit(getattr(resource, name))
    except (OSError, ValueError, AttributeError) as exc:
        raise SandboxError('prlimit 资源能力探测失败，拒绝执行') from exc
    audit('resource_limits', limits=limits, launcher=launcher)
    # 命名空间创建后再设置限制，避免宿主已有线程计入创建阶段。
    return command[:-len(argv)] + [launcher, *limits, '--', *argv]


def check_workspace_quota() -> int:
    """不跟随符号链接，超额拒绝；不删除任何工作区文件。"""
    total = 0
    try:
        for directory, _, files in os.walk(sandbox_root(), followlinks=False):
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        continue
                if total > config.WORKSPACE_LIMIT_MB * 1024 * 1024:
                    raise SandboxError('工作区超过 WORKSPACE_LIMIT_MB 配额，请先人工整理或增加配额')
    except OSError as exc:
        raise SandboxError('无法检查工作区配额，拒绝执行') from exc
    return total
