"""显式内存预算下的正文磁盘存储、字节 LRU 和进程 RSS 准入检查。"""
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
from threading import RLock

import config


class ByteCache:
    def __init__(self, limit):
        self.limit, self.bytes = limit, 0
        self.items = OrderedDict()
        self.lock = RLock()

    def get(self, key):
        with self.lock:
            item = self.items.get(key)
            if item is not None:
                self.items.move_to_end(key)
                return item[0]
            return None

    def put(self, key, value, size):
        with self.lock:
            if key in self.items:
                self.bytes -= self.items.pop(key)[1]
            if size <= self.limit:
                self.items[key] = (value, size)
                self.bytes += size
            while self.bytes > self.limit:
                self.bytes -= self.items.popitem(last=False)[1][1]


_cache = ByteCache(0)


def shared_cache():
    # 预留模型、向量和元数据空间；正文与分块缓存共同使用最多 64 MiB。
    limit = min(64, config.RAG_MEMORY_LIMIT_MB / 4) * 1024 ** 2
    with _cache.lock:
        _cache.limit = int(limit)
        while _cache.bytes > _cache.limit:
            _cache.bytes -= _cache.items.popitem(last=False)[1][1]
    return _cache


def check_memory_budget(extra_bytes=0):
    if not config.RAG_MEMORY_LIMIT_MB:
        return
    try:
        resident = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        raise MemoryError('当前平台不能检查 RSS；请关闭 RAG_MEMORY_LIMIT_MB 或使用支持的 Linux 环境') from None
    if resident + extra_bytes > config.RAG_MEMORY_LIMIT_MB * 1024 ** 2:
        raise MemoryError('超过 RAG_MEMORY_LIMIT_MB 进程内存准入预算；请提高预算、缩小知识库或使用独立进程')


class TextStore:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'text-content.sqlite'
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS texts (id TEXT PRIMARY KEY, content TEXT NOT NULL)')
        self.lock = RLock()
        self.pending = 0

    def put(self, text):
        key = hashlib.sha256(text.encode()).hexdigest()
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO texts VALUES (?,?)', (key, text))
            self.pending += 1
            if self.pending >= 512:
                self.flush()
        return key

    def get(self, key):
        cache = shared_cache()
        identity = (str(self.path), key)
        text = cache.get(identity)
        if text is None:
            with self.lock:
                row = self.db.execute('SELECT content FROM texts WHERE id=?', (key,)).fetchone()
                if row is None:
                    raise ValueError('正文缓存缺失，请重新建立索引')
                text = row[0]
            cache.put(identity, text, sys.getsizeof(text) + sys.getsizeof(identity) + len(key))
        return text

    def flush(self):
        with self.lock:
            self.db.commit()
            self.pending = 0

    def __del__(self):
        if hasattr(self, 'db'):
            self.db.close()


@lru_cache(maxsize=4)
def text_store(directory):
    return TextStore(directory)


class TextRecord(Mapping):
    def __init__(self, fields, text_key, text, store):
        self.fields, self.text_key, self.store = fields, text_key, store
        self.identifier = store.put(text)

    def __getitem__(self, key):
        return self.store.get(self.identifier) if key == self.text_key else self.fields[key]

    def __iter__(self):
        yield from self.fields
        yield self.text_key

    def __len__(self):
        return len(self.fields) + 1


class TextSequence(Sequence):
    def __init__(self, records, key):
        self.records, self.key = records, key

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [record[self.key] for record in self.records[index]]
        return self.records[index][self.key]


def object_bytes(value):
    if isinstance(value, (tuple, list)):
        return sys.getsizeof(value) + sum(object_bytes(item) for item in value)
    return sys.getsizeof(value)
