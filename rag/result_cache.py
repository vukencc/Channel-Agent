"""线程安全、有界、仅缓存真实计算结果的 LRU。"""
from collections import OrderedDict
from threading import RLock


class ResultCache:
    def __init__(self):
        self.items = OrderedDict()
        self.lock = RLock()

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
