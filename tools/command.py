"""沙箱内的命令执行工具。

安全约束：shell=False（管道/重定向不生效）、工作目录固定在沙箱内、
限制超时与输出长度、危险命令直接拦截、执行前需用户确认。
"""
import subprocess

import config
from pydantic import BaseModel, Field

from tools.base import register_tool
from tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    check_command,
    sandbox_root,
    truncate,
)


class RunCommandArgs(BaseModel):
    """
    在沙箱目录内执行一条命令。需要用户确认；危险命令会被直接拒绝。

    命令按空格拆分为参数后直接执行（shell=False），因此不支持管道、重定向、通配符。
    """
    command: str = Field(
        description="要执行的命令，例如 'ls -la' 或 'cat notes/todo.txt'"
    )
    reason: str = Field(default="", description="说明执行目的，会显示在确认提示里")


@register_tool(RunCommandArgs, name="run_command")
def run_command(command: str, reason: str = "") -> str:
    """
    在沙箱目录内执行一条命令。
    """
    try:
        argv = check_command(command)
    except SandboxError as exc:
        audit("blocked", action="run_command", command=command, reason=str(exc))
        return f"[已拦截] {exc}"

    if not ask_permission("run_command", command, reason):
        return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"

    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=sandbox_root(),
            capture_output=True,
            text=True,
            timeout=config.COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        audit("timeout", action="run_command", command=command,
              timeout=config.COMMAND_TIMEOUT)
        return f"[超时] 命令超过 {config.COMMAND_TIMEOUT:.0f} 秒未结束，已终止。"
    except FileNotFoundError:
        audit("failed", action="run_command", command=command, error="命令不存在")
        return f"[失败] 找不到命令：{argv[0]}"
    except OSError as exc:
        audit("failed", action="run_command", command=command, error=str(exc))
        return f"[失败] 命令无法执行：{exc}"

    audit("executed", action="run_command", command=command,
          exit_code=proc.returncode, reason=reason)

    stdout = truncate(proc.stdout)
    stderr = truncate(proc.stderr)

    parts = [f"[退出码] {proc.returncode}"]
    parts.append(f"[stdout]\n{stdout}" if stdout else "[stdout] （空）")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n".join(parts)
