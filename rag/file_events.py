"""用文件事件补充粗粒度 stat；不可用或溢出时保守重读。"""
import ctypes
import os
import struct


class FileEvents:
    def __init__(self):
        self.fd = -1
        self.paths = {}
        self.watched = set()
        try:
            self.libc = ctypes.CDLL(None, use_errno=True)
            self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (AttributeError, OSError):
            pass

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def watch(self, path):
        if self.fd < 0 or path in self.watched:
            return
        descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(path), 0x2 | 0x4 | 0x8 | 0x400 | 0x800)
        if descriptor < 0:
            self.close()
            return
        self.paths[descriptor] = path
        self.watched.add(path)

    def changed(self):
        if self.fd < 0:
            return None
        changed = set()
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return changed
            except OSError:
                self.close()
                return None
            if not data:
                return changed
            offset = 0
            while offset < len(data):
                descriptor, mask, _, length = struct.unpack_from('iIII', data, offset)
                offset += 16 + length
                if mask & 0x4000:  # 队列溢出，不能信任缓存。
                    return None
                path = self.paths.get(descriptor)
                if path:
                    changed.add(path)
                    if mask & (0x400 | 0x800 | 0x8000):
                        self.watched.discard(path)
                        self.paths.pop(descriptor, None)
