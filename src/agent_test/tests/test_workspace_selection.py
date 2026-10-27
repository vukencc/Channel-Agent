"""用户选择宿主项目目录，仍保持会话授权与真实工具边界。"""
import asyncio
import json
from pathlib import Path
import threading

import pytest

from agent_core import config
from agent_core.core.session_service import open_session
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()


def call(name, **arguments):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def test_selected_project_crud_child_inheritance_and_restart(store, tmp_path):
    project = tmp_path / 'user-project'
    project.mkdir()
    (project / 'existing.txt').write_text('keep')

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: True)
        root = open_session(manager, workspace_path=str(project))
        child = manager.create('child', parent=root)
        assert manager.workspace(root) == manager.workspace(child) == project
        assert '完成' in await manager._tool(root, call('create_file', path='new.txt', content='data'))
        assert await manager._tool(child, call('read_file', path='new.txt')) == 'data'
        assert '拦截' in await manager._tool(child, call('read_file', path='../outside.txt'))
        await manager.flush()
        await manager.shutdown()
        restored = SessionManager(store)
        try:
            assert restored.workspace(restored.sessions[root.id]) == project
            assert restored.workspace(restored.sessions[child.id]) == project
            restored.set_workspace_path(restored.sessions[root.id], None)
            assert restored.workspace(restored.sessions[child.id]) == store.workspace(root.id)
            assert (project / 'existing.txt').read_text() == 'keep'
            assert (project / 'new.txt').read_text() == 'data'
        finally:
            await restored.shutdown()
    asyncio.run(run())


def test_workspace_selection_refuses_private_dangerous_and_missing_paths(store, tmp_path):
    manager = SessionManager(store)
    root = manager.create()
    paths = ['relative', '/', str(Path.home()), str(config.PROJECT_ROOT),
             str(config.PROJECT_ROOT / 'src'), str(store.root), str(store.root.parent),
             str(tmp_path / 'missing'), str(store.workspace_root)]
    for path in paths:
        with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
            manager.set_workspace_path(root, path)
    assert manager.workspace(root) == store.workspace(root.id)


def test_workspace_switch_requires_entire_shared_group_idle_and_preserves_files(store, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    manager = SessionManager(store)
    root = manager.create()
    child = manager.create('child', parent=root)
    old = manager.workspace(root)
    (old / 'old.txt').write_text('old')
    with pytest.raises(PermissionError):
        manager.set_workspace_path(child, str(project))
    child.tool_workers.add(object())
    with pytest.raises(ValueError):
        manager.set_workspace_path(root, str(project))
    child.tool_workers.clear()
    manager.set_workspace_path(root, str(project))
    assert manager.workspace(child) == project
    assert (old / 'old.txt').read_text() == 'old'
    assert list(project.iterdir()) == []


def test_selected_workspace_refuses_symlink_or_directory_replacement(store, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    manager = SessionManager(store)
    root = manager.create(workspace_path=str(project))
    original = tmp_path / 'original'
    project.rename(original)
    project.symlink_to(original, target_is_directory=True)
    with pytest.raises(PermissionError):
        manager.workspace(root)
    project.unlink()
    project.mkdir()
    with pytest.raises(PermissionError):
        manager.workspace(root)
    assert list(project.iterdir()) == []


def test_external_project_command_uses_only_selected_root(store, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('private')

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: True)
        root = manager.create(workspace_path=str(project))
        try:
            result = await manager._tool(root, call('run_command',
                command="pwd; printf selected > command.txt; test ! -e ../private.txt",
                reason='验证选择目录与隔离边界'))
            assert '[退出码] 0' in result, result
            assert '/workspace' in result
            assert (project / 'command.txt').read_text() == 'selected'
            assert outside.read_text() == 'private'
            assert not store.workspace_path(root.id).exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('replace_root', [False, True])
def test_cleanup_protects_selected_project_even_when_binding_is_invalid(store, tmp_path, monkeypatch, replace_root):
    from agent_core.core.maintenance import inspect_cache, inspect_logs
    project = tmp_path / 'project'
    project.mkdir()
    manager = SessionManager(store)
    root = manager.create(workspace_path=str(project))
    if replace_root:
        project.rename(tmp_path / 'original')
        project.mkdir()
        with pytest.raises(PermissionError):
            manager.workspace(root)
    sentinel = project / 'keep.log'
    sentinel.write_text('user data')
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', project)
    with pytest.raises(PermissionError):
        inspect_cache(manager, remove=True)
    monkeypatch.setattr(config, 'AUDIT_LOG', sentinel)
    with pytest.raises(PermissionError):
        inspect_logs(manager, clear=True)
    assert sentinel.read_text() == 'user data'


def test_model_schema_cannot_select_host_workspace():
    from pydantic import ValidationError
    from agent_core.tools.session_tools import CreateSessionArgs
    with pytest.raises(ValidationError):
        CreateSessionArgs(task='inspect', workspace_path='/tmp/project')


def test_archived_external_workspace_remains_protected_after_restart(store, tmp_path, monkeypatch):
    from agent_core.core.maintenance import inspect_cache
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'keep.txt').write_text('keep')

    async def run():
        manager = SessionManager(store)
        root = manager.create(workspace_path=str(project))
        await manager.flush()
        await manager.delete_session(root)
        await manager.shutdown()
        restored = SessionManager(store)
        try:
            monkeypatch.setattr(config, 'RAG_CACHE_DIR', project)
            with pytest.raises(PermissionError):
                inspect_cache(restored, remove=True)
            assert (project / 'keep.txt').read_text() == 'keep'
        finally:
            await restored.shutdown()
    asyncio.run(run())


def test_workspace_record_cannot_forge_external_authorization(store, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    manager = SessionManager(store)
    root = manager.create()
    info = project.stat()
    root.record.update(workspace_path=str(project), workspace_identity=[info.st_dev, info.st_ino])
    with pytest.raises(PermissionError):
        manager.workspace(root)


def test_queued_child_prevents_root_workspace_switch(store, tmp_path, monkeypatch):
    from agent_core.core.agent_tasks import AgentTasks
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    project = tmp_path / 'project'
    project.mkdir()

    async def run():
        manager = SessionManager(store)
        manager.agent_tasks = AgentTasks(manager)
        root = manager.create()
        try:
            await manager.agent_tasks.start(root, 'queued work', ['read_file'], delay_seconds=60)
            with pytest.raises(ValueError):
                manager.set_workspace_path(root, str(project))
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('directory', [False, True])
def test_preview_reader_refuses_replaced_workspace_before_reading(tmp_path, directory):
    from agent_frontend.web.files import directory_entries, text_preview
    project = tmp_path / 'project'
    project.mkdir()
    info = project.stat()
    identity = (info.st_dev, info.st_ino)
    project.rename(tmp_path / 'original')
    project.mkdir()
    (project / 'private.txt').write_text('private')
    operation = directory_entries if directory else text_preview
    target = project if directory else project / 'private.txt'
    with pytest.raises(PermissionError):
        operation(project, target, expected_identity=identity)


@pytest.mark.parametrize('action', ['create_file', 'update_file', 'delete_file', 'edit_file',
                                   'append_file', 'mkdir', 'move', 'copy'])
def test_file_mutation_revalidates_root_after_confirmation(tmp_path, action):
    from agent_core.tools import file_crud, file_extras
    from agent_core.tools.sandbox import ToolContext, tool_context
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'file.txt').write_text('original')
    info = project.stat()

    def replace_root(*_):
        project.rename(tmp_path / 'original')
        project.mkdir()
        (project / 'file.txt').write_text('original')
        return True

    operation = getattr(file_crud, action, None) or getattr(file_extras, action)
    arguments = {
        'create_file': ('new.txt', 'new'), 'update_file': ('file.txt', 'changed'),
        'delete_file': ('file.txt',), 'edit_file': ('file.txt', 'original', 'changed'),
        'append_file': ('file.txt', 'appended', 8), 'mkdir': ('new-directory',),
        'move': ('file.txt', 'moved.txt'), 'copy': ('file.txt', 'copied.txt'),
    }[action]
    context = ToolContext(project, tmp_path / 'audit.jsonl', replace_root, threading.Event(),
                          permission_policy='standard', root_identity=(info.st_dev, info.st_ino))
    with tool_context(context):
        result = operation(*arguments)
    assert '完成' not in result
    assert (project / 'file.txt').read_text() == 'original'
    assert sorted(item.name for item in project.iterdir()) == ['file.txt']
    assert (tmp_path / 'original' / 'file.txt').read_text() == 'original'


@pytest.mark.parametrize('directory', [False, True])
def test_web_preview_checks_opened_root_identity(store, tmp_path, monkeypatch, directory):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from agent_frontend.web import app as web_app
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'file.txt').write_text('visible')
    with TestClient(web_app.create_app(store=store), base_url='http://localhost') as client:
        state = client.post('/api/sessions', json={'workspace_path': str(project)}).json()
        name = 'directory_entries' if directory else 'text_preview'
        original = getattr(web_app, name)

        def replace_after_validation(root, target, **kwargs):
            project.rename(tmp_path / 'original')
            project.mkdir()
            (project / 'file.txt').write_text('hidden-private-content')
            return original(root, target, **kwargs)

        monkeypatch.setattr(web_app, name, replace_after_validation)
        route = 'files' if directory else 'file'
        response = client.get(f'/api/sessions/{state["id"]}/{route}',
                              params={'path': '.' if directory else 'file.txt'})
        assert response.status_code == 403
        assert 'hidden-private-content' not in response.text


def test_web_can_create_browse_and_switch_existing_projects(store, tmp_path):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from agent_frontend.web.app import create_app
    project = tmp_path / 'project'
    other = tmp_path / 'other'
    project.mkdir()
    other.mkdir()
    (project / 'linked').symlink_to(other, target_is_directory=True)
    (project / '.agent').mkdir()
    (project / '.agent' / 'keep').write_text('private')
    with TestClient(create_app(store=store), base_url='http://localhost') as client:
        denied = client.post('/api/sessions', json={'workspace_path': str(store.root)})
        assert denied.status_code == 403
        assert not client.get('/api/sessions').json()['sessions']
        listing = client.get('/api/workspaces/directories', params={'path': str(tmp_path)})
        assert listing.status_code == 200, listing.text
        assert str(project) in {item['path'] for item in listing.json()['directories']}
        listing = client.get('/api/workspaces/directories', params={'path': str(project)}).json()
        assert 'linked' not in {item['name'] for item in listing['directories']}
        assert '.agent' not in {item['name'] for item in listing['directories']}
        created = client.post('/api/sessions', json={'workspace_path': str(other)})
        assert created.status_code == 201, created.text
        state = created.json()
        assert state['workspace_path'] == str(other) and state['workspace_custom']
        other.rename(tmp_path / 'original')
        other.mkdir()
        invalid = client.get('/api/sessions/' + state['id']).json()
        assert invalid['workspace_path'] == str(other) and invalid['workspace_error']
        changed = client.patch('/api/sessions/' + state['id'], json={'workspace_path': None})
        assert changed.status_code == 200, changed.text
        assert not changed.json()['workspace_custom']
        assert (project / '.agent' / 'keep').read_text() == 'private'
