import numpy as np
import pytest


def test_ann_persists_and_matches_simple_exact_neighbors(tmp_path):
    pytest.importorskip('hnswlib', reason='真实 ANN 回归需 --extra ann')
    from ai_agent_startup.rag.vector_backend import AnnIndex
    vectors = np.eye(64, dtype=np.float32)
    first = AnnIndex(vectors, tmp_path)
    assert first.search(vectors[7], 1)[0][0] == 7
    assert list(tmp_path.glob('*.hnsw'))
    restored = AnnIndex(vectors, tmp_path)
    assert restored.loaded_from_disk
    assert restored.search(vectors[9], 1)[0][0] == 9


def test_ann_content_changes_invalidate_cache(tmp_path):
    pytest.importorskip('hnswlib', reason='真实 ANN 回归需 --extra ann')
    from ai_agent_startup.rag.vector_backend import AnnIndex
    vectors = np.eye(4, dtype=np.float32)
    first = AnnIndex(vectors, tmp_path)
    changed = vectors[::-1].copy()
    second = AnnIndex(changed, tmp_path)
    assert first.identity != second.identity
    assert second.search(vectors[0], 1)[0][0] == 3


def test_missing_ann_dependency_reports_exact_fallback(tmp_path, monkeypatch, caplog):
    import sys
    from ai_agent_startup import config
    from ai_agent_startup.rag.index import RetrievalIndex
    from test_hybrid_rag import CountingEmbedding
    monkeypatch.setitem(sys.modules, 'hnswlib', None)
    monkeypatch.setattr(config, 'RAG_VECTOR_BACKEND', 'ann')
    monkeypatch.setattr(config, 'RAG_ANN_MIN_CHILDREN', 1)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    index = RetrievalIndex([{'content': 'a', 'metadata': {'filename': 'a'}}])
    assert index.dense('q', 1)[0]['id'] == '["a",0]'
    assert index.vector_backend == 'exact'
    assert '回退 exact' in caplog.text
