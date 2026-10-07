"""Offline checks for archived history ranking and source routing."""
import importlib
import asyncio
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from ai_agent_startup import config
from ai_agent_startup.core import history_search
from ai_agent_startup.core.session_service import session_service_context


def _chunks():
    return [
        {'ID': 'a' * 32, 'Summary': 'alpha design notes', 'RawHistory': 'private alpha payload'},
        {'ID': 'b' * 32, 'Summary': 'beta release notes', 'RawHistory': 'private beta payload'},
        {'ID': 'c' * 32, 'Summary': 'gamma release notes', 'RawHistory': 'private gamma payload'},
    ]


def test_bm25_search_has_real_scores_and_excludes_raw_history():
    rows = history_search.search_chunks(_chunks(), 'alpha', limit=2)
    assert [row['ID'] for row in rows] == ['a' * 32]
    assert rows[0]['score'] > 0
    assert rows[0]['retrieval']['bm25_score'] == rows[0]['score']
    assert rows[0]['retrieval']['vector_score'] is None
    assert 'RawHistory' not in rows[0]
    assert 'private alpha payload' not in repr(rows)


def test_hybrid_uses_real_vector_rrf_and_rerank_scores(monkeypatch):
    class Embedding:
        identity = 'test-local-model'

        def embed_documents(self, texts):
            assert texts == [row['Summary'] for row in _chunks()]
            return np.array([[0.0, 1.0], [1.0, 0.0], [0.8, 0.6]], dtype=np.float32)

        def embed_queries(self, texts):
            assert texts == ['release']
            return np.array([[1.0, 0.0]], dtype=np.float32)

    class Reranker:
        def score(self, query, passages):
            assert query == 'release'
            return [2.0 if 'gamma' in text else 1.0 for text in passages]

    monkeypatch.setattr(history_search, '_local_embedding_available', lambda: True)
    monkeypatch.setattr(history_search, '_local_reranker_available', lambda: True)
    monkeypatch.setattr(history_search, 'get_embedding_model', lambda: Embedding())
    monkeypatch.setattr(history_search, 'get_reranker', lambda: Reranker())
    rows = history_search.search_chunks(_chunks(), 'release', limit=3, method='hybrid')
    assert rows[0]['ID'] == 'c' * 32
    assert rows[0]['score'] == 2.0
    assert rows[0]['retrieval']['effective_method'] == 'hybrid'
    assert rows[0]['retrieval']['vector_score'] == pytest.approx(0.8)
    assert rows[0]['retrieval']['bm25_score'] > 0
    assert rows[0]['retrieval']['rrf_score'] > 0
    assert rows[0]['retrieval']['rerank_score'] == 2.0
    assert all('RawHistory' not in row for row in rows)


def test_hybrid_reports_model_absence_without_fake_scores(monkeypatch):
    monkeypatch.setattr(history_search, '_local_embedding_available', lambda: False)
    rows = history_search.search_chunks(_chunks(), 'release', limit=2, method='hybrid')
    assert rows
    assert all(row['retrieval']['effective_method'] == 'bm25' for row in rows)
    assert all(row['retrieval']['vector_score'] is None for row in rows)
    assert all(row['retrieval']['rrf_score'] is None for row in rows)
    assert all(row['retrieval']['rerank_score'] is None for row in rows)
    assert all(row['retrieval']['degraded_reason'] for row in rows)


def test_rag_history_source_routes_to_hybrid_without_touching_document_index(monkeypatch):
    rag_tool = importlib.import_module('ai_agent_startup.tools.rag_search')
    history_tool = importlib.import_module('ai_agent_startup.tools.context_history')
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True, raising=False)
    monkeypatch.setattr(rag_tool, '_rag_search', lambda *_a, **_kw: pytest.fail('document RAG ran'))
    seen = []

    def fake_history(query, limit=5, method='bm25'):
        seen.append((query, limit, method))
        return '[{"ID":"' + 'a' * 32 + '"}]'

    monkeypatch.setattr(history_tool, 'history_search', fake_history)
    monkeypatch.setattr(rag_tool, 'audit', lambda *_a, **_kw: None)
    result = rag_tool.rag_search('alpha', source='history', breadth='wide')
    assert seen == [('alpha', 8, 'hybrid')]
    assert 'a' * 32 in result
    with pytest.raises(ValueError, match='strictness'):
        rag_tool.rag_search('alpha', source='history', strictness='loose')


def test_history_search_output_is_complete_json_and_summary_only(monkeypatch):
    history_tool = importlib.import_module('ai_agent_startup.tools.context_history')
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 230)
    rows = [{'ID': 'a' * 32, 'Summary': 'x' * 1000, 'RawHistory': 'secret',
             'score': 1.0, 'retrieval': {'effective_method': 'bm25'}}]
    payload = history_tool._search_json(rows)
    assert len(payload) <= config.TOOL_MAX_OUTPUT
    parsed = json.loads(payload)
    assert parsed[0]['ID'] == 'a' * 32
    assert parsed[0]['summary_truncated'] is True
    assert 'secret' not in payload


def test_history_tools_pass_only_the_current_session_to_service(monkeypatch):
    history_tool = importlib.import_module('ai_agent_startup.tools.context_history')
    monkeypatch.setattr(history_tool, 'audit', lambda *_a, **_kw: None)
    owner = SimpleNamespace(id='owner')

    class History:
        async def search(self, session, query, limit=5, method='bm25'):
            assert session is owner
            assert (query, limit, method) == ('needle', 2, 'bm25')
            return [{'ID': 'a' * 32, 'Summary': 'found', 'RawHistory': 'secret'}]

        async def read(self, session, chunk_id, before=0, after=0, offset=0, limit=6000):
            assert session is owner
            assert chunk_id == 'a' * 32
            return {'chunks': [{'ID': chunk_id, 'RawHistory': '[{"role":"user"}]'}],
                    'next_offset': None, 'total_chars': 17}

    manager = SimpleNamespace(history_context=History(), sessions={'owner': owner}, closing=False)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        with session_service_context(manager, 'owner', loop):
            found = json.loads(history_tool.history_search('needle', limit=2))
            assert found == [{'ID': 'a' * 32, 'Summary': 'found'}]
            detail = json.loads(history_tool.history_read('a' * 32, limit=100))
            assert detail['chunks'][0]['RawHistory'] == '[{"role":"user"}]'
        with session_service_context(manager, 'other', loop):
            with pytest.raises(PermissionError, match='归档'):
                history_tool.history_search('needle')
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
