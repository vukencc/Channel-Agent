"""Model delegation must preserve an unlimited parent's round policy."""

import asyncio
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.llm import TOOL_SCHEMAS
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.tool_settings import bounded_schemas


@pytest.fixture
def unlimited_store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


@pytest.mark.parametrize('name', ['create_session', 'delegate'])
def test_unlimited_parent_schema_only_offers_inheritance(monkeypatch, name):
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)
    schema = bounded_schemas(TOOL_SCHEMAS, [name])[0]
    rounds = schema['function']['parameters']['properties']['max_rounds']
    assert rounds['default'] == rounds['minimum'] == rounds['maximum'] == 0
    assert rounds['enum'] == [0]


@pytest.mark.parametrize('name', ['create_session', 'delegate'])
def test_unlimited_parent_rejects_model_selected_child_cap(unlimited_store, name):
    async def run():
        manager = SessionManager(unlimited_store, confirmation_handler=lambda *_args: True)
        try:
            parent = manager.create()
            arguments = {'task': 'Inspect input', 'max_rounds': 40}
            call = {'id': 'unexpected-cap', 'type': 'function', 'function': {
                'name': name, 'arguments': json.dumps(arguments)}}
            with pytest.raises(ValueError, match='轮次|上限|继承'):
                await manager._tool(parent, call)
            assert set(manager.sessions) == {parent.id}
            assert manager.agent_tasks is None or not manager.agent_tasks.records
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_finite_parent_schema_still_offers_child_cap(monkeypatch):
    from ai_agent_startup.core.session_limits import limits_scope

    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    with limits_scope({'budget_overrides': {'MAX_TOOL_ROUNDS': 12}}):
        schema = bounded_schemas(TOOL_SCHEMAS, ['create_session'])[0]
    rounds = schema['function']['parameters']['properties']['max_rounds']
    assert rounds['minimum'] == rounds['default'] == 0
    assert rounds['maximum'] == 12
    assert 'enum' not in rounds


def test_default_child_runs_past_forty_rounds(unlimited_store):
    async def run():
        calls = 0

        async def model(_history, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 45:
                return {'role': 'assistant', 'content': 'child finished'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'read-{calls}', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': 'input.txt'})}}]}

        manager = SessionManager(unlimited_store, model=model,
                                 confirmation_handler=lambda *_args: True)
        try:
            parent = manager.create()
            (manager.workspace(parent) / 'input.txt').write_text('verified input')
            call = {'id': 'create-child', 'type': 'function', 'function': {
                'name': 'create_session', 'arguments': json.dumps({
                    'task': 'Read until finished', 'tool_names': ['read_file']})}}
            created = json.loads(await manager._tool(parent, call))
            child = manager.sessions[created['session_id']]
            assert child.record['budget_overrides']['MAX_TOOL_ROUNDS'] == 0
            await asyncio.wait_for(manager.agent_tasks.tasks[created['task_id']], 10)
            assert calls == 45
            assert child.record['status'] == 'idle'
            assert child.record['last_run']['rounds_limit'] is None
            assert manager.agent_tasks.status(parent.id, created['task_id'])['status'] == 'completed'
        finally:
            await manager.shutdown()

    asyncio.run(run())
