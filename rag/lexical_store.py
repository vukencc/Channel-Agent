"""SQLite 增量倒排索引；WAL 读快照使更新不改变正在使用的旧检索对象。"""
from collections import Counter
from contextlib import closing
import hashlib
import math
import sqlite3
from threading import RLock

from rag.lexical import tokenize


class PersistentBM25:
    def __init__(self, path, texts, identifiers):
        if len(texts) != len(identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError('BM25 文档 ID 必须唯一且与正文数量一致')
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.positions = {identifier: i for i, identifier in enumerate(identifiers)}
        with closing(self._writer()) as db:
            saved = dict(db.execute('SELECT id, digest FROM documents'))
        changes = ((identifier, text) for identifier, text in zip(identifiers, texts, strict=True)
                   if saved.get(identifier) != hashlib.sha256(text.encode()).hexdigest())
        self._write(changes, set(saved) - self.positions.keys())
        self._snapshot()

    def _writer(self):
        db = sqlite3.connect(self.path, timeout=120)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, digest TEXT NOT NULL, length INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS postings (token TEXT NOT NULL, doc TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                tf INTEGER NOT NULL, PRIMARY KEY(token, doc));
            CREATE INDEX IF NOT EXISTS postings_doc ON postings(doc);
            CREATE TABLE IF NOT EXISTS totals (id INTEGER PRIMARY KEY CHECK(id=1), count INTEGER, length INTEGER);
            INSERT OR IGNORE INTO totals VALUES (1, 0, 0);
        ''')
        return db

    def _write(self, changes, removed):
        with closing(self._writer()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            count, total = db.execute('SELECT count, length FROM totals WHERE id=1').fetchone()
            for identifier in removed:
                old = db.execute('SELECT length FROM documents WHERE id=?', (identifier,)).fetchone()
                if old is not None:
                    db.execute('DELETE FROM documents WHERE id=?', (identifier,))
                    count -= 1
                    total -= old[0]
            for identifier, text in changes.items() if hasattr(changes, 'items') else changes:
                terms = Counter(tokenize(text))
                length = sum(terms.values())
                old = db.execute('SELECT length FROM documents WHERE id=?', (identifier,)).fetchone()
                if old is None:
                    count += 1
                else:
                    total -= old[0]
                    db.execute('DELETE FROM postings WHERE doc=?', (identifier,))
                db.execute('INSERT INTO documents VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,length=excluded.length',
                           (identifier, hashlib.sha256(text.encode()).hexdigest(), length))
                db.executemany('INSERT INTO postings VALUES (?,?,?)',
                               ((token, identifier, frequency) for token, frequency in terms.items()))
                total += length
            db.execute('UPDATE totals SET count=?,length=? WHERE id=1', (count, total))

    def _snapshot(self):
        self.lock = RLock()
        self.reader = sqlite3.connect(self.path, timeout=120, check_same_thread=False)
        self.reader.execute('PRAGMA query_only=ON')
        self.reader.execute('BEGIN')
        self.count, total = self.reader.execute('SELECT count,length FROM totals WHERE id=1').fetchone()
        self.average_length = total / self.count if self.count else 0

    def with_updates(self, changes, removed):
        """发布新快照；现有 ID 的正文变更不复制全量位置映射。"""
        updated = object.__new__(type(self))
        updated.path = self.path
        removed = set(removed)
        if removed or changes.keys() - self.positions.keys():
            identifiers = [key for key in self.positions if key not in removed]
            identifiers += [key for key in changes if key not in self.positions]
            updated.positions = {key: i for i, key in enumerate(identifiers)}
        else:
            updated.positions = self.positions
        updated._write(changes, removed)
        updated._snapshot()
        return updated

    def search(self, query, limit):
        if limit < 1:
            raise ValueError('limit must be positive')
        if not self.average_length:
            return []
        scores = {}
        with self.lock:
            for token in dict.fromkeys(tokenize(query)):
                frequency = self.reader.execute('SELECT COUNT(*) FROM postings WHERE token=?', (token,)).fetchone()[0]
                if not frequency:
                    continue
                idf = math.log1p((self.count - frequency + .5) / (frequency + .5))
                rows = self.reader.execute('SELECT p.doc,p.tf,d.length FROM postings p JOIN documents d ON p.doc=d.id WHERE p.token=?', (token,))
                for identifier, tf, length in rows:
                    index = self.positions[identifier]
                    value = idf * (tf * 2.5 / (tf + 1.5 * (.25 + .75 * length / self.average_length)))
                    scores[index] = scores.get(index, 0.) + value
        return sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:limit]

    def close(self):
        if hasattr(self, 'reader'):
            self.reader.close()
            del self.reader

    def __del__(self):
        self.close()
