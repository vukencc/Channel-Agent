import pytest

import config


def test_byte_cache_evicts_by_bytes_and_not_only_entry_count():
    from rag.text_store import ByteCache
    cache = ByteCache(100)
    cache.put('a', 'a' * 80, 80)
    cache.put('b', 'b' * 80, 80)
    assert cache.get('a') is None
    assert cache.get('b') == 'b' * 80
    assert cache.bytes <= 100


def test_memory_mode_preserves_text_offsets_and_does_not_retain_raw_source(tmp_path, monkeypatch):
    from rag.index import DocLoader
    monkeypatch.setattr(config, 'RAG_MEMORY_LIMIT_MB', 1024, raising=False)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'cache')
    root = tmp_path / 'docs'
    root.mkdir()
    text = '公开文档正文。' * 300
    (root / 'a.txt').write_text(text)
    document = DocLoader(root).load()[0]
    assert document['content'] == text
    assert 'content' not in vars(document), '正文应由磁盘内容寻址存储按需提供'


def test_low_memory_limit_fails_before_large_index_allocation(monkeypatch):
    from rag.text_store import check_memory_budget
    monkeypatch.setattr(config, 'RAG_MEMORY_LIMIT_MB', 1, raising=False)
    with pytest.raises(MemoryError, match='RAG_MEMORY_LIMIT_MB'):
        check_memory_budget()
