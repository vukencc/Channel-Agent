"""Concurrency limits and workspace write serialization on real agent turns."""

import asyncio
import json
import threading

import pytest
from pydantic import BaseModel

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.base import Tool


class EmptyArgs(BaseModel):
    pass


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'TOOL_CONCURRENCY', 4)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()


def _call(name, identifier):
    return {'id': identifier, 'function': {'name': name, 'arguments': '{}'}}


@pytest.mark.parametrize('limit,expected_peak', [(2, 2), (1, 1)])
def test_model_slots_allow_overlap_and_queue_at_limit(store, monkeypatch, limit, expected_peak):
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', limit)

    async def run():
        entered = []
        first_entered = asyncio.Event()
        both_entered = asyncio.Event()
        second_call_started = asyncio.Event()
        release = asyncio.Event()

        async def model(history, **kwargs):
            entered.append(kwargs['session_id'])
            first_entered.set()
            if len(entered) == 2:
                both_entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model)
        original_call_model = manager._call_model
        calls_started = 0

        async def observed_call_model(history, **kwargs):
            nonlocal calls_started
            calls_started += 1
            if calls_started == 2:
                second_call_started.set()
            return await original_call_model(history, **kwargs)

        monkeypatch.setattr(manager, '_call_model', observed_call_model)
        one, two = manager.create('one'), manager.create('two')
        manager.submit(one, 'work one')
        manager.submit(two, 'work two')
        try:
            if limit == 2:
                await asyncio.wait_for(both_entered.wait(), 3)
            else:
                await asyncio.wait_for(first_entered.wait(), 3)
                await asyncio.wait_for(second_call_started.wait(), 3)
                assert len(entered) == 1
            assert manager.concurrency_status()['active_model_calls'] == expected_peak
            release.set()
            await asyncio.wait_for(asyncio.gather(one.task, two.task), 3)
            assert len(entered) == 2
            assert manager.concurrency_status()['active_model_calls'] == 0
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_read_tools_overlap_in_one_batch_and_results_keep_call_order(store, monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        first_entered = asyncio.Event()
        second_entered = asyncio.Event()
        release = asyncio.Event()
        arrival = []
        lock = threading.Lock()

        def read_probe():
            with lock:
                index = len(arrival)
                arrival.append(index)
            loop.call_soon_threadsafe((first_entered if index == 0 else second_entered).set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=3)
            return f'result-{index}'

        monkeypatch.setitem(TOOL_REGISTRY, 'read_probe', Tool(
            'read_probe', EmptyArgs, read_probe, concurrency='read',
        ))
        calls = 0

        async def model(history, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    _call('read_probe', 'first'), _call('read_probe', 'second'),
                ]}
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, 'read both')
        try:
            await asyncio.wait_for(asyncio.gather(first_entered.wait(), second_entered.wait()), 3)
            assert len(manager.concurrency_status()['active_tools']) == 2
        finally:
            release.set()
        await asyncio.wait_for(session.task, 3)
        messages = session.record['messages']
        assistant_index = next(i for i, row in enumerate(messages) if len(row.get('tool_calls', [])) == 2)
        results = messages[assistant_index + 1:assistant_index + 3]
        assert [row['tool_call_id'] for row in results] == ['first', 'second']
        assert sorted(row['content'] for row in results) == ['result-0', 'result-1']
        assert manager.concurrency_status()['active_tools'] == []
        await manager.shutdown()

    asyncio.run(run())


def test_timed_out_read_tool_keeps_runtime_count_until_worker_exits(store, monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = asyncio.Event()

        def read_probe():
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=3)
            return 'late result'

        monkeypatch.setitem(TOOL_REGISTRY, 'timeout_probe', Tool(
            'timeout_probe', EmptyArgs, read_probe, timeout_s=.05, concurrency='read',
        ))
        manager = SessionManager(store)
        session = manager.create()
        pending = asyncio.create_task(manager._tool(session, _call('timeout_probe', 'slow')))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert len(manager.concurrency_status()['active_tools']) == 1
            assert '超时' in await asyncio.wait_for(pending, 3)
            assert len(manager.concurrency_status()['active_tools']) == 1
        finally:
            release.set()
        async def cleared():
            while manager.concurrency_status()['active_tools']:
                await asyncio.sleep(0)
        await asyncio.wait_for(cleared(), 3)
        await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('command_jobs_enabled', [False, True])
def test_serial_tools_lock_per_shared_root_without_blocking_other_roots(store, monkeypatch, command_jobs_enabled):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', command_jobs_enabled)

    async def run():
        loop = asyncio.get_running_loop()
        entered = asyncio.Queue()
        release = asyncio.Event()

        def serial_probe():
            from ai_agent_startup.tools.sandbox import sandbox_root
            root = sandbox_root()
            loop.call_soon_threadsafe(entered.put_nowait, root)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=3)
            return str(root)

        monkeypatch.setitem(TOOL_REGISTRY, 'serial_probe', Tool(
            'serial_probe', EmptyArgs, serial_probe, concurrency='serial',
        ))
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create('parent')
        unrelated = manager.create('unrelated')
        manager.set_workspace_mode(parent, 'shared')
        created = json.loads(await manager._tool(parent, {'id': 'create', 'function': {
            'name': 'create_session', 'arguments': json.dumps({
                'task': 'wait for serial probe', 'tool_names': ['serial_probe'],
                'max_rounds': 1,
            }),
        }}))
        peer = manager.sessions[created['session_id']]
        await manager.agent_tasks.tasks[created['task_id']]
        assert manager.workspace(parent) == manager.workspace(peer)
        first = asyncio.create_task(manager._tool(parent, _call('serial_probe', 'first')))
        try:
            assert await asyncio.wait_for(entered.get(), 3) == manager.workspace(parent)
            second = asyncio.create_task(manager._tool(peer, _call('serial_probe', 'second')))
            third = asyncio.create_task(manager._tool(unrelated, _call('serial_probe', 'third')))
            assert await asyncio.wait_for(entered.get(), 3) == manager.workspace(unrelated)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.get(), .1)
            assert len(manager.concurrency_status()['active_tools']) >= 2
        finally:
            release.set()
        assert await asyncio.wait_for(entered.get(), 3) == manager.workspace(parent)
        await asyncio.wait_for(asyncio.gather(first, second, third), 3)
        assert manager.concurrency_status()['active_tools'] == []
        await manager.shutdown()

    asyncio.run(run())
