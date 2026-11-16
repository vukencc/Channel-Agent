"""有界审计追加句柄；直接写内核，不在 Python 用户态缓冲已确认事件。"""
import atexit
import os
import stat
import threading
from collections import OrderedDict
from pathlib import Path

_handles = OrderedDict()
_lock = threading.RLock()
_maximum = 64


def _open_windows_audit(path: Path) -> int:
    import ctypes
    import msvcrt
    from agent_core.core.files import opened_path
    from agent_core.core.windows_files import _create, _close, _info, _FileInfo, _INVALID, _validate
    from agent_core.core.windows_security import protect_private_path
    _validate(path)
    with opened_path(path.parent, path.parent, directory=True):
        handle = _create(str(path), 0x40000000 | 0x80, 0x1 | 0x2, None, 4, 0x00200000, None)
        if handle == _INVALID:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileInfo()
            if not _info(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & (0x400 | 0x10) or info.links != 1:
                raise PermissionError('审计文件不能是重解析点、目录或硬链接')
            protect_private_path(path)
            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_APPEND | os.O_BINARY)
        except BaseException:
            _close(handle)
            raise
        return descriptor


def close_audit_handles(root: Path | None = None):
    with _lock:
        for key in list(_handles):
            if root is None or Path(key).is_relative_to(root):
                os.close(_handles.pop(key)[0])


def _relative_audit_info(name: str, directory_fd: int):
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PermissionError('审计文件不能是链接、目录或其他特殊文件')
    return info


def _open_relative_audit(name: str, directory_fd: int) -> int:
    descriptor = os.open(name, os.O_APPEND | os.O_CREAT | os.O_WRONLY
                         | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        current = _relative_audit_info(name, directory_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or current is None
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise PermissionError('审计文件身份已发生变化')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def append_audit(path: Path, line: str, sync: bool = False, *, directory_fd: int | None = None):
    """可在 POSIX 使用调用方持有的安全目录能力；路径仍作为共享串行化键。"""
    if directory_fd is not None:
        if (os.name == 'nt' or os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW')
                or type(directory_fd) is not int or directory_fd < 0
                or not stat.S_ISDIR(os.fstat(directory_fd).st_mode)
                or path.name in {'', '.', '..'}):
            raise PermissionError('当前平台或句柄不支持安全目录审计')
    key = os.fspath(path)
    payload = (line + '\n').encode('utf-8')
    with _lock:
        existing = _handles.get(key)
        if existing is not None:
            try:
                current = (_relative_audit_info(path.name, directory_fd) if directory_fd is not None
                           else os.stat(key, follow_symlinks=False))
                unchanged = current is not None and (current.st_dev, current.st_ino) == existing[1]
            except FileNotFoundError:
                unchanged = False
            except OSError:
                os.close(_handles.pop(key)[0])
                raise
            if not unchanged:
                os.close(_handles.pop(key)[0])
                existing = None
        if existing is None:
            if directory_fd is not None:
                descriptor = _open_relative_audit(path.name, directory_fd)
            elif os.name == 'nt':
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = _open_windows_audit(path)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(key, os.O_APPEND | os.O_CREAT | os.O_WRONLY
                                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            info = os.fstat(descriptor)
            existing = descriptor, (info.st_dev, info.st_ino)
            _handles[key] = existing
            while len(_handles) > _maximum:
                os.close(_handles.popitem(last=False)[1][0])
        _handles.move_to_end(key)
        try:
            written = os.write(existing[0], payload)
            offset = written
            while offset < len(payload):
                if written <= 0:
                    raise OSError('审计追加未完成')
                written = os.write(existing[0], payload[offset:])
                offset += written
            if sync:
                os.fsync(existing[0])
        except OSError:
            os.close(_handles.pop(key)[0])
            raise


atexit.register(close_audit_handles)
