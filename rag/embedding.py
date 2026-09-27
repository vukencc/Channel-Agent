"""Batched document/query embeddings with validated, normalized vectors."""
from functools import lru_cache
import hashlib
from pathlib import Path

import httpx
import numpy as np

import config

LOCAL_MODEL_NAME = 'BAAI/bge-small-zh-v1.5'
QUERY_INSTRUCTION = '为这个句子生成表示以用于检索相关文章：'


def local_model_path():
    if config.EMBEDDING_LOCAL_PATH:
        return config.EMBEDDING_LOCAL_PATH
    root = config.RAG_CACHE_DIR / 'models/models--Qdrant--bge-small-zh-v1.5'
    reference = root / 'refs/main'
    if reference.is_file():
        return root / 'snapshots' / reference.read_text().strip()
    snapshots = sorted((root / 'snapshots').glob('*'))
    return snapshots[0] if len(snapshots) == 1 else None


def artifact_signature(path):
    if not path:
        return ()
    return tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                 for p in sorted(Path(path).iterdir()) if p.is_file()
                 and p.suffix in {'.onnx', '.json', '.model', '.txt', '.safetensors', '.bin'}
                 and p.name != 'source.json')


@lru_cache(maxsize=4)
def artifact_hash(signature):
    digest = hashlib.sha256()
    for name, _, _ in signature:
        with open(name, 'rb') as stream:
            digest.update(hashlib.file_digest(stream, 'sha256').digest())
    return digest.hexdigest()


def normalize(vectors, count):
    values = np.asarray(vectors, dtype=np.float32)
    if values.ndim != 2 or len(values) != count or not np.isfinite(values).all():
        raise ValueError('Embedding response has invalid shape or non-finite values')
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError('Embedding response contains a zero vector')
    return values / norms


class EmbeddingModel:
    def __init__(self):
        self.source = config.EMBEDDING_MODEL_SOURCE.upper()
        self.batch_size = config.RAG_BATCH_SIZE
        if self.batch_size < 1:
            raise ValueError('RAG_BATCH_SIZE must be positive')
        if self.source == 'LOCAL':
            from fastembed import TextEmbedding
            path = local_model_path()
            self._model = TextEmbedding(
                model_name=LOCAL_MODEL_NAME,
                cache_dir=str(config.RAG_CACHE_DIR / 'models'),
                specific_model_path=str(path) if path else None,
                threads=config.RAG_THREADS,
            )
            self._artifact_hash = artifact_hash(artifact_signature(local_model_path()))
        elif self.source == 'API':
            if not config.EMBEDDING_MODEL_URL:
                raise ValueError('EMBEDDING_MODEL_SOURCE=API requires EMBEDDING_MODEL_URL')
            self._url = config.EMBEDDING_MODEL_URL
            self._api_key = config.EMBEDDING_MODEL_API_KEY
            self._model_name = config.EMBEDDING_MODEL_NAME
        else:
            raise ValueError(f'Unknown EMBEDDING_MODEL_SOURCE: {self.source}')

    @property
    def identity(self):
        # No API key in persisted metadata. Source/revision changes invalidate indexes.
        if self.source == 'LOCAL':
            return f'{LOCAL_MODEL_NAME}:{self._artifact_hash}:normalized-v2'
        endpoint = hashlib.sha256(f'{self._url}\n{self._model_name}'.encode()).hexdigest()
        return f'api:{endpoint}:normalized-v2'

    def embed(self, texts: list[str]) -> np.ndarray:
        """Compatibility alias: encode documents without query instructions."""
        return self.embed_documents(texts)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0), dtype=np.float32)
        order = np.argsort([len(text) for text in texts], kind='stable') if self.source == 'LOCAL' else np.arange(len(texts))
        texts = [texts[i] for i in order]
        batches = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            if self.source == 'LOCAL':
                vectors = list(self._model.embed(batch, batch_size=self.batch_size))
            else:
                response = httpx.post(
                    self._url, headers={'Authorization': f'Bearer {self._api_key}'},
                    json={'model': self._model_name, 'input': batch}, timeout=config.TIMEOUT,
                )
                response.raise_for_status()
                rows = sorted(response.json()['data'], key=lambda item: item['index'])
                if [row['index'] for row in rows] != list(range(len(batch))):
                    raise ValueError('Embedding API returned missing or duplicate indexes')
                vectors = [row['embedding'] for row in rows]
            batches.append(normalize(vectors, len(batch)))
        return np.concatenate(batches)[np.argsort(order)]

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        if self.source == 'LOCAL':
            texts = [QUERY_INSTRUCTION + text for text in texts]
        return self.embed_documents(texts)


@lru_cache(maxsize=2)
def _get_model(key):
    return EmbeddingModel()


def get_embedding_model() -> EmbeddingModel:
    return _get_model((
        config.EMBEDDING_MODEL_SOURCE, config.EMBEDDING_MODEL_URL,
        config.EMBEDDING_MODEL_NAME, config.EMBEDDING_MODEL_API_KEY,
        config.EMBEDDING_LOCAL_PATH, config.RAG_CACHE_DIR,
        config.RAG_BATCH_SIZE, config.RAG_THREADS,
        artifact_signature(local_model_path()) if config.EMBEDDING_MODEL_SOURCE.upper() == 'LOCAL' else (),
    ))
