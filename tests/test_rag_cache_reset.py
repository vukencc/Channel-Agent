"""Offline resource eviction and lazy rebuild contracts for cache maintenance."""
import subprocess
import sys

import pytest


def test_reset_does_not_import_unloaded_rag_components():
    result = subprocess.run(
        [sys.executable, '-c', '''
import sys
from ai_agent_startup.rag.cache import clear_runtime_caches
before = set(sys.modules)
clear_runtime_caches()
assert set(sys.modules) == before
assert 'ai_agent_startup.rag.index' not in sys.modules
assert 'ai_agent_startup.rag.embedding' not in sys.modules
assert 'ai_agent_startup.rag.rerank' not in sys.modules
assert 'numpy' not in sys.modules
assert 'torch' not in sys.modules
'''], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_reset_closes_resources_evicts_models_and_rebuilds(tmp_path, monkeypatch):
    import shutil
    import sqlite3
    import weakref

    import numpy as np

    from ai_agent_startup import config
    from ai_agent_startup.rag import embedding, index, lexical, rerank, text_store
    from ai_agent_startup.rag.cache import clear_runtime_caches
    from ai_agent_startup.rag.result_cache import ResultCache

    clear_runtime_caches()
    cache_dir = tmp_path / 'cache'
    root = tmp_path / 'documents'
    root.mkdir()
    document = root / 'source.txt'
    document.write_text('apple apple pear', encoding='utf-8')
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', cache_dir)
    monkeypatch.setattr(config, 'RAG_MEMORY_LIMIT_MB', 1024)
    monkeypatch.setattr(config, 'RAG_BM25_PERSIST', True)
    monkeypatch.setattr(config, 'RAG_VECTOR_BACKEND', 'exact')
    monkeypatch.setattr(config, 'EMBEDDING_MODEL_SOURCE', 'API')
    monkeypatch.setattr(config, 'RAG_QUERY_CACHE_SIZE', 8)
    builds = []

    class Model:
        identity = 'offline-reset-test'

        def __init__(self):
            builds.append('embedding')

        def embed_documents(self, texts):
            return np.tile([1., 0.], (len(texts), 1))

        def embed_queries(self, texts):
            return self.embed_documents(texts)

    class Scorer:
        def __init__(self):
            builds.append('reranker')
            self.score_cache = ResultCache()
            self.score_cache.put('score', 1., 8)

    monkeypatch.setattr(embedding, 'EmbeddingModel', Model)
    monkeypatch.setattr(rerank, 'Reranker', Scorer)
    try:
        first = index.get_index(root)
        assert index.get_index(root) is first
        assert first.dense('apple', 1)
        lexical._cached_tokens('cached tokens')
        embedding.artifact_hash(())
        store = text_store.text_store(cache_dir)
        text_key = store.put('retained text')
        store.flush()
        assert store.get(text_key) == 'retained text'
        text_cache = text_store.shared_cache()
        assert text_cache.items and text_cache.bytes > 0
        assert first.query_cache.items
        scorer = rerank._get_reranker(('offline',))
        scores = scorer.score_cache
        scorer_ref = weakref.ref(scorer)
        del scorer
        model_ref = weakref.ref(first.model)
        vector_mapping = first.vectors._mmap
        lexical_connection = first.lexical.reader
        watcher = index._file_events[root.resolve()]
        sentinel = cache_dir / 'preserve-until-disk-cleanup.txt'
        sentinel.write_text('keep', encoding='utf-8')

        clear_runtime_caches()
        clear_runtime_caches()  # Idempotent, including externally retained resources.
        assert index._cached_index is None and index._cached_key is None
        assert not index._document_cache and not index._file_events
        assert watcher.fd == -1 and vector_mapping.closed
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            lexical_connection.execute('SELECT 1')
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            store.get(text_key)
        assert not first.query_cache.items and not scores.items
        assert not text_cache.items and text_cache.bytes == 0
        assert index._split_cached.cache_info().currsize == 0
        assert lexical._cached_tokens.cache_info().currsize == 0
        assert embedding.artifact_hash.cache_info().currsize == 0
        assert embedding._get_model.cache_info().currsize == 0
        assert rerank._get_reranker.cache_info().currsize == 0
        assert model_ref() is None and scorer_ref() is None
        assert sentinel.read_text(encoding='utf-8') == 'keep'
        assert document.read_text(encoding='utf-8') == 'apple apple pear'

        # Disk removal belongs to maintenance, after runtime resources are closed.
        shutil.rmtree(cache_dir)
        second = index.get_index(root)
        assert second is not first and index.get_index(root) is second
        assert second.parents[0]['document'] == 'apple apple pear'
        assert second.dense('apple', 1)
        assert text_store.text_store(cache_dir) is not store
        assert rerank._get_reranker(('offline',)) is not scorer_ref()
        assert builds.count('embedding') == 2
        assert builds.count('reranker') == 2
    finally:
        clear_runtime_caches()


def test_reset_clears_result_caches_held_by_callers():
    from ai_agent_startup.rag.cache import clear_runtime_caches
    from ai_agent_startup.rag.result_cache import ResultCache

    cached = ResultCache()
    cached.put('result', object(), 1)
    assert cached.get('result', 1) is not None
    clear_runtime_caches()
    assert cached.get('result', 1) is None
