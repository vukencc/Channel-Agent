import json
import os

import pytest

from ai_agent_startup import config
from ai_agent_startup.tools import file_crud, file_extras, sandbox


@pytest.fixture
def roots(sandbox_env, tmp_path, monkeypatch):
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'a.txt').write_text('只读知识内容')
    monkeypatch.setattr(config, 'TOOL_ROOTS', {'docs': {'path': str(docs), 'read_only': True}}, raising=False)
    return docs


def test_named_root_read_list_stat_and_glob(roots):
    assert file_crud.read_file('@docs/a.txt') == '只读知识内容'
    assert '@docs/a.txt' in file_crud.list_files('@docs')
    assert json.loads(file_extras.stat('@docs/a.txt'))['size'] == (roots / 'a.txt').stat().st_size
    assert json.loads(file_extras.glob('@docs/*.txt'))['paths'] == ['@docs/a.txt']


@pytest.mark.parametrize('operation', [
    lambda: file_crud.create_file('@docs/new.txt', 'new'),
    lambda: file_crud.update_file('@docs/a.txt', 'new'),
    lambda: file_crud.edit_file('@docs/a.txt', '只读', '修改'),
    lambda: file_crud.append_file('@docs/a.txt', '新增', 6),
    lambda: file_crud.delete_file('@docs/a.txt'),
    lambda: file_extras.mkdir('@docs/new'),
    lambda: file_extras.move('@docs/a.txt', 'moved.txt'),
    lambda: file_extras.copy('@docs/a.txt', '@docs/new.txt'),
])
def test_all_writers_deny_read_only_root_before_confirmation(roots, monkeypatch, operation):
    def unexpected(*args):
        raise AssertionError('只读根的写请求必须在确认前拒绝')
    monkeypatch.setattr(sandbox, 'confirmer', unexpected)
    result = operation()
    assert '只读' in result
    assert (roots / 'a.txt').read_text() == '只读知识内容'
    assert list(roots.iterdir()) == [roots / 'a.txt']


def test_copy_from_read_only_root_keeps_confirmation(roots, sandbox_env, monkeypatch):
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    assert '取消' in file_extras.copy('@docs/a.txt', 'copied.txt')
    assert not (sandbox_env / 'copied.txt').exists()
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: True)
    assert '完成' in file_extras.copy('@docs/a.txt', 'copied.txt')
    assert (sandbox_env / 'copied.txt').read_text() == '只读知识内容'


def test_named_root_cannot_escape_by_parent_or_symlink(roots, tmp_path):
    secret = tmp_path / 'outside.txt'
    secret.write_text('不允许读取的内容')
    (roots / 'link.txt').symlink_to(secret)
    for path in ('@docs/../outside.txt', '@docs/link.txt', '@unknown/a.txt'):
        assert '拦截' in file_crud.read_file(path)


def test_rag_named_root_uses_confined_loader_without_global_config_mutation(roots, monkeypatch):
    from ai_agent_startup.tools import rag_search
    from ai_agent_startup.rag.index import DocLoader
    outside = roots.parent / 'outside.txt'
    outside.write_text('不应进入检索索引')
    (roots / 'escape.txt').symlink_to(outside)
    original = config.DOC_DIR
    seen = []
    def get_index(root=None):
        seen.extend(DocLoader(root, confined=True).load())
        return object()
    monkeypatch.setattr(rag_search, 'get_index', get_index, raising=False)
    monkeypatch.setattr(rag_search, '_rag_search', lambda *args, **kwargs: '检索桩')
    assert rag_search.rag_search('查询', source='@docs') == '检索桩'
    assert [row['content'] for row in seen] == ['只读知识内容']
    assert config.DOC_DIR == original


def test_actual_command_sandbox_does_not_mount_extra_roots(roots):
    import shlex
    from ai_agent_startup.tools.command import run_command
    result = run_command('test ! -e ' + shlex.quote(str(roots / 'a.txt')))
    assert '[退出码] 0' in result, result


@pytest.mark.integration
@pytest.mark.skipif(os.getenv('RUN_RAG_INTEGRATION') != '1', reason='需要显式启用本地模型')
def test_real_rag_reads_named_root_without_escaping(tmp_path, monkeypatch):
    import shutil
    from ai_agent_startup.tools import rag_search
    from ai_agent_startup.rag.index import get_index
    docs = tmp_path / 'docs'
    shutil.copytree(config.DOC_DIR, docs)
    outside = tmp_path / 'outside.txt'
    outside.write_text('这份文件不应被命名根索引读取。')
    (docs / 'outside-link.txt').symlink_to(outside)
    monkeypatch.setattr(config, 'TOOL_ROOTS', {'docs': {'path': str(docs), 'read_only': True}})
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'rag-cache')
    index = get_index(root=docs)
    assert index.parents
    assert all(row['metadata']['filename'] != 'outside-link.txt' for row in index.parents)
    result = rag_search.rag_search('蓝色花瓶在案件中有什么作用？', source='@docs', strictness='loose')
    assert '[检索状态]' in result and '来源:' in result and 'rerank=' in result
