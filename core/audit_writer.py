"""有界审计追加句柄；直接写内核，不在 Python 用户态缓冲已确认事件。"""
import atexit
import os
import threading
from collections import OrderedDict
from pathlib import Path

_handles = OrderedDict()
_lock = threading.RLock()
_maximum = 64


def close_audit_handles():
    with _lock:
        while _handles:
            _, (descriptor, _) = _handles.popitem()
            os.close(descriptor)


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
