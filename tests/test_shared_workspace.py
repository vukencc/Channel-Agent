"""Shared parent/child workspaces keep session identity and sandbox limits."""

import asyncio
import json
import threading

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()


def _call(name, arguments):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}


async def _create_child(manager, parent, *, tool_names=None):
    requested = ['read_file', 'create_file', 'send_session_message', 'read_session_messages'] if tool_names is None else tool_names
    arguments = {
        'task': 'inspect the project', 'tool_names': requested,
        'max_rounds': 1,
    }
    result = json.loads(await manager._tool(parent, _call('create_session', arguments)))
    await manager.agent_tasks.tasks[result['task_id']]
    return manager.sessions[result['session_id']], result


def test_shared_child_reads_and_writes_parent_workspace_with_sandbox_boundary(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent, unrelated = manager.create('parent'), manager.create('unrelated')
        manager.set_workspace_mode(parent, 'shared')
        child, _ = await _create_child(manager, parent)
        assert manager.workspace(parent) == manager.workspace(child)
        assert manager.workspace(unrelated) != manager.workspace(child)
        assert '完成' in await manager._tool(parent, _call('create_file', {'path': 'parent.txt', 'content': 'from parent'}))
        assert await manager._tool(child, _call('read_file', {'path': 'parent.txt'})) == 'from parent'
        assert '完成' in await manager._tool(child, _call('create_file', {'path': 'child.txt', 'content': 'from child'}))
        assert await manager._tool(parent, _call('read_file', {'path': 'child.txt'})) == 'from child'
        assert '不存在' in await manager._tool(unrelated, _call('read_file', {'path': 'child.txt'}))
        assert '拦截' in await manager._tool(child, _call('read_file', {'path': '../other.txt'}))
        await manager.shutdown()

    asyncio.run(run())


def test_child_shares_parent_workspace_without_mode_selection(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        child, _ = await _create_child(manager, parent, tool_names=['read_file'])
        assert manager.workspace(parent) == manager.workspace(child)
        assert manager.workspace(child) == store.workspace(parent.id)
        assert store.directory(child.id) != store.directory(parent.id)
        await manager.shutdown()

    asyncio.run(run())


def test_shared_child_communication_subset_matches_prompt_and_real_tools(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        manager.set_tool_names(parent, ['create_session', 'read_file', 'send_session_message', 'read_session_messages'])
        manager.set_workspace_mode(parent, 'shared')
        child, created = await _create_child(manager, parent, tool_names=['read_file', 'send_session_message', 'read_session_messages'])
        assert created['communication_tools'] == ['read_session_messages', 'send_session_message']
        assert set(created['communication_tools']) <= set(child.record['tool_names'])
        assert 'send_session_message' in child.record['messages'][0]['content']
        sent = json.loads(await manager._tool(child, _call('send_session_message', {
            'session_id': parent.id, 'message': 'finished',
        })))
        received = json.loads(await manager._tool(parent, _call('read_session_messages', {})))
        assert received['messages'] == [sent]
        assert '拒绝' in await manager._tool(child, _call('create_file', {'path': 'denied.txt', 'content': 'x'}))
        await manager.shutdown()

    asyncio.run(run())


def test_shared_workspace_persists_and_survives_parent_soft_delete(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        manager.set_workspace_mode(parent, 'shared')
        child, _ = await _create_child(manager, parent, tool_names=['read_file'])
        path = manager.workspace(parent)
        (path / 'keep.txt').write_text('persisted', encoding='utf-8')
        await manager.flush()
        await manager.shutdown()

        restored = SessionManager(store, model=model)
        parent, child = restored.sessions[parent.id], restored.sessions[child.id]
        assert restored.workspace(child) == restored.workspace(parent) == path
        assert await restored._tool(child, _call('read_file', {'path': 'keep.txt'})) == 'persisted'
        await restored.delete_session(parent)
        assert path.is_dir()
        assert (path / 'keep.txt').read_text(encoding='utf-8') == 'persisted'
        assert restored.workspace(child) == path
        await restored.shutdown()

    asyncio.run(run())


def test_child_cannot_change_workspace_owner_or_mode(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent, unrelated = manager.create('parent'), manager.create('unrelated')
        manager.set_workspace_mode(parent, 'shared')
        child, _ = await _create_child(manager, parent, tool_names=['read_file'])
        with pytest.raises((PermissionError, ValueError)):
            manager.set_workspace_mode(child, 'isolated')
        with pytest.raises((PermissionError, ValueError)):
            manager.set_workspace_mode(child, unrelated.id)
        assert manager.workspace(child) == manager.workspace(parent)
        assert manager.workspace(child) != manager.workspace(unrelated)
        await manager.shutdown()

    asyncio.run(run())


def test_denied_shared_child_creation_keeps_parent_workspace_private(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: False)
        parent = manager.create('parent')
        unrelated = manager.create('unrelated')
        manager.set_workspace_mode(parent, 'shared')
        response = await manager._tool(parent, _call('create_session', {
            'task': 'inspect', 'tool_names': ['read_file'],
        }))
        assert '取消' in response or '拒绝' in response
        assert set(manager.sessions) == {parent.id, unrelated.id}
        assert manager.workspace(parent) != manager.workspace(unrelated)
        await manager.shutdown()

    asyncio.run(run())


def test_shared_child_cannot_forge_workspace_owner_after_creation(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent, unrelated = manager.create('parent'), manager.create('unrelated')
        manager.set_workspace_mode(parent, 'shared')
        child, _ = await _create_child(manager, parent, tool_names=['read_file'])
        original_owner = child.record['workspace_owner_id']
        child.record['workspace_owner_id'] = unrelated.id
        try:
            with pytest.raises((PermissionError, ValueError)):
                manager.workspace(child)
        finally:
            child.record['workspace_owner_id'] = original_owner
        original_parent = child.record['delegated_from']
        child.record['delegated_from'] = unrelated.id
        try:
            with pytest.raises((PermissionError, ValueError)):
                manager.workspace(child)
        finally:
            child.record['delegated_from'] = original_parent
        assert manager.workspace(child) == manager.workspace(parent)
        await manager.shutdown()

    asyncio.run(run())


def test_shared_child_audit_and_task_controls_use_child_identity(store, monkeypatch):
    from ai_agent_startup.core import sessions
    from ai_agent_startup.tools import TOOL_REGISTRY
    from ai_agent_startup.tools.agent_tasks import AgentTaskArgs, task_status
    from ai_agent_startup.tools.base import Tool

    monkeypatch.setitem(TOOL_REGISTRY, 'task_status', Tool('task_status', AgentTaskArgs, task_status, concurrency='read'))
    monkeypatch.setattr(sessions, 'TOOL_SCHEMAS', [tool.schema for tool in TOOL_REGISTRY.values()])

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        try:
            parent = manager.create('parent')
            manager.set_workspace_mode(parent, 'shared')
            child, created = await _create_child(manager, parent, tool_names=['read_file', 'task_status'])
            (manager.workspace(parent) / 'notes.txt').write_text('notes')
            assert await manager._tool(child, _call('read_file', {'path': 'notes.txt'})) == 'notes'
            rows = [json.loads(line) for line in (store.directory(child.id) / 'audit.jsonl').read_text().splitlines()]
            assert any(row.get('event') == 'executed' and row.get('action') == 'read_file' for row in rows)
            assert all(row['session_id'] == child.id for row in rows)
            with pytest.raises(PermissionError):
                await manager._tool(child, _call('task_status', {'task_id': created['task_id']}))
            result = json.loads(await manager._tool(parent, _call('task_status', {'task_id': created['task_id']})))
            assert result['owner'] == parent.id
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('command_jobs_enabled', [False, True])
def test_shared_child_read_waits_for_workspace_writer(store, monkeypatch, command_jobs_enabled):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', command_jobs_enabled)

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        entered, release = threading.Event(), threading.Event()
        holder = None
        try:
            parent = manager.create('parent')
            manager.set_workspace_mode(parent, 'shared')
            child, _ = await _create_child(manager, parent, tool_names=['read_file'])
            root = manager.workspace(parent)
            (root / 'notes.txt').write_text('safe content', encoding='utf-8')
            guard = manager.command_jobs.guard if command_jobs_enabled else manager.workspaces.guard

            def hold_writer():
                with guard(root, threading.Event()):
                    entered.set()
                    release.wait(timeout=5)

            holder = threading.Thread(target=hold_writer, daemon=True)
            holder.start()
            assert await asyncio.to_thread(entered.wait, 2)
            read = asyncio.create_task(manager._tool(child, _call('read_file', {'path': 'notes.txt'})))
            for _ in range(200):
                if any(row['name'] == 'read_file' and row['state'] == 'waiting_workspace'
                       for row in manager._active_tools.values()):
                    break
                await asyncio.sleep(.01)
            else:
                pytest.fail('read_file never reached the workspace guard')
            assert not read.done()
            release.set()
            assert await asyncio.wait_for(read, 2) == 'safe content'
        finally:
            release.set()
            if holder is not None:
                await asyncio.to_thread(holder.join, 2)
            await manager.shutdown()

    asyncio.run(run())
