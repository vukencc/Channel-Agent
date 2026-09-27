"""Content-addressed embeddings and a reusable parent/child retrieval index."""
import hashlib
import fcntl
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

import numpy as np

import config
from rag.chunking import TextSplitter
from rag.embedding import get_embedding_model, artifact_signature, local_model_path
from rag.lexical import BM25Index


@dataclass
class DocLoader:
    dir: Path

    def load(self) -> list[dict]:
        root = Path(self.dir).resolve()
        if not root.is_dir():
            raise ValueError(f'DOC_DIR is not a directory: {root}')
        return [
            {'content': path.read_text(encoding='utf-8'),
             'metadata': {'source': str(path), 'filename': path.relative_to(root).as_posix(),
                          'doc_id': path.relative_to(root).as_posix()}}
            for path in sorted(root.rglob('*'))
            if path.is_file() and path.suffix.lower() in {'.txt', '.md'}
        ]


def cached_embeddings(texts: list[str], model, progress=None) -> np.ndarray:
    """Serialize builds per model across processes; completed matrices remain read-only."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    directory = config.RAG_CACHE_DIR / 'vectors'
    directory.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(model.identity.encode()).hexdigest()
    with (directory / f'{identity}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _cached_embeddings(texts, model, progress)


def _cached_embeddings(texts: list[str], model, progress=None) -> np.ndarray:
    """Persist each content vector; transactions allow safe interrupted-build resume."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    directory = config.RAG_CACHE_DIR / 'vectors'
    directory.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(model.identity.encode()).hexdigest()
    sequence = hashlib.sha256()
    for text in texts:
        sequence.update(hashlib.sha256(text.encode()).digest())
    matrix_path = directory / f'{identity}-{sequence.hexdigest()}.npy'
    complete_path = matrix_path.with_suffix('.complete')
    if matrix_path.exists() and complete_path.exists():
        return np.load(matrix_path, mmap_mode='r', allow_pickle=False)
    with sqlite3.connect(directory / f'{identity}.sqlite', timeout=120) as db:
        db.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, value BLOB NOT NULL)')
        matrix = None
        for start in range(0, len(texts), 512):
            batch = texts[start:start + 512]
            keys = [hashlib.sha256(text.encode()).hexdigest() for text in batch]
            unique = dict(zip(keys, batch))
            placeholders = ','.join('?' for _ in unique)
            found = dict(db.execute(f'SELECT key,value FROM vectors WHERE key IN ({placeholders})', list(unique)))
            missing = [key for key in unique if key not in found]
            if missing:
                values = model.embed_documents([unique[key] for key in missing])
                added = [(key, vector.astype(np.float32).tobytes()) for key, vector in zip(missing, values, strict=True)]
                db.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?)', added)
                db.commit()
                found.update(added)
            rows = np.stack([np.frombuffer(found[key], dtype=np.float32) for key in keys])
            if matrix is None:
                matrix = np.lib.format.open_memmap(matrix_path, mode='w+', dtype=np.float32,
                                                   shape=(len(texts), rows.shape[1]))
            matrix[start:start + len(rows)] = rows
            if progress:
                progress(min(start + 512, len(texts)), len(texts), len(missing))
    matrix.flush()
    complete_path.touch()
    return np.load(matrix_path, mmap_mode='r', allow_pickle=False)


class RetrievalIndex:
    def __init__(self, documents: list[dict], progress=None):
        self.parents: list[dict] = []
        self.children: list[dict] = []
        splitter = TextSplitter(config.RAG_PARENT_CHARS, config.RAG_CHILD_CHARS)
        seen_docs = set()
        for doc in documents:
            metadata = dict(doc['metadata'])
            doc_id = str(metadata.get('doc_id', metadata['filename']))
            if doc_id in seen_docs:
                raise ValueError(f'Duplicate document ID: {doc_id}')
            seen_docs.add(doc_id)
            for pi, (text, start, end) in enumerate(splitter.split_parents(doc['content'])):
                parent_index = len(self.parents)
                parent_id = json.dumps([doc_id, pi], ensure_ascii=False, separators=(',', ':'))
                self.parents.append({'id': parent_id, 'document': text, 'metadata': {
                    **metadata, 'doc_id': doc_id, 'parent': pi, 'start': start, 'end': end,
                }})
                for ci, (child, a, b) in enumerate(splitter.pack_children(text)):
                    self.children.append({'content': child, 'parent_index': parent_index,
                                          'child': ci, 'start': start + a, 'end': start + b})
        self.by_id = {parent['id']: parent for parent in self.parents}
        self.parent_indexes = np.array([child['parent_index'] for child in self.children], dtype=np.int32)
        self.model = get_embedding_model() if self.children else None
        self.vectors = cached_embeddings([row['content'] for row in self.children], self.model, progress)
        self.lexical = BM25Index([parent['document'] for parent in self.parents])

    def dense(self, query: str, limit: int) -> list[dict]:
        if not self.children:
            return []
        vector = self.model.embed_queries([query])[0]
        if self.vectors.shape[1] != len(vector):
            raise ValueError('Query/document embedding dimensions differ')
        child_scores = self.vectors @ vector
        parent_scores = np.full(len(self.parents), -np.inf, dtype=np.float32)
        np.maximum.at(parent_scores, self.parent_indexes, child_scores)
        # Parent list is deterministic; ties retain document/chunk order.
        order = np.argsort(-parent_scores, kind='stable')[:limit]
        return [{'id': self.parents[i]['id'], 'score': float(parent_scores[i])} for i in order]

    def bm25(self, query: str, limit: int) -> list[dict]:
        return [{'id': self.parents[i]['id'], 'score': score}
                for i, score in self.lexical.search(query, limit)]


_lock = RLock()
_cached_key = None
_cached_index = None


def get_index() -> RetrievalIndex:
    """Read content to detect same-size/mtime edits; reuse all expensive work."""
    global _cached_key, _cached_index
    documents = DocLoader(config.DOC_DIR).load()
    digest = hashlib.sha256()
    for doc in documents:
        digest.update(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode())
    key = (str(config.DOC_DIR), digest.hexdigest(), config.RAG_PARENT_CHARS,
           config.RAG_CHILD_CHARS, config.EMBEDDING_MODEL_SOURCE,
           config.EMBEDDING_MODEL_URL, config.EMBEDDING_MODEL_NAME,
           config.EMBEDDING_MODEL_API_KEY, config.EMBEDDING_LOCAL_PATH,
           config.RAG_CACHE_DIR, config.RAG_BATCH_SIZE, config.RAG_THREADS)
    if config.EMBEDDING_MODEL_SOURCE.upper() == 'LOCAL':
        key += (artifact_signature(local_model_path()),)
    with _lock:
        if key != _cached_key:
            index = RetrievalIndex(documents)
            _cached_key, _cached_index = key, index
        return _cached_index
