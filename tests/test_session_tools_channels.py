"""Regression coverage for explicit session creation and parent-child messages."""

import asyncio
import json

import pytest
from pydantic import BaseModel

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.base import Tool


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def _call(name, arguments, identifier=None):
    return {'id': identifier or name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}


def test_open_session_calls_registered_create_tool_once(store, monkeypatch):
    from ai_agent_startup.core.session_service import open_session

    registered = TOOL_REGISTRY['create_session']
    calls = []
    original = registered.run

    def counted(arguments):
        calls.append(json.loads(arguments))
        return original(arguments)

    monkeypatch.setattr(registered, 'run', counted)
    manager = SessionManager(store)
    session = open_session(manager, title='UI 会话', prompt='system prompt')
    assert len(calls) == 1
    assert session.id in manager.sessions
    assert session.record['title'] == 'UI 会话'
    assert session.record['messages'][0]['content'] == 'system prompt'
    assert store.load_all()[0]['id'] == session.id


def test_create_session_requires_task_confirmation_and_parent_scope(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'child finished'}

    async def run():
        denied = SessionManager(store, model=model, confirmation_handler=lambda *_: False)
        parent = denied.create()
        await denied.flush()
        with pytest.raises(ValueError):
            await denied._tool(parent, _call('create_session', {'task': ''}))
        response = await denied._tool(parent, _call('create_session', {'task': 'inspect', 'tool_names': [], 'max_rounds': 1}))
        assert '取消' in response or '拒绝' in response
        assert len(denied.sessions) == 1
        await denied.shutdown()

        confirmations = []
        allowed = SessionManager(store, model=model, confirmation_handler=lambda *_: confirmations.append(True) or True)
        parent = allowed.sessions[parent.id]
        allowed.set_permission_policy(parent, 'readonly')
        allowed.set_tool_names(parent, ['create_session', 'read_file'])
        await allowed.flush()
        response = await allowed._tool(parent, _call('create_session', {
            'task': 'inspect', 'tool_names': ['read_file'], 'max_rounds': 1,
        }))
        assert '拒绝' in response or '取消' in response
        assert confirmations == []
        assert len(allowed.sessions) == 1
        allowed.set_permission_policy(parent, 'standard')
        response = await allowed._tool(parent, _call('create_session', {
            'task': 'inspect', 'tool_names': ['read_file'], 'max_rounds': 1,
        }))
        created = json.loads(response)
        child = allowed.sessions[created['session_id']]
        assert created['task_id'] in allowed.agent_tasks.records
        assert child.record['delegated_from'] == parent.id
        assert child.record['permission_policy'] == 'standard'
        assert child.record['tool_names'] == ['read_file']
        assert child.record['budget_overrides']['MAX_TOOL_ROUNDS'] == 1
        assert store.workspace(child.id) != store.workspace(parent.id)
        await allowed.agent_tasks.tasks[created['task_id']]
        await allowed.shutdown()

    asyncio.run(run())


def test_parent_child_messages_are_bidirectional_persistent_and_private(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        unrelated = manager.create('unrelated')
        await manager.flush()
        created = json.loads(await manager._tool(parent, _call('create_session', {
            'task': 'read notes', 'tool_names': ['send_session_message', 'read_session_messages'],
            'max_rounds': 1,
        })))
        child = manager.sessions[created['session_id']]
        await manager.agent_tasks.tasks[created['task_id']]

        sent_down = json.loads(await manager._tool(parent, _call('send_session_message', {
            'session_id': child.id, 'message': 'parent to child',
        })))
        sent_up = json.loads(await manager._tool(child, _call('send_session_message', {
            'session_id': parent.id, 'message': 'child to parent',
        })))
        assert sent_down['sender_id'] == parent.id
        assert sent_down['recipient_id'] == child.id
        assert sent_up['sender_id'] == child.id
        assert sent_up['recipient_id'] == parent.id
        assert sent_down['seq'] > 0 and sent_up['seq'] > 0

        for caller, recipient, expected in ((parent, parent.id, sent_up), (child, child.id, sent_down)):
            result = json.loads(await manager._tool(caller, _call('read_session_messages', {'after_seq': 0, 'limit': 20})))
            assert result['messages'] == [expected]
            assert result['next_seq'] == expected['seq']

        with pytest.raises(PermissionError):
            manager.communication.send(unrelated.id, child.id, 'unrelated')
        await manager.shutdown()

        restored = SessionManager(store, model=model)
        assert restored.communication.read(parent.id, after_seq=0, limit=20)['messages'] == [sent_up]
        assert restored.communication.read(child.id, after_seq=0, limit=20)['messages'] == [sent_down]
        await restored.shutdown()

    asyncio.run(run())


def test_arriving_message_waits_for_complete_tool_batch(store, monkeypatch):
    class EmptyArgs(BaseModel):
        pass

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_tool():
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return 'tool complete'

        monkeypatch.setitem(TOOL_REGISTRY, 'session_channel_probe', Tool(
            'session_channel_probe', EmptyArgs, slow_tool,
        ))
        histories = []

        async def model(history, **kwargs):
            histories.append([dict(message) for message in history])
            if len(histories) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    _call('session_channel_probe', {}, 'one'),
                    _call('session_channel_probe', {}, 'two'),
                ]}
            return {'role': 'assistant', 'content': 'finished'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        child = manager.create('child')
        child.record['delegated_from'] = parent.id
        await manager.save(child)
        manager.submit(parent, 'run probes')
        await asyncio.wait_for(entered.wait(), 2)
        try:
            manager.communication.send(child.id, parent.id, 'message while busy')
        finally:
            release.set()
        await asyncio.wait_for(parent.task, 3)
        messages = parent.record['messages']
        assistant_index = next(i for i, message in enumerate(messages) if len(message.get('tool_calls', [])) == 2)
        assert [message['tool_call_id'] for message in messages[assistant_index + 1:assistant_index + 3]] == ['one', 'two']
        delivered = [i for i, message in enumerate(messages) if message['role'] == 'user' and 'message while busy' in message['content']]
        assert len(delivered) == 1
        assert delivered[0] > assistant_index + 2
        assert len(histories) == 2
        await manager.shutdown()

    asyncio.run(run())


def test_cancelled_turn_does_not_replay_persisted_message_on_restart(store):
    async def run():
        entered = asyncio.Event()
        model_calls = []

        async def model(history, **kwargs):
            model_calls.append(history)
            entered.set()
            await asyncio.sleep(60)
            return {'role': 'assistant', 'content': 'late'}

        manager = SessionManager(store, model=model)
        parent = manager.create('parent')
        child = manager.create('child')
        child.record['delegated_from'] = parent.id
        await manager.save(child)
        manager.submit(parent, 'work')
        await asyncio.wait_for(entered.wait(), 2)
        sent = manager.communication.send(child.id, parent.id, 'after cancellation')
        manager.cancel(parent)
        await asyncio.wait_for(asyncio.gather(parent.task, return_exceptions=True), 2)
        await manager.shutdown()

        restored = SessionManager(store, model=model)
        await asyncio.sleep(0)
        assert len(model_calls) == 1
        assert not restored.sessions[parent.id].busy
        assert restored.communication.read(parent.id, after_seq=0, limit=20)['messages'] == [sent]
        assert all('after cancellation' not in str(message) for message in restored.sessions[parent.id].record['messages'])
        await restored.shutdown()

    asyncio.run(run())


def test_child_limits_and_timeout_apply_with_global_budget_feature_off(store, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', False)
    monkeypatch.setattr(config, 'TOOL_TIMEOUT', .5)
    model_calls = []

    async def model(history, **kwargs):
        model_calls.append(kwargs['session_id'])
        return {'role': 'assistant', 'content': 'done'}

    class EmptyArgs(BaseModel):
        pass

    def slow_read():
        import time
        time.sleep(.08)
        return 'late'

    monkeypatch.setitem(TOOL_REGISTRY, 'read_file', Tool(
        'read_file', EmptyArgs, slow_read, timeout_s=1, concurrency='read',
    ))

    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        created = json.loads(await manager._tool(parent, _call('create_session', {
            'task': 'bounded task', 'tool_names': ['read_file'], 'max_rounds': 1,
        })))
        child = manager.sessions[created['session_id']]
        child.record['budget_overrides']['TOOL_TIMEOUT'] = .02
        await manager.agent_tasks.tasks[created['task_id']]
        assert child.record['budget_overrides']['MAX_TOOL_ROUNDS'] == 1
        assert model_calls == [child.id]
        result = await manager._tool(child, _call('read_file', {}))
        assert '超时' in result
        await manager.shutdown()

    asyncio.run(run())


def test_child_cannot_recurse_or_expand_parent_tools(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        confirmations = []
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: confirmations.append(True) or True)
        parent = manager.create()
        manager.set_tool_names(parent, ['create_session', 'read_file'])
        await manager.flush()
        with pytest.raises(PermissionError):
            await manager._tool(parent, _call('create_session', {
                'task': 'too broad', 'tool_names': ['run_command'], 'max_rounds': 1,
            }))
        assert confirmations == []
        created = json.loads(await manager._tool(parent, _call('create_session', {
            'task': 'allowed', 'tool_names': ['read_file'], 'max_rounds': 1,
        })))
        child = manager.sessions[created['session_id']]
        await manager.agent_tasks.tasks[created['task_id']]
        assert 'create_session' not in child.record['tool_names']
        child.record['tool_names'].append('create_session')  # Defense also applies inside the tool.
        with pytest.raises(PermissionError):
            await manager._tool(child, _call('create_session', {
                'task': 'nested', 'tool_names': [], 'max_rounds': 1,
            }))
        assert len(confirmations) == 1
        await manager.shutdown()

    asyncio.run(run())


def test_inbox_symlink_and_full_capacity_preserve_existing_data(store, tmp_path, monkeypatch):
    manager = SessionManager(store)
    parent = manager.create()
    child = manager.create()
    child.record['delegated_from'] = parent.id
    first = manager.communication.send(parent.id, child.id, 'keep this')
    inbox = store.directory(child.id) / 'inbox.json'
    original = inbox.read_bytes()
    monkeypatch.setattr(config, 'SESSION_INBOX_MAX_BYTES', len(original))
    with pytest.raises(ValueError):
        manager.communication.send(parent.id, child.id, 'overflow')
    assert inbox.read_bytes() == original
    assert manager.communication.pending_count(child) == 1
    assert first['seq'] == 1

    external = tmp_path / 'outside.json'
    external.write_text('sentinel', encoding='utf-8')
    (store.directory(parent.id) / 'inbox.json').symlink_to(external)
    with pytest.raises(PermissionError):
        manager.communication.send(child.id, parent.id, 'blocked')
    assert external.read_text(encoding='utf-8') == 'sentinel'
