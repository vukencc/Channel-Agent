"""线程安全、有界、仅缓存真实计算结果的 LRU。"""
from collections import OrderedDict
from threading import RLock
from weakref import WeakSet


_instances = WeakSet()
_instances_lock = RLock()


class ResultCache:
    def __init__(self):
        self.items = OrderedDict()
        self.lock = RLock()
        with _instances_lock:
            _instances.add(self)

    def clear(self) -> None:
        with self.lock:
            self.items.clear()

    def get(self, key, size):
        if size <= 0:
            return None
        with self.lock:
            value = self.items.get(key)
            if value is not None:
                self.items.move_to_end(key)
            return value

    def put(self, key, value, size):
        if size <= 0:
            return
        with self.lock:
            self.items[key] = value
            self.items.move_to_end(key)
            while len(self.items) > size:
                self.items.popitem(last=False)


def clear_runtime_cache() -> None:
    with _instances_lock:
        instances = list(_instances)
    for cache in instances:
        cache.clear()
