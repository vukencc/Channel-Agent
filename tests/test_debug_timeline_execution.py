"""Timeline events follow real turns, tool calls, delegation and communication."""
import asyncio
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def timeline_store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 2)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _events(manager, session_id=None, kind=None):
    rows = manager.debug.snapshot()['timeline']
    return [row for row in rows if (session_id is None or row.get('session_id') == session_id)
            and (kind is None or row['kind'] == kind)]


def test_real_turn_groups_parallel_read_tools_and_final_with_causal_ids(timeline_store):
    calls = []
    async def model(history, **kwargs):
        calls.append(history)
        if history[-1]['role'] == 'tool':
            kwargs['emit']('metrics', {'input_tokens': 21, 'output_tokens': 4,
                                       'tokens_source': 'provider'})
            return {'role': 'assistant', 'content': 'private final answer'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'read-one', 'function': {'name': 'read_file',
             'arguments': json.dumps({'path': 'one.txt'})}},
            {'id': 'read-two', 'function': {'name': 'read_file',
             'arguments': json.dumps({'path': 'two.txt'})}},
        ]}

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            session = manager.create()
            workspace = manager.workspace(session)
            (workspace / 'one.txt').write_text('private first body')
            (workspace / 'two.txt').write_text('private second body')
            manager.submit(session, 'private prompt')
            await asyncio.wait_for(session.task, 5)
            assert len(calls) == 2
            events = _events(manager, session.id)
            turn = next(row for row in events if row['kind'] == 'turn.started')
            batch = next(row for row in events if row['kind'] == 'tools.batch')
            tools = [row for row in events if row['kind'] == 'tool.completed']
            assert {row['tool_call_id'] for row in tools} == {'read-one', 'read-two'}
            assert {row['batch_id'] for row in tools} == {batch['batch_id']}
            assert {row['turn_id'] for row in tools} == {turn['turn_id']}
            assert {row['state'] for row in tools} == {'completed'}
            assert all(row.get('tool_name') == 'read_file' for row in tools)
            final = next(row for row in events if row['kind'] == 'model.final')
            assert final['turn_id'] == turn['turn_id']
            assert isinstance(final['message_index'], int)
            sample = next(row for row in events if row['kind'] == 'context.sample')
            assert isinstance(sample['metrics']['context_utilization'], (int, float))
            usage = next(row for row in events if row['kind'] == 'model.completed'
                         and row.get('metrics', {}).get('tokens_source') == 'provider')
            assert usage['metrics']['input_tokens'] == 21
            assert usage['metrics']['output_tokens'] == 4
            serialized = json.dumps(manager.debug.snapshot())
            assert all(secret not in serialized for secret in
                       ('private prompt', 'private first body', 'private second body', 'private final answer'))
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_delegated_child_and_direct_parent_message_are_linked(timeline_store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'child done'}

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            parent = manager.create()
            manager.set_tool_names(parent, ['read_file'])
            await manager.flush()
            task_id = await manager.agent_tasks.start(parent, 'private child task', [], 1)
            await asyncio.wait_for(manager.agent_tasks.tasks[task_id], 5)
            child_id = manager.agent_tasks.records[task_id]['child_id']
            created = next(iter(_events(manager, child_id, 'agent.created')))
            assert created['task_id'] == task_id
            assert created['parent_session_id'] == parent.id
            assert any(row.get('task_id') == task_id for row in _events(manager, child_id, 'agent.started'))
            assert any(row.get('task_id') == task_id for row in _events(manager, child_id, 'agent.completed'))
            message = manager.communication.send(parent.id, child_id, 'private direct message')
            received = manager.communication.read(child_id)
            assert received['messages'][0]['seq'] == message['seq']
            sent = next(row for row in _events(manager, parent.id, 'message.sent')
                        if row.get('message_seq') == message['seq'])
            read = next(row for row in _events(manager, child_id, 'message.read')
                        if row.get('message_seq') == message['seq'])
            assert sent['related_session_id'] == child_id
            assert read['related_session_id'] == parent.id
            assert 'private direct message' not in json.dumps(manager.debug.snapshot())
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_cancelled_turn_records_stop_without_model_content(timeline_store):
    entered = asyncio.Event()
    async def model(history, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, 'private cancellation prompt')
            await asyncio.wait_for(entered.wait(), 5)
            manager.cancel(session)
            await asyncio.wait_for(asyncio.gather(session.task, return_exceptions=True), 5)
            stops = _events(manager, session.id, 'turn.stopped')
            assert any(row.get('state') == 'cancelled' for row in stops)
            assert 'private cancellation prompt' not in json.dumps(manager.debug.snapshot())
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_failed_turn_records_terminal_error_without_exception_text(timeline_store):
    async def model(history, **kwargs):
        raise RuntimeError('private provider failure text')

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, 'private failing prompt')
            await asyncio.wait_for(session.task, 5)
            assert session.record['status'] == 'error'
            stops = _events(manager, session.id, 'turn.stopped')
            assert len(stops) == 1
            assert stops[0]['state'] == 'error'
            assert 'private provider failure text' not in json.dumps(manager.debug.snapshot())
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_model_timeout_retry_keeps_one_turn_and_one_final(timeline_store, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    attempts = 0
    async def model(history, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError('private timeout details')
        return {'role': 'assistant', 'content': 'recovered final answer'}

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, 'private retry prompt')
            await asyncio.wait_for(session.task, 5)
            assert attempts == 2
            events = _events(manager, session.id)
            assert len([row for row in events if row['kind'] == 'turn.started']) == 1
            assert len([row for row in events if row['kind'] == 'model.final']) == 1
            assert any(row['kind'] == 'phase.changed' and row.get('phase') == 'recovery'
                       for row in events)
            assert 'private timeout details' not in json.dumps(manager.debug.snapshot())
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_two_concurrent_sessions_keep_distinct_turn_ids(timeline_store):
    entered = set()
    release = asyncio.Event()
    async def model(history, **kwargs):
        entered.add(history[-1]['content'])
        await release.wait()
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            first, second = manager.create(), manager.create()
            manager.submit(first, 'first private prompt')
            manager.submit(second, 'second private prompt')
            for _ in range(200):
                if len(entered) == 2:
                    break
                await asyncio.sleep(.01)
            assert len(entered) == 2
            starts = {session.id: _events(manager, session.id, 'turn.started')[0]
                      for session in (first, second)}
            assert starts[first.id]['turn_id'] != starts[second.id]['turn_id']
            release.set()
            await asyncio.wait_for(asyncio.gather(first.task, second.task), 5)
            for session in (first, second):
                own = _events(manager, session.id)
                assert all(row.get('turn_id') in {None, starts[session.id]['turn_id']}
                           for row in own)
                assert any(row['kind'] == 'model.final' for row in own)
            assert 'first private prompt' not in json.dumps(manager.debug.snapshot())
        finally:
            release.set()
            await manager.shutdown()
    asyncio.run(run())


def test_debug_disabled_keeps_model_call_count_and_no_timeline(timeline_store, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', False)
    calls = 0
    async def model(history, **kwargs):
        nonlocal calls
        calls += 1
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(timeline_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, 'ordinary request')
            await asyncio.wait_for(session.task, 5)
            assert calls == 1
            assert manager.debug.snapshot()['timeline'] == []
        finally:
            await manager.shutdown()
    asyncio.run(run())
