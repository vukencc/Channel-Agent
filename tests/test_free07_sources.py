from datetime import datetime, timezone
import os

import pytest

from ai_agent_startup import config
from test_hybrid_rag import CountingEmbedding
from ai_agent_startup.rag.index import RetrievalIndex, DocLoader
from ai_agent_startup.rag.tool import rag_search


def test_time_filter_applies_before_candidate_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(config, 'RAG_CANDIDATES', 1)
    monkeypatch.setattr(config, 'RAG_RERANK_TOP_N', 1)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    class ScoreStub:
        def score(self, query, passages):
            return [1.0 for _ in passages]
    monkeypatch.setattr('ai_agent_startup.rag.tool.get_reranker', ScoreStub)
    index = RetrievalIndex([
        {'content': '关键词 ' * 10, 'metadata': {'filename': 'old.txt', 'updated_at': 10}},
        {'content': '关键词', 'metadata': {'filename': 'new.txt', 'updated_at': 20}},
    ])
    trace = {}
    result = rag_search('关键词', top_k=1, index=index, trace=trace, updated_after=15)
    assert 'new.txt' in result and 'old.txt' not in result
    assert all([row['metadata']['filename'] for row in trace['stages'][stage]] == ['new.txt']
               for stage in ('vector', 'bm25', 'rrf', 'rerank'))


def test_timestamp_tracking_invalidates_same_content_mtime_change(tmp_path):
    path = tmp_path / 'doc.txt'
    path.write_text('内容不变')
    os.utime(path, (10, 10))
    loader = DocLoader(tmp_path, track_updates=True)
    assert loader.load()[0]['metadata']['updated_at'] == 10
    before = loader.fingerprint
    os.utime(path, (20, 20))
    assert loader.load()[0]['metadata']['updated_at'] == 20
    assert before != loader.fingerprint


def test_named_library_is_explicit_and_does_not_mutate_global(tmp_path, monkeypatch):
    from ai_agent_startup.tools import rag_search as tool
    root = tmp_path / 'kb'
    root.mkdir()
    monkeypatch.setattr(config, 'RAG_SOURCES', {'kb': {'path': str(root)}}, raising=False)
    before = config.DOC_DIR
    captured = []
    monkeypatch.setattr(tool, 'get_index', lambda **kwargs: captured.append(kwargs) or object())
    monkeypatch.setattr(tool, '_rag_search', lambda *args, **kwargs: '检索测试桩')
    assert tool.rag_search('查询', source='kb', top_k=2,
                          updated_after=datetime(2020, 1, 1, tzinfo=timezone.utc)) == '检索测试桩'
    assert captured[0] == {'root': root, 'track_updates': True}
    assert config.DOC_DIR == before
    assert '拦截' in tool.rag_search('查询', source='../arbitrary')


def test_tool_rejects_unbounded_top_k_and_ambiguous_time():
    from ai_agent_startup.tools.rag_search import RagSearchArgs
    with pytest.raises(ValueError):
        RagSearchArgs(query='q', top_k=1000000)
    with pytest.raises(ValueError):
        RagSearchArgs(query='q', updated_after='2026-01-01')


def test_parallel_libraries_keep_content_and_lexical_namespaces_separate(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from ai_agent_startup.rag.index import get_index
    roots = [tmp_path / name for name in ('first', 'second')]
    for root, content in zip(roots, ('第一库苹果', '第二库香蕉')):
        root.mkdir()
        (root / 'same.txt').write_text(content)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(config, 'RAG_BM25_PERSIST', True)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    with ThreadPoolExecutor(max_workers=2) as pool:
        indexes = list(pool.map(get_index, roots))
    assert [index.parents[0]['document'] for index in indexes] == ['第一库苹果', '第二库香蕉']
    assert indexes[0].bm25('香蕉', 2) == []
    assert indexes[1].bm25('苹果', 2) == []
    assert len(list((tmp_path / 'cache' / 'lexical').glob('*.sqlite'))) == 2


@pytest.mark.integration
@pytest.mark.skipif(os.getenv('RUN_RAG_INTEGRATION') != '1', reason='需要显式启用本地模型')
def test_real_named_library_top_k_and_time_filter(tmp_path, monkeypatch):
    import shutil
    from ai_agent_startup.tools.rag_search import rag_search as tool_search
    root = tmp_path / 'library'
    shutil.copytree(config.DOC_DIR, root)
    monkeypatch.setattr(config, 'RAG_SOURCES', {'library': {'path': str(root)}})
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'cache')
    result = tool_search('蓝色花瓶在案件中有什么作用？', source='library', top_k=1,
                         strictness='loose', updated_after=datetime(1970, 1, 1, tzinfo=timezone.utc))
    assert '最多 1 条' in result and '来源:' in result
    empty = tool_search('蓝色花瓶', source='library', updated_after=datetime(2100, 1, 1, tzinfo=timezone.utc))
    assert '没有符合修改时间条件' in empty
