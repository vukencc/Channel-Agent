import json

import pytest

import config
from core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_MEMORY_MANAGEMENT', True, raising=False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def test_namespaces_are_explicit_and_default_stays_isolated(store):
    first, second = store.create('a', 's'), store.create('b', 's')
    store.remember(first['id'], '会话事实')
    store.remember(first['id'], '项目事实', namespace='project')
    store.remember(first['id'], '全局事实', namespace='global')
    assert not store.memory(second['id'])
    assert '项目事实' in store.memory(second['id'], namespace='project')
    assert '全局事实' in store.memory(second['id'], namespace='global')
    assert '会话事实' not in store.memory(second['id'], namespace='project')
    with pytest.raises(ValueError):
        store.memory(first['id'], namespace='../escape')


def test_expired_memory_is_retained_but_not_injected(store):
    identifier = store.create('a', 's')['id']
    store.remember(identifier, '已过期的事实', expires_at='2000-01-01T00:00:00Z')
    store.remember(identifier, '仍有效的事实')
    assert '已过期' in store.memory(identifier)
    assert '已过期' not in store.memory_for_model(identifier, '事实')
    assert '仍有效' in store.memory_for_model(identifier, '事实')


def test_search_filters_and_atomic_edit_keep_id_and_limits(store, monkeypatch):
    identifier = store.create('a', 's')['id']
    store.remember(identifier, '苹果项目', tags=['project'], source='manual')
    store.remember(identifier, '香蕉项目', tags=['other'], source='import')
    results = store.search_memory(identifier, '项目', tags=['project'], source='manual')
    assert [entry['text'] for entry in results] == ['苹果项目']
    entry_id = results[0]['id']
    store.edit_memory(identifier, entry_id, {'text': '新的苹果项目', 'tags': ['edited']})
    assert store.search_memory(identifier, '苹果', tags=['edited'])[0]['id'] == entry_id
    before = store.memory(identifier)
    monkeypatch.setattr(config, 'MEMORY_MAX_CHARS', 1)
    with pytest.raises(ValueError, match='MEMORY_MAX_CHARS'):
        store.edit_memory(identifier, entry_id, {'text': '越过预算的文本'})
    assert store.memory(identifier) == before
    rows = [json.loads(line) for line in (store.directory(identifier) / 'audit.jsonl').read_text().splitlines()]
    assert any(row.get('event') == 'memory_edit' for row in rows)


def test_management_cli_search_edit_confirmation_and_namespace_restore(store):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from core.cli import AgentCLI
    with create_pipe_input() as pipe:
        cli = AgentCLI(store, input=pipe, output=DummyOutput())
        cli.handle('/memory scope project')
        cli.handle('/memory add {"text":"苹果项目","tags":["project"],"expires_at":"2100-01-01T00:00:00Z"}')
        entry = store.memory_entries(cli.current, 'project')[0]
        cli.handle('/memory search {"query":"苹果","tags":["project"]}')
        assert '苹果项目' in cli.notice
        command = '/memory edit ' + entry['id'] + ' {"text":"修改后的项目"}'
        cli.handle(command)
        assert '确认修改记忆' in cli.notice
        cli.handle('/no')
        assert '苹果项目' in store.memory(cli.current, 'project')
        cli.handle(command)
        cli.handle('/yes')
        assert '修改后的项目' in store.memory(cli.current, 'project')
        assert store.load_all()[0]['memory_namespace'] == 'project'
        exported = json.loads(store.export(cli.active.record, 'json').read_text())
        assert '修改后的项目' in exported['memory']


def test_management_namespace_requires_explicit_flag(store, monkeypatch):
    identifier = store.create('a', 's')['id']
    monkeypatch.setattr(config, 'ENABLE_MEMORY_MANAGEMENT', False)
    with pytest.raises(ValueError, match='ENABLE_MEMORY_MANAGEMENT'):
        store.memory(identifier, 'global')
    store.remember(identifier, '默认隔离仍可用')
    assert '默认隔离' in store.memory(identifier)
