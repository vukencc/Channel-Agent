import pytest

from rag.lexical import BM25Index


def test_persistent_bm25_matches_formula_and_warm_start(tmp_path, monkeypatch):
    from rag import lexical_store
    texts = ['apple apple pear', 'pear plum', 'plum']
    path = tmp_path / 'bm25.sqlite'
    first = lexical_store.PersistentBM25(path, texts, ['a', 'b', 'c'])
    assert first.search('apple', 10)[0][0] == 0
    assert first.search('apple', 10)[0][1] == pytest.approx(BM25Index(texts).search('apple', 10)[0][1])
    def forbidden(text):
        pytest.fail('持久索引重启不应重新分词')
    with monkeypatch.context() as patch:
        patch.setattr(lexical_store, 'tokenize', forbidden)
        restored = lexical_store.PersistentBM25(path, texts, ['a', 'b', 'c'])
    assert restored.search('pear', 10)


def test_incremental_snapshot_preserves_old_reader_and_removes_terms(tmp_path):
    from rag.lexical_store import PersistentBM25
    first = PersistentBM25(tmp_path / 'bm25.sqlite', ['apple pear', 'plum'], ['a', 'b'])
    changed = first.with_updates({'a': 'banana pear'}, ['b'])
    assert first.search('apple', 2)
    assert not changed.search('apple plum', 2)
    assert changed.search('banana', 2)[0][0] == 0


def test_scores_and_order_match_memory_backend_after_document_changes(tmp_path):
    from rag.lexical_store import PersistentBM25
    texts = ['苹果 apple pear', 'apple apple', '梨 pear', '', 'apple pear 梨']
    ids = list(map(str, range(len(texts))))
    persistent = PersistentBM25(tmp_path / 'bm25.sqlite', texts, ids)
    for query in ['apple pear', '苹果 梨', 'absent', '']:
        assert persistent.search(query, 10) == BM25Index(texts).search(query, 10)
    texts[1] = 'new 梨'
    changed = PersistentBM25(tmp_path / 'bm25.sqlite', texts, ids)
    for query in ['apple pear', 'new 梨']:
        assert changed.search(query, 10) == BM25Index(texts).search(query, 10)
