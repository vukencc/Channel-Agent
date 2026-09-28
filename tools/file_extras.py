"""显式启用的文件整理工具；不覆盖目标，写入前确认，复制原子发布。"""
import ctypes
import fnmatch
from functools import lru_cache
import json
import os
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

import config
from tools.base import register_tool
from tools.sandbox import (SandboxError, ask_permission, audit, cancellation_requested,
                           check_workspace_quota, resolve_path, sandbox_root, path_scope)


class MkdirArgs(BaseModel):
    """创建目录；默认仅创建一层，需要 parents=true 才创建缺失父目录；写前确认。"""
    path: str
    parents: bool = False
    reason: str = ''


class TransferArgs(BaseModel):
    """整理普通文件；目标必须不存在，父目录须已创建；不递归复制目录。写前确认。"""
    source: str
    destination: str
    reason: str = ''


class StatArgs(BaseModel):
    """查看文件/目录类型、字节大小与修改时间；无需确认。"""
    path: str


class GlobArgs(BaseModel):
    """查找沙箱内相对路径，支持 **；不跟随符号链接，有界返回。"""
    pattern: str = Field(default='*', min_length=1, max_length=1024)
    limit: int = Field(default=100, ge=1, le=1000)


def _failed(action, exc, **fields):
    audit('blocked', action=action, reason=str(exc), **fields)
    return f'[已拦截] {exc}'


def mkdir(path: str, parents: bool = False, reason: str = '') -> str:
    try:
        target = resolve_path(path, write=True)
        check_workspace_quota()
        if target.exists():
            raise SandboxError('目标已存在，不覆盖')
        if not ask_permission('mkdir', path, reason):
            return '[已取消] 未创建目录。'
        if cancellation_requested() or resolve_path(path, write=True) != target:
            raise SandboxError('已取消或路径发生变化')
        check_workspace_quota()
        target.mkdir(parents=parents)
        audit('executed', action='mkdir', path=path, reason=reason)
        return f'[完成] 已创建目录 {path}'
    except (OSError, SandboxError) as exc:
        return _failed('mkdir', exc, path=path)


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _rename_no_replace(source, destination):
    # Linux 原子且不覆盖的重命名；其他平台拒绝，绝不退化为覆盖式 rename。
    if sys.platform != 'linux':
        raise SandboxError('原子 move 目前需要 Linux；可确认 copy 后单独删除源文件')
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise SandboxError('系统不支持不覆盖的原子重命名')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _transfer(action, source, destination, reason):
    temporary = None
    try:
        src, dst = resolve_path(source, write=action == 'move'), resolve_path(destination, write=True)
        if not src.is_file() or dst.exists() or dst.is_symlink() or not dst.parent.is_dir():
            raise SandboxError('源必须是普通文件，目标必须不存在且父目录已创建')
        identity = _identity(src)
        usage = check_workspace_quota()
        if action == 'copy' and usage + identity[2] > config.WORKSPACE_LIMIT_MB * 1024 ** 2:
            raise SandboxError('复制后将超过 WORKSPACE_LIMIT_MB 配额')
        if not ask_permission(action, f'{source} → {destination}', reason):
            return '[已取消] 文件未变更。'
        if (cancellation_requested() or resolve_path(source, write=action == 'move') != src or resolve_path(destination, write=True) != dst
                or _identity(src) != identity or dst.exists() or dst.is_symlink()):
            raise SandboxError('取消或确认期间文件路径/内容发生变化')
        usage = check_workspace_quota()
        if action == 'move':
            _rename_no_replace(src, dst)
        else:
            if usage + identity[2] > config.WORKSPACE_LIMIT_MB * 1024 ** 2:
                raise SandboxError('复制后将超过 WORKSPACE_LIMIT_MB 配额')
            descriptor, temporary = tempfile.mkstemp(prefix='.copy-', dir=dst.parent)
            with os.fdopen(descriptor, 'wb') as output, src.open('rb') as input_stream:
                copied = 0
                while chunk := input_stream.read(65536):
                    copied += len(chunk)
                    if cancellation_requested() or copied > identity[2]:
                        raise SandboxError('已取消或复制期间源文件增长')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if (_identity(src) != identity or resolve_path(destination, write=True) != dst
                    or cancellation_requested()):
                raise SandboxError('复制期间源文件或目标路径发生变化')
            # 同文件系统 hard link 原子发布且不覆盖并发创建的目标。
            os.link(temporary, dst)
        audit('executed', action=action, source=source, destination=destination, reason=reason)
        return f'[完成] {action}: {source} → {destination}'
    except (OSError, SandboxError) as exc:
        return _failed(action, exc, source=source, destination=destination)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def move(source: str, destination: str, reason: str = '') -> str:
    return _transfer('move', source, destination, reason)


def copy(source: str, destination: str, reason: str = '') -> str:
    return _transfer('copy', source, destination, reason)


def stat(path: str) -> str:
    try:
        target = resolve_path(path)
        value = target.stat()
        result = {'path': path, 'type': 'directory' if target.is_dir() else 'file',
                  'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
        audit('read', action='stat', path=path)
        return json.dumps(result, ensure_ascii=False)
    except (OSError, SandboxError) as exc:
        return _failed('stat', exc, path=path)


def _glob_paths(root):
    # os.walk 从不进入链接目录，避免先越界枚举再过滤。
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in dirs + files:
            path = Path(directory) / name
            if not path.is_symlink():
                yield path


def _glob_match(path, pattern):
    parts, patterns = Path(path).parts, Path(pattern).parts
    @lru_cache(maxsize=None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == '**':
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
    return match(0, 0)


def glob(pattern: str = '*', limit: int = 100) -> str:
    try:
        if Path(pattern).is_absolute() or '..' in Path(pattern).parts:
            raise SandboxError('匹配模式必须位于沙箱内')
        root, scoped_pattern, prefix = path_scope(pattern)
        matches = []
        chars = 0
        for path in _glob_paths(root):
            if cancellation_requested():
                raise SandboxError('已取消')
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
                continue
            relative = prefix + path.relative_to(root).as_posix()
            resolve_path(relative)
            if not _glob_match(path.relative_to(root).as_posix(), scoped_pattern):
                continue
            chars += len(relative) + 8
            if len(matches) >= limit or chars > config.TOOL_MAX_OUTPUT - 128:
                audit('read', action='glob', pattern=pattern, count=len(matches), truncated=True)
                return json.dumps({'paths': matches, 'truncated': True}, ensure_ascii=False)
            matches.append(relative)
        audit('read', action='glob', pattern=pattern, count=len(matches))
        return json.dumps({'paths': matches, 'truncated': False}, ensure_ascii=False)
    except (OSError, ValueError, SandboxError) as exc:
        return _failed('glob', exc, pattern=pattern)


def register_file_extras():
    for name, model in [('mkdir', MkdirArgs), ('move', TransferArgs), ('copy', TransferArgs),
                        ('stat', StatArgs), ('glob', GlobArgs)]:
        register_tool(model, name=name, concurrency='read' if name in {'stat', 'glob'} else 'serial')(globals()[name])
