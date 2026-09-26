"""工具沙箱：路径约束、风险确认、危险命令判定、审计日志。

安全原则（fail-closed）：任何异常、无法确认、用户拒绝、确认超时，一律按「不允许」处理。
"""
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


class SandboxError(Exception):
    """沙箱约束被违反。"""


def sandbox_root() -> Path:
    """沙箱根目录（从 config 读取），不存在则创建。"""
    root = Path(config.SANDBOX_DIR)  # type: ignore[arg-type]
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
    path = Path(config.AUDIT_LOG)  # type: ignore[arg-type]
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
    impl = confirmer or _console_confirm
    try:
        return bool(impl(prompt, timeout))
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


# 危险命令（按可执行文件名匹配）：直接拦截，不给确认机会
DANGEROUS_COMMANDS = {
    "rm", "rmdir", "dd", "mkfs", "mkfs.ext4", "shutdown", "reboot", "halt", "poweroff",
    "sudo", "su", "chmod", "chown", "chgrp", "kill", "pkill", "killall",
    "mount", "umount", "systemctl", "service", "iptables", "useradd", "userdel", "passwd",
    "crontab", "at", "nc", "ncat", "netcat", "telnet", "ssh", "scp", "sftp",
    "curl", "wget",
    # 解释器可以绕过沙箱（能读写沙箱外的文件），一律拦掉
    "sh", "bash", "zsh", "dash", "fish", "python", "python3", "perl", "ruby", "node", "php",
}


def check_command(command: str) -> list[str]:
    """
    校验命令并返回 argv。

    用 shlex 拆分，配合 shell=False，管道/重定向等元字符不会被解释；
    命中危险命令或使用绝对路径调用程序，直接抛 SandboxError。
    """
    if not command or not command.strip():
        raise SandboxError("命令不能为空")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise SandboxError(f"命令无法解析：{exc}") from exc

    if not argv:
        raise SandboxError("命令不能为空")

    if Path(argv[0]).is_absolute():
        raise SandboxError(f"不允许用绝对路径调用外部程序：{argv[0]}")

    program = Path(argv[0]).name
    if program in DANGEROUS_COMMANDS:
        raise SandboxError(f"命令「{program}」在禁止清单内，已拦截")

    return argv
