"""Algorithm/contract tests. Fixtures here do not measure retrieval quality."""
import numpy as np
import pytest

from ai_agent_startup import config
from ai_agent_startup.rag.chunking import TextSplitter
from ai_agent_startup.rag.embedding import normalize
from ai_agent_startup.rag.fusion import reciprocal_rank_fusion
from ai_agent_startup.rag.index import DocLoader, RetrievalIndex, cached_embeddings
from ai_agent_startup.rag.lexical import BM25Index, tokenize
from ai_agent_startup.rag.tool import rag_search


def test_rrf_known_ranks_dedup_and_missing_channel():
    rows = reciprocal_rank_fusion([
        [{'id': 'a'}, {'id': 'a'}, {'id': 'b'}],
        [{'id': 'b'}, {'id': 'c'}],
    ], k=60)
    assert [row['id'] for row in rows] == ['b', 'a', 'c']
    assert rows[0]['score'] == pytest.approx(1 / 62 + 1 / 61)
    assert rows[1]['score'] == pytest.approx(1 / 61)
    assert rows[2]['ranks'] == {'1': 2}


def test_rrf_ties_are_stable_and_empty():
    assert reciprocal_rank_fusion([[], []]) == []
    assert [row['id'] for row in reciprocal_rank_fusion([[{'id': 'z'}], [{'id': 'a'}]])] == ['a', 'z']
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([], 0)


def test_bm25_independent_formula_and_small_corpus():
    import math
    index = BM25Index(['apple apple pear', 'pear plum', 'plum'])
    # apple appears once among 3 docs; average document length = 2.
    expected = math.log1p(2.5 / 1.5) * (2 * 2.5) / (2 + 1.5 * (0.25 + 0.75 * 3 / 2))
    assert index.search('apple', 10) == [(0, pytest.approx(expected))]
    assert BM25Index(['苹果']).search('苹果', 5)[0][0] == 0
    assert BM25Index(['苹果', '香蕉']).search('火星', 10) == []
    assert BM25Index([]).search('苹果', 5) == []
    assert BM25Index(['!!!']).search('???', 5) == []


def test_tokenization_normalizes_chinese_latin_and_numbers():
    assert tokenize('ＡＰＩ １２３ 苹果') == tokenize('api 123 苹果')
    assert '苹果' in tokenize('苹果价格')
    assert tokenize('，。！') == []


def test_bounded_chunks_cover_nonwhitespace_and_keep_offsets():
    text = '第一句。' * 120 + '\n\n' + 'x' * 1000
    splitter = TextSplitter(parent_chars=70, child_chars=25)
    chunks = splitter.split_parents(text)
    assert ''.join(t for t, _, _ in chunks) == text.replace('\n', '')
    for parent, start, end in chunks:
        assert text[start:end] == parent
        assert len(parent) <= 70
        for child, a, b in splitter.split_children(parent):
            assert len(child) <= 25
            assert text[start + a:start + b] == child


class CountingEmbedding:
    identity = 'test-only-fixed-vectors'

    def __init__(self):
        self.inputs = []

    def embed_documents(self, texts):
        self.inputs.extend(texts)
        return np.tile([1., 0.], (len(texts), 1))

    def embed_queries(self, texts):
        return self.embed_documents(texts)


def test_vector_cache_reuses_and_invalidates_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    model = CountingEmbedding()
    first = cached_embeddings(['a', 'a', 'b'], model)
    assert model.inputs == ['a', 'b']
    assert np.array_equal(first, cached_embeddings(['a', 'a', 'b'], model))
    assert model.inputs == ['a', 'b']
    cached_embeddings(['a', 'c'], model)
    assert model.inputs == ['a', 'b', 'c']
    model.identity = 'changed-model'
    cached_embeddings(['a'], model)
    assert model.inputs[-1] == 'a' and len(model.inputs) == 4


def test_docloader_recursive_paths_and_missing(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    (tmp_path / 'a' / 'same.txt').write_text('first')
    (tmp_path / 'b' / 'same.txt').write_text('second')
    assert [r['metadata']['doc_id'] for r in DocLoader(tmp_path).load()] == ['a/same.txt', 'b/same.txt']
    with pytest.raises(ValueError):
        DocLoader(tmp_path / 'missing').load()


def test_parent_offsets_and_distinct_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    text = '首段。\n\n  第二段。第二句。'
    index = RetrievalIndex([{'content': text, 'metadata': {'filename': 'a.txt'}}])
    for child in index.children:
        assert text[child['start']:child['end']] == child['content']
    assert len({r['id'] for r in index.dense('test', 10)}) == 2


def test_cache_detects_same_size_content_and_doc_dir_change(tmp_path, monkeypatch):
    from ai_agent_startup.rag.index import get_index
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    directory = tmp_path / 'docs'
    directory.mkdir()
    monkeypatch.setattr(config, 'DOC_DIR', directory)
    path = directory / 'a.txt'
    path.write_text('aa')
    first = get_index()
    assert get_index() is first
    path.write_text('bb')
    second = get_index()
    assert second is not first
    assert second.parents[0]['document'] == 'bb'


@pytest.mark.parametrize('query,top_k', [('', None), ('  ', None), ('q', 0), ('q', -1), ('q', True)])
def test_invalid_requests_fail_before_model_load(query, top_k):
    with pytest.raises(ValueError):
        rag_search(query, top_k=top_k)


def test_empty_corpus_skips_models_and_populates_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DOC_DIR', tmp_path)
    trace = {}
    assert '知识库为空' in rag_search('test', trace=trace)
    assert all(not rows for rows in trace['stages'].values())


@pytest.mark.parametrize('vectors', [[[0, 0]], [[float('nan'), 1]], [[1, 2], [3, 4]], [1, 2]])
def test_invalid_embeddings_rejected(vectors):
    with pytest.raises(ValueError):
        normalize(vectors, 1)


def test_rerank_errors_propagate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    index = RetrievalIndex([{'content': '苹果', 'metadata': {'filename': 'a'}}])
    def fail():
        raise RuntimeError('model load failed')
    monkeypatch.setattr('ai_agent_startup.rag.tool.get_reranker', fail)
    with pytest.raises(RuntimeError, match='model load failed'):
        rag_search('苹果', index=index)


def test_vector_and_bm25_union_reaches_real_rerank_interface(tmp_path, monkeypatch):
    """Isolated contract check; deliberately artificial ranks, not a quality claim."""
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    monkeypatch.setattr('ai_agent_startup.rag.index.get_embedding_model', CountingEmbedding)
    monkeypatch.setattr(config, 'RAG_CANDIDATES', 1)
    monkeypatch.setattr(config, 'RAG_RERANK_TOP_N', 2)
    index = RetrievalIndex([
        {'content': '苹果', 'metadata': {'filename': 'a'}},
        {'content': '香蕉', 'metadata': {'filename': 'b'}},
    ])
    seen = []
    class Scorer:
        def score(self, query, passages):
            seen.extend(passages)
            return list(range(len(passages)))
    monkeypatch.setattr('ai_agent_startup.rag.tool.get_reranker', Scorer)
    trace = {}
    rag_search('香蕉', top_k=1, strictness='loose', index=index, trace=trace)
    assert set(seen) == {'苹果', '香蕉'}
    assert trace['stages']['vector'][0]['document'] == '苹果'
    assert trace['stages']['bm25'][0]['document'] == '香蕉'
    assert trace['stages']['rerank'][0]['document'] == seen[-1]


def test_duplicate_document_ids_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path)
    with pytest.raises(ValueError, match='Duplicate document ID'):
        RetrievalIndex([{'content': 'a', 'metadata': {'filename': 'x'}},
                        {'content': 'b', 'metadata': {'filename': 'x'}}])


def test_packed_children_preserve_full_content_and_offsets():
    splitter = TextSplitter(parent_chars=200, child_chars=12)
    text = '第一句。第二句。 第三句！还有一段特别长的文字需要分开。尾句。'
    chunks = splitter.pack_children(text)
    assert len(chunks) < len(splitter.split_children(text))
    assert ''.join(piece for piece, _, _ in chunks).replace(' ', '') == text.replace(' ', '')
    for piece, start, end in chunks:
        assert piece == text[start:end]
        assert len(piece) <= 12


def test_api_embedding_order_and_validation(monkeypatch):
    from ai_agent_startup.rag.embedding import EmbeddingModel
    monkeypatch.setattr(config, 'EMBEDDING_MODEL_SOURCE', 'API')
    monkeypatch.setattr(config, 'EMBEDDING_MODEL_URL', 'https://example.test/embeddings')
    monkeypatch.setattr(config, 'EMBEDDING_MODEL_API_KEY', 'test-only')
    payload = [{'index': 1, 'embedding': [0, 2]}, {'index': 0, 'embedding': [3, 0]}]
    requests = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'data': payload}
    def post(url, **kwargs):
        requests.append(kwargs['json']['input'])
        return Response()
    monkeypatch.setattr('ai_agent_startup.rag.embedding.httpx.post', post)
    model = EmbeddingModel()
    assert np.array_equal(model.embed_documents(['a', 'longer']), [[1, 0], [0, 1]])
    assert requests == [['a', 'longer']]
    payload[:] = [{'index': 0, 'embedding': [1, 0]}]
    model.embed_queries(['question'])
    assert requests[-1] == ['question']  # No BGE instruction sent to an arbitrary API.
    payload[:] = [{'index': 1, 'embedding': [1, 0]}]
    with pytest.raises(ValueError, match='missing or duplicate'):
        model.embed_documents(['a'])


def test_model_fingerprint_ignores_download_locks_but_tracks_weights(tmp_path):
    from ai_agent_startup.rag.embedding import artifact_hash, artifact_signature
    weights = tmp_path / 'model.onnx'
    weights.write_bytes(b'original weights')
    before = artifact_signature(tmp_path)
    digest = artifact_hash(before)
    (tmp_path / 'model.onnx.lock').touch()
    (tmp_path / 'model.onnx.part').write_bytes(b'incomplete download')
    assert artifact_signature(tmp_path) == before
    assert artifact_hash(artifact_signature(tmp_path)) == digest
    weights.write_bytes(b'changed model weights')
    assert artifact_hash(artifact_signature(tmp_path)) != digest
