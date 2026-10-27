"""Evidence indexing must preserve the plan revision used for status changes."""

import asyncio
import json

import pytest

from agent_core import config
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools import TOOL_REGISTRY, all_schemas


def _call(name, arguments, identifier):
    return {'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}


def _task():
    return {'key': 'inspect', 'title': 'Inspect input',
            'acceptance': 'Input inspected', 'depends_on': []}


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    from agent_core.tools.task_plans import register_task_plans

    previous_tools = dict(TOOL_REGISTRY)
    previous_schemas = list(TOOL_SCHEMAS)
    register_task_plans()
    TOOL_SCHEMAS[:] = all_schemas()
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    # 本组仅核验版本/证据；保留有界模式，允许受控回调在步骤尚未完成时结束。
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 24)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(previous_tools)
    TOOL_SCHEMAS[:] = previous_schemas


def _evidence_audit(store, owner):
    path = store.directory(owner.id) / 'audit.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)['event'] == 'plan_evidence_recorded']


def test_parallel_plan_get_and_read_preserve_revision_for_update(store):
    async def run():
        async def model(history, **kwargs):
            results = [message for message in history if message['role'] == 'tool']
            if not results:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    _call('plan_get', {}, 'get-plan'),
                    _call('read_file', {'path': 'input.txt'}, 'read-input'),
                ]}
            if len(results) == 2:
                page = json.loads(results[0]['content'])
                assert 'actual input' in results[1]['content']
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    _call('plan_update', {'plan_id': page['plan_id'], 'task_key': 'inspect',
                        'status': 'in_progress', 'expected_revision': page['revision']}, 'start-step'),
                ]}
            return {'role': 'assistant', 'content': 'finished'}

        manager = SessionManager(store, model=model)
        try:
            owner = manager.create()
            created = await manager.task_plans.create(owner, 'inspect input', [_task()], expected_revision=0)
            (manager.workspace(owner) / 'input.txt').write_text('actual input')
            manager.submit(owner, 'inspect input')
            await asyncio.wait_for(owner.task, 10)

            results = {message['tool_call_id']: message['content']
                       for message in owner.record['messages'] if message['role'] == 'tool'}
            assert owner.record['status'] == 'idle'
            assert json.loads(results['get-plan'])['revision'] == created['revision']
            assert json.loads(results['start-step'])['revision'] == created['revision'] + 1
            current = await manager.task_plans.get(owner)
            assert current['revision'] == created['revision'] + 1
            assert [item['tool_call_id'] for item in current['available_evidence']] == ['read-input']
            assert current['available_evidence'][0]['source'] == 'executed'
            assert [(entry['phase'], entry['revision']) for entry in _evidence_audit(store, owner)] == [
                ('requested', created['revision']), ('committed', created['revision'])]
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_status_update_rejects_stale_revision_after_evidence_is_recorded(store):
    async def run():
        async def model(history, **kwargs):
            if any(message['role'] == 'tool' for message in history):
                return {'role': 'assistant', 'content': 'finished'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                _call('read_file', {'path': 'input.txt'}, 'read-input')]}

        manager = SessionManager(store, model=model)
        try:
            owner = manager.create()
            created = await manager.task_plans.create(owner, 'inspect input', [_task()], expected_revision=0)
            before_read_signature = (await manager.task_plans.get(owner))['execution_signature']
            (manager.workspace(owner) / 'input.txt').write_text('actual input')
            manager.submit(owner, 'inspect input')
            await asyncio.wait_for(owner.task, 10)

            after_read = await manager.task_plans.get(owner)
            assert after_read['revision'] == created['revision']
            assert after_read['execution_signature'] != before_read_signature
            assert len(after_read['available_evidence']) == 1
            assert after_read['available_evidence'][0]['tool_call_id'] == 'read-input'
            updated = await manager.task_plans.update(owner, created['plan_id'], 'inspect',
                'in_progress', expected_revision=created['revision'])
            assert updated['revision'] == created['revision'] + 1
            with pytest.raises(ValueError, match='计划版本冲突'):
                await manager.task_plans.update(owner, created['plan_id'], 'inspect',
                    'blocked', block_reason='stale update', expected_revision=created['revision'])
            current = await manager.task_plans.get(owner)
            assert current['revision'] == updated['revision']
            assert current['tasks'][0]['status'] == 'in_progress'
            assert current['available_evidence'] == after_read['available_evidence']
            assert [(entry['phase'], entry['revision']) for entry in _evidence_audit(store, owner)] == [
                ('requested', created['revision']), ('committed', created['revision'])]
        finally:
            await manager.shutdown()
    asyncio.run(run())
