"""沙箱内的文件 CRUD 工具。

- 读操作（read / list）不需要确认；
- 写操作（create / update / delete）属风险操作，必须经用户确认，
  拒绝或确认超时一律中止（fail-closed）。
"""
from typing import List
import os
import tempfile

import config

from pydantic import BaseModel, Field

from tools.base import register_tool
from tools.sandbox import (
    SandboxError,
    ask_permission,
    audit,
    resolve_path,
    sandbox_root,
    path_scope,
)


class CreateFileArgs(BaseModel):
    """
    用户要求新建、保存内容到文件时主动调用。自动创建父目录，不覆盖已有文件。工具自动处理写入确认。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    content: str = Field(default="", description="要写入的内容")
    reason: str = Field(default="", description="说明这次写入的用途，会显示在确认提示里")


class ReadFileArgs(BaseModel):
    """
    分页读取文件；无需确认。大文件返回 next_offset，必须继续读取所需片段，不能把分页内容当作全文。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    offset: int = Field(default=0, ge=0, description="从 0 开始的字符偏移；续读使用返回的 next_offset，不是字节数")
    limit: int = Field(default=config.FILE_READ_CHARS, ge=1, description="本页最多字符数，服务端还会限制在 FILE_READ_CHARS 内")
    search: str = Field(default="", max_length=1024, description="可选：从 offset 起查找精确文本，从首次匹配处读取；用于定位标签、函数、CSS 选择器")


class EditFileArgs(BaseModel):
    """修改已有文件的首选工具。精确替换一个唯一文本片段，其他内容保留，写前确认。
    先 read_file 读取目标片段；old_text 必须逐字匹配且只出现一次。
    长文件逐步修改，每次 new_text 建议不超过 4000 字符，不要重新输出整份文件。
    插入内容时用相邻唯一文本作锚点；删除片段时 new_text 传空字符串。
    """
    path: str = Field(description="相对沙箱根目录的文件路径")
    old_text: str = Field(min_length=1, description="从实际文件读取的唯一原文，不接受省略号或模糊匹配")
    new_text: str = Field(description="替换后的局部内容；未选中的文件内容不变")
    reason: str = Field(default="", description="本次局部修改的目的")


EditFileArgs.__doc__ = EditFileArgs.__doc__.replace('4000', str(config.FILE_APPEND_CHARS))


class UpdateFileArgs(BaseModel):
    """
    仅适合小文件全文更新。长文件或只读取了分页时使用 edit_file 局部修改，避免重传全文与截断覆盖。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    content: str = Field(default="", description="新的完整内容（会覆盖原内容）")
    reason: str = Field(default="", description="说明这次修改的用途，会显示在确认提示里")


class DeleteFileArgs(BaseModel):
    """
    用户明确要求删除文件时调用（不支持目录）。工具自动处理确认，拒绝后不要换工具重试。
    """
    path: str = Field(description="相对沙箱根目录的路径，例如 notes/todo.txt")
    reason: str = Field(default="", description="说明删除原因，会显示在确认提示里")


class ListFilesArgs(BaseModel):
    """
    用户要求查看目录，或操作前需要确认文件位置时调用。无需确认，默认列出沙箱根目录。
    """
    path: str = Field(default=".", description="相对沙箱根目录的目录路径，默认列根目录")


@register_tool(CreateFileArgs, name="create_file")
def create_file(path: str, content: str = "", reason: str = "") -> str:
    """
    在沙箱内新建文件（不覆盖已存在文件）。
    """
    try:
        target = resolve_path(path, write=True)
    except SandboxError as exc:
        audit("blocked", action="create_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if target.exists():
        return f"[失败] 文件已存在，未覆盖：{path}"

    if not ask_permission("create_file", f"{path}（{len(content)} 字符）", reason, paths=[path]):
        return "[已取消] 用户未确认（拒绝或确认超时），文件未创建。"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        audit("failed", action="create_file", path=path, error=str(exc))
        return f"[失败] 写入失败：{exc}"

    audit("executed", action="create_file", path=path, size=len(content), reason=reason)
    return f"[完成] 已创建 {path}（{len(content)} 字符）"


@register_tool(ReadFileArgs, name="read_file", concurrency="read")
def read_file(path: str, offset: int = 0, limit: int = config.FILE_READ_CHARS, search: str = "") -> str:
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
    if offset < 0 or limit < 1:
        return "[失败] offset 必须非负，limit 必须为正数。"

    try:
        size = min(limit, config.FILE_READ_CHARS, max(1, config.TOOL_MAX_OUTPUT))
        if search:
            with target.open(encoding='utf-8', newline='') as source:
                tail, consumed, found = '', 0, -1
                while chunk := source.read(65536):
                    window = tail + chunk
                    base = consumed - len(tail)
                    index = window.find(search, max(0, offset - base))
                    if index >= 0:
                        found = base + index
                        break
                    consumed += len(chunk)
                    tail = window[-(len(search) - 1):] if len(search) > 1 else ''
                if found < 0:
                    return f"[未找到] 从 offset={offset} 起没有匹配 search 的文本，文件未修改。"
                offset = found
        with target.open(encoding="utf-8", newline='') as stream:
            remaining = offset
            while remaining:
                skipped = stream.read(min(remaining, 65536))
                if not skipped:
                    return "[失败] offset 超出文件末尾，请使用上一页的 next_offset。"
                remaining -= len(skipped)
            text = stream.read(size)
            more = bool(stream.read(1))
    except (OSError, UnicodeDecodeError) as exc:
        audit("failed", action="read_file", path=path, error=str(exc))
        return f"[失败] 读取失败：{exc}"

    audit("executed", action="read_file", path=path, size=len(text), offset=offset, has_more=more)
    if offset == 0 and not more and not search:
        return text
    next_offset = offset + len(text)
    state = f"next_offset={next_offset}" if more else "EOF"
    return f"[文件分页] path={path} offset={offset} chars={len(text)} {state}\n[内容开始]\n{text}\n[内容结束]"


@register_tool(EditFileArgs, name="edit_file")
def edit_file(path: str, old_text: str, new_text: str, reason: str = "") -> str:
    """Replace one exact anchor atomically; refuse ambiguity and changes during confirmation."""
    try:
        target = resolve_path(path, write=True)
        before = target.read_bytes()
        text = before.decode('utf-8')
    except SandboxError as exc:
        audit("blocked", action="edit_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"
    except (OSError, UnicodeError) as exc:
        return f"[失败] 无法读取文件：{exc}"
    count = text.count(old_text) if old_text else 0
    if count != 1:
        return f"[失败] old_text 必须非空且唯一匹配，实际匹配 {count} 处；请重新读取目标片段并扩大锚点，文件未修改。"
    if old_text == new_text:
        return "[未修改] 新旧片段相同。"
    if not ask_permission('edit_file', f'{path}（替换 {len(old_text)} → {len(new_text)} 字符）', reason, paths=[path]):
        return "[已取消] 用户未确认，文件未修改。"
    temporary = None
    try:
        if resolve_path(path, write=True) != target or target.read_bytes() != before:
            return "[失败] 确认期间文件发生变化，请重新读取后修改。"
        after = text.replace(old_text, new_text, 1).encode('utf-8')
        descriptor, temporary = tempfile.mkstemp(prefix='.edit-', dir=target.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except OSError as exc:
        audit('failed', action='edit_file', path=path, error=str(exc))
        return f"[失败] 修改失败：{exc}"
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    audit('executed', action='edit_file', path=path, removed=len(old_text), added=len(new_text), reason=reason)
    return f"[完成] 已局部更新 {path}（替换 1 处，文件 {len(after)} 字节；其余内容保留）"


@register_tool(UpdateFileArgs, name="update_file")
def update_file(path: str, content: str = "", reason: str = "") -> str:
    """
    覆盖写入沙箱内已存在的文件。
    """
    try:
        target = resolve_path(path, write=True)
    except SandboxError as exc:
        audit("blocked", action="update_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if not target.is_file():
        return f"[失败] 文件不存在，请先用 create_file 创建：{path}"
    try:
        with target.open(encoding='utf-8') as stream:
            large = len(stream.read(config.FILE_READ_CHARS + 1)) > config.FILE_READ_CHARS
    except (OSError, UnicodeError) as exc:
        return f"[失败] 无法检查原文件：{exc}"
    if large:
        audit('blocked', action='update_file', path=path, reason='large_file_requires_edit')
        return '[已拦截] 原文件超过单页读取上限，禁止全文覆盖；请分页 read_file 后使用 edit_file 局部替换，原文件未修改。'

    if not ask_permission("update_file", f"{path}（{len(content)} 字符）", reason, paths=[path]):
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
        target = resolve_path(path, write=True)
    except SandboxError as exc:
        audit("blocked", action="delete_file", path=path, reason=str(exc))
        return f"[已拦截] {exc}"

    if target == sandbox_root():
        return "[失败] 不能删除沙箱根目录。"

    if not target.is_file():
        return f"[失败] 文件不存在（目录不支持删除）：{path}"

    if not ask_permission("delete_file", path, reason, paths=[path]):
        return "[已取消] 用户未确认（拒绝或确认超时），文件未删除。"

    try:
        target.unlink()
    except OSError as exc:
        audit("failed", action="delete_file", path=path, error=str(exc))
        return f"[失败] 删除失败：{exc}"

    audit("executed", action="delete_file", path=path, reason=reason)
    return f"[完成] 已删除 {path}"


@register_tool(ListFilesArgs, name="list_files", concurrency="read")
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

    root, _, prefix = path_scope(path)
    entries: List[str] = []
    for item in sorted(target.iterdir()):
        # 软链只显示本身，不去 stat 目标，避免顺链走到沙箱外
        if item.is_symlink():
            entries.append(f"- [link] {prefix}{item.relative_to(root)}")
        elif item.is_dir():
            entries.append(f"- [dir ] {prefix}{item.relative_to(root)}")
        else:
            entries.append(f"- [file] {prefix}{item.relative_to(root)} ({item.stat().st_size} 字节)")

    audit("executed", action="list_files", path=path, count=len(entries))

    if not entries:
        return f"[目录] {path}（空）"
    return f"[目录] {path}（{len(entries)} 项）\n" + "\n".join(entries)


class AppendFileArgs(BaseModel):
    """分段生成长文件：每段最多 4000 字符，expected_chars 必须等于当前文件字符长度。
    新文件先 create_file，再根据成功结果的 next_offset 追加；失败后读取确认，禁止重复追加。
    """
    path: str
    content: str = Field(min_length=1, max_length=config.FILE_APPEND_CHARS)
    expected_chars: int = Field(ge=0)
    reason: str = ''


AppendFileArgs.__doc__ = AppendFileArgs.__doc__.replace('4000', str(config.FILE_APPEND_CHARS))


@register_tool(AppendFileArgs, name='append_file')
def append_file(path: str, content: str, expected_chars: int, reason: str = '') -> str:
    AppendFileArgs(path=path, content=content, expected_chars=expected_chars, reason=reason)
    temporary = None
    try:
        target = resolve_path(path, write=True)
        before = target.read_bytes()
        actual = len(before.decode('utf-8'))
        if actual != expected_chars:
            audit('blocked', action='append_file', path=path, expected=expected_chars, actual=actual)
            return f'[已拦截] 文件字符偏移不匹配：当前 {actual}，请求 {expected_chars}；请先读取核对。'
        if not ask_permission('append_file', f'{path}，偏移 {actual}，新增 {len(content)} 字符', reason, paths=[path]):
            return '[已取消] 用户未确认，文件未修改。'
        if resolve_path(path, write=True) != target or target.read_bytes() != before:
            return '[已拦截] 确认期间文件已变化，请重新读取。'
        descriptor, temporary = tempfile.mkstemp(prefix='.append-', dir=target.parent)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(before)
            stream.write(content.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
        audit('executed', action='append_file', path=path, added_chars=len(content))
        return f'[完成] 已追加 {len(content)} 字符；next_offset={actual + len(content)}'
    except (OSError, UnicodeError, SandboxError) as exc:
        audit('failed', action='append_file', path=path, error=type(exc).__name__)
        return f'[失败] 无法追加：{exc}'
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
