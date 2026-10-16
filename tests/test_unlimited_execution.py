"""Unlimited requests retain cancellation, progress, and delegation boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.budgets import BudgetExceeded
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.task_execution import TaskExecutionPolicy
from ai_agent_startup.core.tool_settings import bounded_schemas
from ai_agent_startup.core.llm import TOOL_SCHEMAS
from ai_agent_startup.tools.agent_tasks import DelegateArgs
from ai_agent_startup.tools.session_tools import CreateSessionArgs


@pytest.fixture
def unlimited_store(tmp_path, monkeypatch, sandbox_env):
    for name in ('ENABLE_TASK_PLANS', 'ENABLE_STRUCTURED_CONTEXT', 'ENABLE_AGENT_TASKS',
                 'ENABLE_COMMAND_JOBS', 'MEMORY_AUTO_EXTRACT', 'AUTO_CONTINUE_TASKS'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _call(number, name='read_file'):
    return {'id': f'tool-{number}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps({'path': 'input.txt'})}}


def test_unlimited_request_runs_past_old_ceiling_until_model_completion(unlimited_store, monkeypatch):
    # 超过旧版 24 * 4 = 96 轮上限；使用本次显式容量，不混入上下文溢出因素。
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 100000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    async def run():
        calls = []

        async def model(history, **kwargs):
            calls.append(history)
            return {'role': 'assistant', 'content': 'done' if len(calls) == 121 else '',
                    **({} if len(calls) == 121 else {'tool_calls': [_call(len(calls))]})}

        manager = SessionManager(unlimited_store, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            manager.submit(session, 'Read until finished')
            await asyncio.wait_for(session.task, 20)
            assert len(calls) == 121
            assert session.record['status'] == 'idle'
            assert len(session.record['last_run']['tools']) == 120
            assert session.record['last_run']['rounds_limit'] is None
            assert session.record['last_run']['budget']['rounds_remaining'] is None
            assert len([m for m in session.record['messages'] if m['role'] == 'user']) == 1
            assert '剩余模型交互' not in ''.join(m.get('content', '') for m in calls[-1])
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('stop', ['cancel', 'cost', 'no_progress', 'empty'])
def test_unlimited_request_keeps_stop_conditions(unlimited_store, stop):
    async def run():
        calls = 0

        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            if stop == 'cancel':
                manager.cancel(session)
            if stop == 'cost':
                raise BudgetExceeded('explicit cost budget')
            if stop == 'empty':
                return {'role': 'assistant', 'content': ''}
            return {'role': 'assistant', 'content': '',
                    'tool_calls': [_call(calls, 'missing_tool')]}

        manager = SessionManager(unlimited_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, 'Do the work')
            await asyncio.wait_for(session.task, 10)
            if stop == 'cancel':
                assert calls == 1
                assert session.record['status'] == 'cancelled'
            else:
                assert session.record['status'] == 'checkpoint'
                assert session.record['last_run']['stop_reason'] == (
                    'cost_or_rate_budget' if stop == 'cost' else 'no_execution_progress')
                assert 1 <= calls <= 16
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_explicit_positive_limit_remains_a_hard_cap(unlimited_store, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)

    async def run():
        calls = 0

        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {'role': 'assistant', 'content': '', 'tool_calls': [_call(calls)]}

        manager = SessionManager(unlimited_store, model=model)
        try:
            session = manager.create()
            session.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': 3}
            (manager.workspace(session) / 'input.txt').write_text('real input')
            manager.submit(session, 'Do the work')
            await asyncio.wait_for(session.task, 10)
            assert calls == 3
            assert session.record['last_run']['stop_reason'] == 'tool_budget'
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_unlimited_policy_and_delegation_schema(monkeypatch):
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 0)
    policy = TaskExecutionPolicy.for_session({}, 0)
    assert policy.maximum_rounds is None
    assert '剩余模型交互' not in policy.budget_guide(1000)
    assert CreateSessionArgs().max_rounds == 0
    assert DelegateArgs(task='Inspect').max_rounds == 0
    schema = next(row for row in bounded_schemas(TOOL_SCHEMAS, ['create_session']))
    rounds = schema['function']['parameters']['properties']['max_rounds']
    assert rounds['default'] == rounds['minimum'] == 0
    assert rounds['maximum'] == 0
    assert rounds['enum'] == [0]


@pytest.mark.parametrize('parent_limit,child_limit,expected', [
    (0, 0, 0), (0, 80, 80), (3, 0, 3), (3, 2, 2),
])
def test_child_round_default_inherits_parent(unlimited_store, monkeypatch,
                                           parent_limit, child_limit, expected):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True)

    async def run():
        manager = SessionManager(unlimited_store)
        try:
            parent = manager.create()
            parent.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': parent_limit}
            identifier = await manager.agent_tasks.start(parent, 'Inspect', ['read_file'],
                                                         child_limit, delay_seconds=3600)
            child = manager.sessions[manager.agent_tasks.records[identifier]['child_id']]
            assert child.record['budget_overrides']['MAX_TOOL_ROUNDS'] == expected
            assert child.record['budget_owner_id'] == parent.id
            with pytest.raises(ValueError, match='轮次|上限'):
                await manager.agent_tasks.start(parent, 'Inspect', ['read_file'],
                                                257 if parent_limit == 0 else 4)
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_child_inherits_runtime_endpoint_capacity(unlimited_store, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)

    async def run():
        manager = SessionManager(unlimited_store)
        try:
            parent = manager.create()
            parent.record['model_profile'] = 'runtime-selected'
            endpoint = SimpleNamespace(context_tokens=192000, response_reserve_tokens=4000,
                                       options=lambda: {})
            monkeypatch.setattr(manager.model_registry, 'resolve', lambda *_: endpoint)
            identifier = await manager.agent_tasks.start(parent, 'Inspect', ['read_file'],
                                                         delay_seconds=3600)
            child = manager.sessions[manager.agent_tasks.records[identifier]['child_id']]
            assert child.record['budget_overrides']['MODEL_INPUT_TOKENS'] == 188000
            assert child.record['budget_overrides']['MODEL_INPUT_CHARS'] == 752000
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_prompt_file_uses_resolved_automatic_character_capacity(tmp_path, monkeypatch):
    from ai_agent_startup.core.prompts import default_prompt
    path = tmp_path / 'prompt.txt'
    text = 'Complete the user task. ' * 1000
    path.write_text(text, encoding='utf-8')
    monkeypatch.setattr(config, 'SYSTEM_PROMPT_FILE', path)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 192000)
    assert default_prompt() == text
