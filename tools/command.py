"""沙箱内的命令执行工具。

Bubblewrap 隔离主机文件与网络，工作目录 /workspace 映射沙箱目录；
执行前确认，限制超时与输出长度。隔离不可用时不退回主机执行。
"""
import subprocess
import os
import signal

import config
from pydantic import BaseModel, Field

from tools.base import register_tool
from tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    check_command,
    isolated_command,
    sandbox_root,
    truncate,
)


class RunCommandArgs(BaseModel):
    """
    在隔离工作区执行命令，支持 Python、Shell、mkdir/cp/mv/rm 等系统工具。
    主机仅沙箱目录映射为 /workspace 并可写；网络关闭，主机密钥和虚拟环境不可见。
    简单文件增删改查优先用 CRUD 工具。命令自动发起执行确认，无需先口头询问。
    普通命令不解释管道/重定向；需要时显式使用 sh -c '...'。
    """
    command: str = Field(
        description="要执行的命令，例如 ls -la、python3 -c 'print(1+1)' 或 sh -c 'ls | head'"
    )
    reason: str = Field(default="", description="说明执行目的，会显示在确认提示里")


@register_tool(RunCommandArgs, name="run_command")
def run_command(command: str, reason: str = "") -> str:
    """
    在沙箱目录内执行一条命令。
    """
    try:
        argv = check_command(command)
        isolated = isolated_command(argv)
    except SandboxError as exc:
        audit("blocked", action="run_command", command=command, reason=str(exc))
        return f"[已拦截] {exc}"

    if not ask_permission("run_command", command, reason):
        return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"

    try:
        proc = subprocess.Popen(
            isolated,
            shell=False,
            cwd=sandbox_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=config.COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
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

    stdout = truncate(stdout)
    stderr = truncate(stderr)

    parts = [f"[退出码] {proc.returncode}"]
    parts.append(f"[stdout]\n{stdout}" if stdout else "[stdout] （空）")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n".join(parts)
