from pathlib import Path

from rag.index import DocLoader


def test_thousand_unchanged_files_are_not_reread(tmp_path, monkeypatch):
    for i in range(1000):
        (tmp_path / f'{i}.txt').write_text(f'document {i}')
    loader = DocLoader(tmp_path)
    loader.load()
    reads = []
    original = Path.read_text
    def counted(path, *args, **kwargs):
        if path.parent == tmp_path:
            reads.append(path.name)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', counted)
    assert len(loader.load()) == 1000
    assert reads == []
    (tmp_path / '1.txt').write_text('updated')
    loader.load()
    assert reads == ['1.txt']


def test_html_loader_removes_scripts(tmp_path):
    (tmp_path / 'page.html').write_text('<h1>标题</h1><script>secret()</script><p>正文</p>')
    docs = DocLoader(tmp_path).load()
    assert len(docs) == 1
    assert '标题' in docs[0]['content'] and '正文' in docs[0]['content']
    assert 'secret' not in docs[0]['content']


def test_same_stat_fingerprint_still_detects_write_event(tmp_path, monkeypatch):
    path = tmp_path / 'a.txt'
    path.write_text('aa')
    loader = DocLoader(tmp_path)
    loader.load()
    original = Path.stat
    frozen = original(path)
    monkeypatch.setattr(Path, 'stat', lambda p, **kw: frozen if p == path else original(p, **kw))
    path.write_text('bb')
    assert loader.load()[0]['content'] == 'bb'
