"""可选 HNSW 索引；内容指纹持久化，默认精确检索不导入此依赖。"""
from core.file_lock import lock_file
import hashlib
import json
import os
import tempfile

import numpy as np

import config


class AnnIndex:
    def __init__(self, vectors: np.ndarray, directory):
        try:
            import hnswlib
        except ImportError:
            raise RuntimeError('ANN 需要可选依赖：uv sync --locked --extra ann') from None
        if vectors.ndim != 2 or not len(vectors):
            raise ValueError('ANN 需要非空二维向量矩阵')
        digest = hashlib.sha256()
        digest.update(json.dumps([vectors.shape, str(vectors.dtype), config.RAG_ANN_M,
                                  config.RAG_ANN_EF_CONSTRUCTION, 'ip-v1']).encode())
        # 按块哈希，避免为整个 memmap 分配 bytes 副本。
        for start in range(0, len(vectors), 4096):
            digest.update(np.ascontiguousarray(vectors[start:start + 4096]).view(np.uint8))
        self.identity = digest.hexdigest()
        self.count = len(vectors)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'{self.identity}.hnsw'
        self.index = hnswlib.Index(space='ip', dim=vectors.shape[1])
        self.loaded_from_disk = False
        with (directory / f'{self.identity}.lock').open('a') as lock:
            lock_file(lock)
            if path.exists():
                self.index.load_index(str(path), max_elements=self.count)
                self.loaded_from_disk = True
            else:
                self.index.init_index(max_elements=self.count, ef_construction=config.RAG_ANN_EF_CONSTRUCTION,
                                      M=config.RAG_ANN_M, random_seed=100)
                self.index.add_items(vectors, np.arange(self.count), num_threads=config.RAG_THREADS)
                descriptor, temporary = tempfile.mkstemp(prefix='.hnsw-', dir=directory)
                os.close(descriptor)
                try:
                    self.index.save_index(temporary)
                    with open(temporary, 'rb') as stream:
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
        self.index.set_ef(config.RAG_ANN_EF_SEARCH)
        self.index.set_num_threads(config.RAG_THREADS)

    def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if limit < 1:
            raise ValueError('ANN limit 必须为正数')
        labels, distances = self.index.knn_query(query, k=min(limit, self.count), num_threads=1)
        return [(int(index), float(1 - distance)) for index, distance in zip(labels[0], distances[0], strict=True)]
