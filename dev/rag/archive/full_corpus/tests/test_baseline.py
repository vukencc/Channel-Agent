"""Archived baseline compatibility check; run explicitly only."""
import os
import pytest
import config
from rag.index import get_index
pytestmark = pytest.mark.skipif(os.getenv("RUN_RAG_INTEGRATION") != "1", reason="Requires local models")

@pytest.fixture
def actual_corpus(monkeypatch, tmp_path):
    from rag.embedding import local_model_path
    from rag.rerank import local_reranker_path
    monkeypatch.setattr(config, 'EMBEDDING_LOCAL_PATH', local_model_path())
    monkeypatch.setattr(config, 'RERANK_LOCAL_PATH', local_reranker_path())
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'model-cache')
    monkeypatch.setattr(config, 'DOC_DIR', config.PROJECT_ROOT / 'data/raw')
    return get_index()


def test_baseline_adapter_matches_frozen_original_cosine_search(actual_corpus):
    """Use real embeddings to verify the offline baseline's ranking semantics."""
    import importlib.util
    import sys
    from dev.rag.archive.full_corpus.evaluate import Baseline
    from rag.index import DocLoader
    baseline = Baseline(DocLoader(config.DOC_DIR).load())
    path = config.PROJECT_ROOT / 'dev/rag/archive/full_corpus/baseline/tool.py'
    spec = importlib.util.spec_from_file_location('frozen_tool', path)
    original = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = original
    spec.loader.exec_module(original)
    original.get_embedding_model = lambda: baseline.model
    store = original.SimpleVectorStore()
    store.vectors = baseline.vectors
    store.documents = [{'content': text, 'metadata': {'parent': parent}}
                       for parent, text in baseline.children]
    query = '凶手如何制造密室？'
    hits = store.search(query, len(store.documents), 0.45)
    expected, seen = [], set()
    for hit in hits:
        parent = hit['metadata']['parent']
        if parent not in seen:
            seen.add(parent)
            expected.append((baseline.parents[parent]['id'], hit['score']))
    actual = baseline.search(query)
    assert [row['id'] for row in actual] == [identifier for identifier, _ in expected[:50]]
    assert [row['score'] for row in actual] == pytest.approx([score for _, score in expected[:50]], abs=1e-5)
