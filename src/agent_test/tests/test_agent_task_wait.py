"""有界子任务等待必须使用真实队列和会话执行器。"""
import asyncio
import json

import pytest
from pydantic import ValidationError

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools import TOOL_REGISTRY


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 24)
    monkeypatch.setattr(config, 'AGENT_TASK_CONCURRENCY', 2)
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 4)
    monkeypatch.setattr(config, 'MODEL_REQUESTS_PER_MINUTE', 0)
    monkeypatch.setattr(config, 'ENABLE_ASSESSMENT', False, raising=False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def call(name, **arguments):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}


async def create(manager, parent, task='inspect', tools=None):
    return json.loads(await manager._tool(parent, call('create_session', task=task,
        tool_names=[] if tools is None else tools, max_rounds=1)))


def test_registered_wait_and_lazy_queue_two_real_children_overlap(store):
    assert 'wait_agent_task' in TOOL_REGISTRY
    assert TOOL_REGISTRY['wait_agent_task'].concurrency == 'read'

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def model(history, **kwargs):
            calls.append(kwargs['session_id'])
            if len(calls) == 2:
                entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'verified child result'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        assert manager.agent_tasks is None
        try:
            first = await create(manager, parent, 'one')
            second = await create(manager, parent, 'two')
            await asyncio.wait_for(entered.wait(), 2)
            assert set(calls) == {first['session_id'], second['session_id']}
            pending = json.loads(await manager._tool(parent, call('wait_agent_task',
                task_id=first['task_id'], timeout_seconds=0)))
            assert pending['status'] == 'running' and not pending['drained']
            release.set()
            for created in (first, second):
                result = json.loads(await manager._tool(parent, call('wait_agent_task',
                    task_id=created['task_id'], timeout_seconds=1)))
                assert result['task_id'] == created['task_id']
                assert result['child_id'] == created['session_id']
                assert result['status'] == 'completed' and result['drained']
                assert result['result'] == 'verified child result'
            assert len(calls) == 2
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_wait_timeout_ownership_and_semaphore_do_not_cancel_or_replay(store, monkeypatch):
    monkeypatch.setattr(config, 'AGENT_TASK_CONCURRENCY', 1)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def model(history, **kwargs):
            calls.append(kwargs['session_id'])
            entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        unrelated = manager.create()
        await manager.flush()
        try:
            first = await create(manager, parent)
            second = await create(manager, parent)
            await asyncio.wait_for(entered.wait(), 2)
            with pytest.raises(PermissionError):
                await manager._tool(unrelated, call('wait_agent_task',
                    task_id=first['task_id'], timeout_seconds=0))
            result = await manager.agent_tasks.wait(parent.id, first['task_id'], .02)
            assert not result['drained'] and result['status'] == 'running'
            assert not manager.agent_tasks.tasks[first['task_id']].done()
            assert len(calls) == 1
            assert manager.agent_tasks.status(parent.id, second['task_id'])['status'] == 'queued'
            release.set()
            await asyncio.wait_for(asyncio.gather(*manager.agent_tasks.tasks.values()), 2)
            assert len(calls) == 2
            result = await manager.agent_tasks.wait(parent.id, first['task_id'], 0)
            assert result['drained'] and result['status'] == 'completed'
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_waiter_cancellation_leaves_no_wait_task_and_preserves_child(store):
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(history, **kwargs):
            entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        try:
            created = await create(manager, parent)
            await asyncio.wait_for(entered.wait(), 2)
            before = set(asyncio.all_tasks())
            waiter = asyncio.create_task(manager.agent_tasks.wait(parent.id, created['task_id'], 30))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not manager.agent_tasks.tasks[created['task_id']].done()
            assert set(asyncio.all_tasks()) <= before
            parent.cancelled.set()
            with pytest.raises(ValueError, match='取消'):
                await manager.agent_tasks.wait(parent.id, created['task_id'], 30)
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_parent_cancellation_interrupts_tool_worker_without_orphan_wait(store):
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(history, **kwargs):
            entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        try:
            created = await create(manager, parent)
            await asyncio.wait_for(entered.wait(), 2)
            waiting = asyncio.create_task(manager._tool(parent, call('wait_agent_task',
                task_id=created['task_id'], timeout_seconds=30)))
            for _ in range(100):
                if any(item['name'] == 'wait_agent_task' and item['state'] == 'running'
                       for item in manager.concurrency_status()['active_tools']):
                    break
                await asyncio.sleep(.001)
            parent.cancelled.set()
            with pytest.raises(ValueError, match='取消'):
                await asyncio.wait_for(waiting, 1)
            await asyncio.sleep(0)
            assert not parent.tool_workers
            assert not manager.agent_tasks.tasks[created['task_id']].done()
            release.set()
            await manager.agent_tasks.tasks[created['task_id']]
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_tool_wait_respects_active_timeout_and_returns_bounded_valid_json(store, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 400)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def model(history, **kwargs):
            entered.set()
            await release.wait()
            return {'role': 'assistant', 'content': '"\\\n中文' * 1000}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        try:
            created = await create(manager, parent)
            await asyncio.wait_for(entered.wait(), 2)
            manager.set_limits(parent, {'TOOL_TIMEOUT': .2})
            # 模拟模型轮次的有效会话预算域。
            from agent_core.core.session_limits import limits_scope
            with limits_scope(parent.record):
                raw = await asyncio.wait_for(manager._tool(parent, call('wait_agent_task',
                    task_id=created['task_id'], timeout_seconds=30)), .5)
            assert len(raw) <= config.TOOL_MAX_OUTPUT
            assert not json.loads(raw)['drained']
            release.set()
            await manager.agent_tasks.tasks[created['task_id']]
            manager.agent_tasks.records[created['task_id']]['error'] = '\\"错误' * 1000
            raw = await manager._tool(parent, call('wait_agent_task',
                task_id=created['task_id'], timeout_seconds=0))
            result = json.loads(raw)
            assert len(raw) <= config.TOOL_MAX_OUTPUT
            assert result['drained'] and result['result_truncated'] and result['error_truncated']
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('arguments', [
    {'task_id': '../task'}, {'task_id': 'a' * 32, 'timeout_seconds': -1},
    {'task_id': 'a' * 32, 'timeout_seconds': 31},
    {'task_id': 'a' * 32, 'timeout_seconds': float('nan')},
    {'task_id': 'a' * 32, 'timeout_seconds': float('inf')},
    {'task_id': 'a' * 32, 'owner': 'b' * 32},
])
def test_wait_arguments_forbid_invalid_id_unbounded_timeout_and_owner(arguments):
    tool = TOOL_REGISTRY['wait_agent_task']
    with pytest.raises(ValidationError):
        tool.args_model.model_validate(arguments)


def test_wait_readonly_creation_confirmation_scope_and_nested_denial(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        confirmations = []
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: confirmations.append(True) or True)
        parent = manager.create()
        manager.set_tool_names(parent, ['create_session', 'wait_agent_task', 'read_file'])
        await manager.flush()
        try:
            with pytest.raises(PermissionError):
                await create(manager, parent, tools=['run_command'])
            created = await create(manager, parent, tools=['create_session', 'wait_agent_task', 'read_file'])
            child = manager.sessions[created['session_id']]
            await manager.agent_tasks.tasks[created['task_id']]
            assert set(child.record['tool_names']) == set(parent.record['tool_names'])
            assert child.record['permission_policy'] == parent.record.get('permission_policy', config.TOOL_PERMISSION_POLICY)
            assert manager.workspace(child) == manager.workspace(parent)
            with pytest.raises(PermissionError):
                await create(manager, child)
            manager.set_permission_policy(parent, 'readonly')
            waited = json.loads(await manager._tool(parent, call('wait_agent_task', task_id=created['task_id'])))
            assert waited['drained']
            denied = await manager._tool(parent, call('create_session', task='denied', tool_names=[], max_rounds=1))
            assert '拒绝' in denied or '取消' in denied
            assert confirmations == [True]
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_finished_runner_with_live_child_worker_is_not_drained(store):
    async def run():
        async def model(*args, **kwargs):
            return {'role': 'assistant', 'content': 'done'}
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        try:
            created = await create(manager, parent)
            await manager.agent_tasks.tasks[created['task_id']]
            child = manager.sessions[created['session_id']]
            marker = asyncio.get_running_loop().create_future()
            child.tool_workers.add(marker)
            result = await manager.agent_tasks.wait(parent.id, created['task_id'], 0)
            assert result['status'] == 'completed'
            assert not result['drained']
            child.tool_workers.remove(marker)
            marker.set_result(None)
            assert (await manager.agent_tasks.wait(parent.id, created['task_id'], 0))['drained']
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_wait_does_not_hold_slot_needed_by_child_read_tool(store, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_CONCURRENCY', 1)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        rounds = 0
        async def model(history, **kwargs):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                entered.set()
                await release.wait()
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    call('read_file', path='input.txt')]}
            assert 'real child input' in history[-1]['content']
            return {'role': 'assistant', 'content': 'read verified'}
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        (manager.workspace(parent) / 'input.txt').write_text('real child input')
        try:
            created = json.loads(await manager._tool(parent, call('create_session', task='read input',
                                tool_names=['read_file'], max_rounds=2)))
            await entered.wait()
            waiting = asyncio.create_task(manager._tool(parent, call('wait_agent_task',
                          task_id=created['task_id'], timeout_seconds=.4)))
            for _ in range(100):
                if any(item['name'] == 'wait_agent_task' and item['state'] == 'running'
                       for item in manager.concurrency_status()['active_tools']):
                    break
                await asyncio.sleep(.001)
            release.set()
            result = json.loads(await waiting)
            assert result['status'] == 'completed' and result['drained']
            assert result['result'] == 'read verified'
            child = manager.sessions[created['session_id']]
            assert child.record['last_run']['tools'][0]['source'] == 'executed'
        finally:
            release.set()
            await manager.shutdown()
    asyncio.run(run())
