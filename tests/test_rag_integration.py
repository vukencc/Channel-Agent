"""Opt-in real models: RUN_RAG_INTEGRATION=1 uv run pytest -m integration.

These assert execution and trace integrity, not a hand-picked quality target.
Quality is measured separately on the frozen public benchmark.
"""
import os

import pytest

import config
from rag.index import get_index
from rag.tool import rag_search
from tools import TOOL_REGISTRY

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('RUN_RAG_INTEGRATION') != '1', reason='Set RUN_RAG_INTEGRATION=1 to run downloaded models')]


@pytest.fixture
def actual_corpus(monkeypatch, tmp_path):
    from rag.embedding import local_model_path
    from rag.rerank import local_reranker_path
    monkeypatch.setattr(config, 'EMBEDDING_LOCAL_PATH', local_model_path())
    monkeypatch.setattr(config, 'RERANK_LOCAL_PATH', local_reranker_path())
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'model-cache')
    monkeypatch.setattr(config, 'DOC_DIR', config.PROJECT_ROOT / 'data/raw')
    return get_index()


def test_real_full_pipeline_and_registered_tool(actual_corpus):
    query = '蓝色花瓶在案件中有什么作用？'
    trace = {}
    text = rag_search(query, index=actual_corpus, trace=trace, strictness='loose')
    assert all(trace['stages'][name] for name in ('vector', 'bm25', 'rrf', 'rerank'))
    union = {row['id'] for name in ('vector', 'bm25') for row in trace['stages'][name]}
    assert {row['id'] for row in trace['stages']['rrf']} == union
    assert {row['id'] for row in trace['stages']['rerank']} <= union
    assert len({row['score'] for row in trace['stages']['rerank']}) > 1
    for rows in trace['stages'].values():
        assert [row['score'] for row in rows] == sorted([row['score'] for row in rows], reverse=True)
        for row in rows:
            source = open(row['metadata']['source'], encoding='utf-8').read()
            assert source[row['metadata']['start']:row['metadata']['end']] == row['document']
    registered = TOOL_REGISTRY['rag_search'].run('{"query":"蓝色花瓶在案件中有什么作用？","strictness":"loose"}')
    assert registered == text


def test_real_strictness_gates_only_after_reranking(actual_corpus):
    traces = []
    for strictness in ('strict', 'normal', 'loose'):
        trace = {}
        rag_search('画家为什么锁门？', index=actual_corpus, strictness=strictness, trace=trace)
        traces.append(trace)
    assert traces[0]['stages'] == traces[1]['stages'] == traces[2]['stages']
    assert set(traces[0]['selected_ids']) <= set(traces[1]['selected_ids']) <= set(traces[2]['selected_ids'])
