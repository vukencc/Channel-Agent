import asyncio
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.budgets import active_budget


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True, raising=False)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    value = SessionStore(tmp_path / 'state', tmp_path / 'work')
    yield value
    value.close()


def test_delegate_shared_workspace_private_scope_cost_owner_and_paired_result(store, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_REQUESTS_PER_MINUTE', 10)
    owners = []
    async def model(history, **kwargs):
        owners.append(active_budget.get()[1])
        return {'role': 'assistant', 'content': '测试子代理响应'}
    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        manager.set_permission_policy(parent, 'readonly')
        manager.set_tool_names(parent, ['read_file'])
        manager.set_limits(parent, {'MAX_TOOL_ROUNDS': 2})
        await manager.flush()
        identifier = await manager.agent_tasks.start(parent, 'task', ['read_file'], 1)
        await manager.agent_tasks.tasks[identifier]
        result = manager.agent_tasks.status(parent.id, identifier)
        child = manager.sessions[result['child_id']]
        assert result['status'] == 'completed'
        assert child.record['permission_policy'] == 'readonly'
        assert child.record['tool_names'] == ['read_file']
        assert child.record['budget_overrides']['MAX_TOOL_ROUNDS'] == 1
        assert owners == [parent.id]
        assert child.id != parent.id and store.directory(child.id) != store.directory(parent.id)
        assert manager.workspace(child) == manager.workspace(parent)
        assert len(parent.record['messages']) == 1  # 不插入未配对结果
        with pytest.raises(PermissionError):
            manager.agent_tasks.status(child.id, identifier)
        with pytest.raises(ValueError):
            await manager.agent_tasks.start(parent, 'bad', ['run_command'], 1)
        with pytest.raises(ValueError):
            await manager.agent_tasks.start(child, 'nested', ['read_file'], 1)
        await manager.shutdown()
    asyncio.run(run())


def test_queue_concurrency_cancellation_and_restart_no_replay(store, monkeypatch):
    monkeypatch.setattr(config, 'AGENT_TASK_CONCURRENCY', 1, raising=False)
    active = 0
    peak = 0
    async def model(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.05)
            return {'role': 'assistant', 'content': 'test'}
        finally:
            active -= 1
    async def run():
        manager = SessionManager(store, model=model)
        parent = manager.create()
        await manager.flush()
        first = await manager.agent_tasks.start(parent, 'one', [], 1)
        second = await manager.agent_tasks.start(parent, 'two', [], 1)
        await asyncio.gather(*list(manager.agent_tasks.tasks.values()))
        assert peak == 1
        delayed = await manager.agent_tasks.start(parent, 'later', [], 1, delay_seconds=60)
        manager.agent_tasks.cancel(parent.id, delayed)
        await asyncio.gather(manager.agent_tasks.tasks[delayed], return_exceptions=True)
        assert manager.agent_tasks.status(parent.id, delayed)['status'] == 'cancelled'
        await manager.shutdown()
        path = manager.agent_tasks.directory / (first + '.json')
        record = json.loads(path.read_text()); record['status'] = 'running'
        path.write_text(json.dumps(record))
        manager = SessionManager(store, model=model)
        assert manager.agent_tasks.status(parent.id, first)['status'] == 'interrupted'
        assert not manager.agent_tasks.tasks
        await manager.shutdown()
    asyncio.run(run())


def test_delegate_tool_requires_confirmation(store, monkeypatch):
    from ai_agent_startup.tools.agent_tasks import register_agent_tasks
    from ai_agent_startup.tools import TOOL_REGISTRY
    previous = dict(TOOL_REGISTRY)
    register_agent_tasks()
    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: False)
        parent = manager.create()
        await manager.flush()
        call = {'id': 'delegate', 'function': {'name': 'delegate', 'arguments': json.dumps({'task': 'do work', 'tool_names': [], 'max_rounds': 1})}}
        result = await manager._tool(parent, call)
        assert '取消' in result
        assert not manager.agent_tasks.records
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        TOOL_REGISTRY.clear(); TOOL_REGISTRY.update(previous)


def test_confirmed_delegate_bridge_starts_child_and_returns_owned_status(store):
    from ai_agent_startup.tools.agent_tasks import register_agent_tasks
    from ai_agent_startup.tools import TOOL_REGISTRY
    previous = dict(TOOL_REGISTRY)
    register_agent_tasks()
    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': '桥接测试模型桩'}
    async def run():
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        await manager.flush()
        identifier = await manager._tool(parent, {'id': 'start', 'function': {'name': 'delegate', 'arguments': '{"task":"read only","tool_names":[],"max_rounds":1}'}})
        assert len(identifier) == 32
        await manager.agent_tasks.tasks[identifier]
        result = await manager._tool(parent, {'id': 'poll', 'function': {'name': 'task_status', 'arguments': json.dumps({'task_id': identifier})}})
        assert json.loads(result)['status'] == 'completed'
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        TOOL_REGISTRY.clear(); TOOL_REGISTRY.update(previous)
