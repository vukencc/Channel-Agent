"""有界审计追加句柄；直接写内核，不在 Python 用户态缓冲已确认事件。"""
import atexit
import os
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


def append_audit(path: Path, line: str, sync: bool = False):
    key = os.fspath(path)
    payload = (line + '\n').encode('utf-8')
    with _lock:
        existing = _handles.get(key)
        if existing is not None:
            try:
                current = os.stat(key, follow_symlinks=False)
                unchanged = (current.st_dev, current.st_ino) == existing[1]
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                os.close(_handles.pop(key)[0])
                existing = None
        if existing is None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt':
                descriptor = _open_windows_audit(path)
            else:
                descriptor = os.open(key, os.O_APPEND | os.O_CREAT | os.O_WRONLY
                                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            stat = os.fstat(descriptor)
            existing = descriptor, (stat.st_dev, stat.st_ino)
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
