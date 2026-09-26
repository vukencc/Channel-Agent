"""沙箱内的文件 CRUD 工具。

- 读操作（read / list）不需要确认；
- 写操作（create / update / delete）属风险操作，必须经用户确认，
  拒绝或确认超时一律中止（fail-closed）。
"""
from typing import List

from pydantic import BaseModel, Field

from tools.base import register_tool
from tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    resolve_path,
    sandbox_root,
    truncate,
)


class CreateFileArgs(BaseModel):
    """
    在沙箱内新建文件。若文件已存在则拒绝，不会覆盖。需要用户确认。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    content: str = Field(default="", description="要写入的内容")
    reason: str = Field(default="", description="说明这次写入的用途，会显示在确认提示里")


class ReadFileArgs(BaseModel):
    """
    读取沙箱内某个文件的内容。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")


class UpdateFileArgs(BaseModel):
    """
    覆盖写入沙箱内已存在的文件。需要用户确认。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    content: str = Field(default="", description="新的完整内容（会覆盖原内容）")
    reason: str = Field(default="", description="说明这次修改的用途，会显示在确认提示里")


class DeleteFileArgs(BaseModel):
    """
    删除沙箱内的一个文件（不支持删除目录）。需要用户确认。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    reason: str = Field(default="", description="说明删除原因，会显示在确认提示里")


class ListFilesArgs(BaseModel):
    """
    列出沙箱内某个目录下的条目（文件/子目录/软链）。
    """
    path: str = Field(default=".", description="相对沙箱根目录的目录路径，默认列根目录")


@register_tool(CreateFileArgs, name="create_file")
def create_file(path: str, content: str = "", reason: str = "") -> str:
    """
    在沙箱内新建文件（不覆盖已存在文件）。
    """
    try:
        target = resolve_path(path)
    except SandboxError as exc:
        audit("blocked", action="create_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if target.exists():
        return f"[失败] 文件已存在，未覆盖：{path}"

    if not ask_permission("create_file", f"{path}（{len(content)} 字符）", reason):
        return "[已取消] 用户未确认（拒绝或确认超时），文件未创建。"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        audit("failed", action="create_file", path=path, error=str(exc))
        return f"[失败] 写入失败：{exc}"

    audit("executed", action="create_file", path=path, size=len(content), reason=reason)
    return f"[完成] 已创建 {path}（{len(content)} 字符）"


@register_tool(ReadFileArgs, name="read_file")
def read_file(path: str) -> str:
    """
    读取沙箱内文件的内容。
    """
    try:
        target = resolve_path(path)
    except SandboxError as exc:
        audit("blocked", action="read_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if not target.is_file():
        return f"[失败] 文件不存在：{path}"

    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        audit("failed", action="read_file", path=path, error=str(exc))
        return f"[失败] 读取失败：{exc}"

    audit("executed", action="read_file", path=path, size=len(text))
    return truncate(text)


@register_tool(UpdateFileArgs, name="update_file")
def update_file(path: str, content: str = "", reason: str = "") -> str:
    """
    覆盖写入沙箱内已存在的文件。
    """
    try:
        target = resolve_path(path)
    except SandboxError as exc:
        audit("blocked", action="update_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if not target.is_file():
        return f"[失败] 文件不存在，请先用 create_file 创建：{path}"

    if not ask_permission("update_file", f"{path}（{len(content)} 字符）", reason):
        return "[已取消] 用户未确认（拒绝或确认超时），文件未修改。"

    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        audit("failed", action="update_file", path=path, error=str(exc))
        return f"[失败] 写入失败：{exc}"

    audit("executed", action="update_file", path=path, size=len(content), reason=reason)
    return f"[完成] 已更新 {path}（{len(content)} 字符）"


@register_tool(DeleteFileArgs, name="delete_file")
def delete_file(path: str, reason: str = "") -> str:
    """
    删除沙箱内的一个文件。
    """
    try:
        target = resolve_path(path)
    except SandboxError as exc:
        audit("blocked", action="delete_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if target == sandbox_root():
        return "[失败] 不能删除沙箱根目录。"

    if not target.is_file():
        return f"[失败] 文件不存在（目录不支持删除）：{path}"

    if not ask_permission("delete_file", path, reason):
        return "[已取消] 用户未确认（拒绝或确认超时），文件未删除。"

    try:
        target.unlink()
    except OSError as exc:
        audit("failed", action="delete_file", path=path, error=str(exc))
        return f"[失败] 删除失败：{exc}"

    audit("executed", action="delete_file", path=path, reason=reason)
    return f"[完成] 已删除 {path}"


@register_tool(ListFilesArgs, name="list_files")
def list_files(path: str = ".") -> str:
    """
    列出沙箱内某个目录下的条目。
    """
    try:
        target = resolve_path(path)
    except SandboxError as exc:
        audit("blocked", action="list_files", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if not target.is_dir():
        return f"[失败] 目录不存在：{path}"

    root = sandbox_root()
    entries: List[str] = []
    for item in sorted(target.iterdir()):
        # 软链只显示本身，不去 stat 目标，避免顺链走到沙箱外
        if item.is_symlink():
            entries.append(f"- [link] {item.relative_to(root)}")
        elif item.is_dir():
            entries.append(f"- [dir ] {item.relative_to(root)}")
        else:
            entries.append(f"- [file] {item.relative_to(root)} ({item.stat().st_size} 字节)")

    audit("executed", action="list_files", path=path, count=len(entries))

    if not entries:
        return f"[目录] {path}（空）"
    return f"[目录] {path}（{len(entries)} 项）\n" + "\n".join(entries)
