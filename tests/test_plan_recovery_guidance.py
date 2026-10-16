"""Regression tests for guidance when a bound plan execution has finished."""

import asyncio
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.llm import TOOL_SCHEMAS
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY, all_schemas


def _task(key):
    return {'key': key, 'title': key, 'acceptance': f'verify {key}', 'depends_on': []}


def _call(name, arguments):
    return {'id': name, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    from ai_agent_startup.tools.agent_tasks import register_agent_tasks
    from ai_agent_startup.tools.task_plans import register_task_plans

    previous_tools = dict(TOOL_REGISTRY)
    previous_schemas = list(TOOL_SCHEMAS)
    register_agent_tasks()
    register_task_plans()
    TOOL_SCHEMAS[:] = all_schemas()
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 24)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(previous_tools)
    TOOL_SCHEMAS[:] = previous_schemas


def test_drained_waiting_child_is_next_action_before_ready_pending_step(store):
    async def child_model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'child finished'}

    async def run():
        manager = SessionManager(store, model=child_model,
                                 confirmation_handler=lambda *_: True)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(
                owner, 'review delegated work', [_task('delegated'), _task('other')],
                expected_revision=0)
            plan = await manager.task_plans.update(
                owner, plan['plan_id'], 'delegated', 'in_progress',
                expected_revision=plan['revision'])
            child = json.loads(await manager._tool(owner, _call('create_session', {
                'task': 'complete delegated work', 'max_rounds': 1,
            })))
            await asyncio.wait_for(manager.agent_tasks.tasks[child['task_id']], 5)
            plan = await manager.task_plans.bind(
                owner, plan['plan_id'], 'delegated', agent_task_id=child['task_id'],
                expected_revision=plan['revision'])
            assert plan['tasks'][0]['execution_ref']['drained']
            assert plan['tasks'][0]['execution_ref']['runtime_status'] == 'completed'
            assert plan['tasks'][0]['status'] == 'waiting'
            assert 'other' in plan['ready']

            summary = json.loads((await manager.task_plans.summary(owner)).split('\n', 2)[2])
            assert summary['next_action'] == {'tool': 'plan_update', 'arguments': {
                'plan_id': plan['plan_id'], 'expected_revision': plan['revision'],
                'task_key': 'delegated', 'status': 'in_progress'}}
            unchanged = await manager.task_plans.get(owner)
            assert unchanged['revision'] == plan['revision']
            assert unchanged['tasks'][0]['status'] == 'waiting'
            assert unchanged['tasks'][1]['status'] == 'pending'
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_child_prompt_explains_tool_call_draft_is_not_execution(store):
    async def child_model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'child finished'}

    async def run():
        manager = SessionManager(store, model=child_model,
                                 confirmation_handler=lambda *_: True)
        try:
            owner = manager.create()
            child = json.loads(await manager._tool(owner, _call('create_session', {
                'task': 'produce a verified result', 'max_rounds': 1,
            })))
            prompt = manager.sessions[child['session_id']].record['messages'][0]['content']
            assert '工具调用草稿' in prompt
            assert '不代表执行' in prompt
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_waiting_child_still_queued_does_not_preempt_ready_step(store):
    async def child_model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'child finished'}

    async def run():
        manager = SessionManager(store, model=child_model,
                                 confirmation_handler=lambda *_: True)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(
                owner, 'review delegated work', [_task('delegated'), _task('other')],
                expected_revision=0)
            plan = await manager.task_plans.update(
                owner, plan['plan_id'], 'delegated', 'in_progress',
                expected_revision=plan['revision'])
            task_id = await manager.agent_tasks.start(
                owner, 'complete delegated work', tool_names=[], max_rounds=1,
                delay_seconds=60)
            plan = await manager.task_plans.bind(
                owner, plan['plan_id'], 'delegated', agent_task_id=task_id,
                expected_revision=plan['revision'])
            assert not plan['tasks'][0]['execution_ref']['drained']
            summary = json.loads((await manager.task_plans.summary(owner)).split('\n', 2)[2])
            assert summary['next_action']['arguments']['task_key'] == 'other'
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('requested_status', ['completed', 'failed'])
def test_illegal_waiting_transition_explains_review_without_mutating_plan(store, requested_status):
    async def child_model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'child finished'}

    async def run():
        manager = SessionManager(store, model=child_model,
                                 confirmation_handler=lambda *_: True)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(
                owner, 'review delegated work', [_task('delegated')], expected_revision=0)
            plan = await manager.task_plans.update(
                owner, plan['plan_id'], 'delegated', 'in_progress',
                expected_revision=plan['revision'])
            child = json.loads(await manager._tool(owner, _call('create_session', {
                'task': 'complete delegated work', 'max_rounds': 1,
            })))
            await asyncio.wait_for(manager.agent_tasks.tasks[child['task_id']], 5)
            plan = await manager.task_plans.bind(
                owner, plan['plan_id'], 'delegated', agent_task_id=child['task_id'],
                expected_revision=plan['revision'])
            plan_path = store.directory(owner.id) / 'task_plan.json'
            original_bytes = plan_path.read_bytes()

            with pytest.raises(ValueError) as error:
                await manager.task_plans.update(
                    owner, plan['plan_id'], 'delegated', requested_status,
                    result='claimed complete', expected_revision=plan['revision'])
            message = str(error.value)
            for detail in ('非法步骤状态迁移', 'delegated', 'waiting', requested_status,
                           'in_progress', 'blocked', 'cancelled'):
                assert detail in message
            assert '核验' in message or '验收' in message
            assert plan_path.read_bytes() == original_bytes
            unchanged = await manager.task_plans.get(owner)
            assert unchanged['revision'] == plan['revision']
            assert unchanged['tasks'][0]['status'] == 'waiting'
        finally:
            await manager.shutdown()

    asyncio.run(run())
