"""缓存接口单元测试；小型计数模型不用于检索质量评估。"""
import numpy as np

from ai_agent_startup import config
from ai_agent_startup.rag.index import RetrievalIndex
from ai_agent_startup.rag.rerank import Reranker
from test_hybrid_rag import CountingEmbedding


def test_query_vector_cache_is_bounded_and_model_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr(config, 'RAG_QUERY_CACHE_SIZE', 1, raising=False)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    index = RetrievalIndex([{'content': 'document', 'metadata': {'filename': 'a'}}])
    before = len(index.model.inputs)
    first = index.dense('one', 1)
    assert index.dense('one', 1) == first
    assert len(index.model.inputs) == before + 1
    index.dense('two', 1)
    index.dense('one', 1)
    assert len(index.model.inputs) == before + 3


def test_rerank_cache_invalidates_content_and_query(monkeypatch):
    monkeypatch.setattr(config, 'RAG_RERANK_CACHE_SIZE', 8, raising=False)
    class Tokenizer:
        def encode(self, text, **kwargs):
            return list(map(ord, text))
        def decode(self, tokens, **kwargs):
            return ''.join(map(chr, tokens))
        def num_special_tokens_to_add(self, **kwargs):
            return 3
    class Model:
        tokenizer = Tokenizer()
        calls = 0
        def predict(self, pairs, **kwargs):
            self.calls += 1
            return np.asarray([len(passage) for _, passage in pairs], dtype=float)
    scorer = Reranker.__new__(Reranker)
    scorer.model = Model()
    scorer.identity = 'test-interface'
    assert scorer.score('q', ['a', 'bb']) == [1, 2]
    assert scorer.score('q', ['a', 'bb']) == [1, 2]
    assert scorer.model.calls == 1
    assert scorer.score('q', ['a', 'bbb']) == [1, 3]
    assert scorer.model.calls == 2
    scorer.score('different', ['a'])
    assert scorer.model.calls == 3


def test_breadth_candidate_override_is_explicit(tmp_path, monkeypatch):
    from ai_agent_startup.rag.tool import rag_search
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    monkeypatch.setattr(config, 'RAG_RERANK_BY_BREADTH', {'narrow': 3})
    index = RetrievalIndex([{'content': str(i), 'metadata': {'filename': str(i)}} for i in range(10)])
    class Scorer:
        def score(self, query, passages):
            return [0.] * len(passages)
    monkeypatch.setattr('ai_agent_startup.rag.tool.get_reranker', Scorer)
    trace = {}
    rag_search('q', index=index, breadth='narrow', trace=trace)
    assert len(trace['stages']['rerank']) == 3
    rag_search('q', index=index, breadth='normal', trace=trace)
    assert len(trace['stages']['rerank']) == 10
