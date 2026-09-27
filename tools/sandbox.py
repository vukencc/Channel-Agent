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
import shlex
import sys
from pathlib import Path
from typing import Callable

import config
from core.log import get_logger

logger = get_logger(__name__)

# 确认实现可被替换（测试或自定义 UI）；返回 True 表示允许执行
confirmer: Callable[[str, float], bool] | None = None


@dataclass
class ToolContext:
    root: Path
    audit_path: Path
    confirm: Callable[[str, float], bool]
    cancelled: Event


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


def resolve_path(user_path: str) -> Path:
    """
    把用户给的相对路径解析成沙箱内的绝对路径。

    拒绝：空路径、绝对路径、以及解析后落在沙箱外的路径
    （`..` 上跳与符号链接都会在 resolve() 时展开，因此一并拦住）。
    """
    if not user_path or not user_path.strip():
        raise SandboxError("路径不能为空")

    candidate = Path(user_path)
    if candidate.is_absolute():
        raise SandboxError(f"不允许绝对路径：{user_path}")

    root = sandbox_root()
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
    path = context.audit_path if context else Path(config.AUDIT_LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def audit(event: str, **fields) -> None:
    """写一条审计记录（文件 + 日志）。审计写入失败不影响主流程。"""
    record = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(record, ensure_ascii=False)
    logger.info("[审计] %s", line)

    try:
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
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


def ask_permission(action: str, detail: str, reason: str = "") -> bool:
    """
    风险操作确认：由策略判定为风险后调用。

    拒绝、超时、异常一律返回 False（fail-closed）。确认过程记审计。
    """
    prompt = f"[风险操作] {action}：{detail}"
    if reason:
        prompt += f"\n  理由：{reason}"

    allowed = confirm(prompt, config.CONFIRM_TIMEOUT)
    audit("confirm", action=action, detail=detail, reason=reason, allowed=allowed)
    return allowed


def check_command(command: str) -> list[str]:
    """Parse argv without a host shell; execution must use isolated_command."""
    if not command or not command.strip():
        raise SandboxError("命令不能为空")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SandboxError(f"命令无法解析：{exc}") from exc
    if not argv or any("\x00" in part for part in argv):
        raise SandboxError("命令不能为空或包含 NUL 字符")
    return argv


def isolated_command(argv: list[str]) -> list[str]:
    """Minimal Linux filesystem, private processes/network, writable workspace only.

    Never fall back to executing on the host if isolation is unavailable.
    """
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
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(sandbox_root()), "/workspace", "--chdir", "/workspace",
                "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
                "--setenv", "HOME", "/tmp", "--setenv", "TMPDIR", "/tmp",
                "--setenv", "LANG", "C.UTF-8", "--", *argv]
    return command
