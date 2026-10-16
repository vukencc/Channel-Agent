"""The plan evidence index must remain bounded after real tool results."""

import asyncio
import hashlib
import json
import uuid

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.history_context import _groups
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def _call(identifier):
    return {'id': identifier, 'type': 'function',
            'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'input.txt'})}}


def _task():
    return {'key': 'inspect', 'title': 'Inspect input',
            'acceptance': 'Input inspected', 'depends_on': []}


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'TASK_PLAN_MAX_BYTES', 4096)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()


async def _paired_evidence(manager, owner, identifier, result):
    call = _call(identifier)
    event_id = uuid.uuid4().hex
    owner.record['messages'].extend([
        {'role': 'assistant', 'content': '', 'tool_calls': [call]},
        {'role': 'tool', 'tool_call_id': identifier, 'content': result,
         '_event_id': event_id, '_provenance': 'executed'},
    ])
    await manager.save(owner)
    return await manager.task_plans.record_evidence(
        owner, call, result, 'executed', event_id=event_id)


def test_multibyte_evidence_stays_within_byte_limit_and_keeps_latest_pair(store):
    async def run():
        manager = SessionManager(store, model=None)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            latest = None
            for index in range(24):
                result = f'第{index}次读取：' + '中文内容' * 110
                latest = await _paired_evidence(manager, owner, f'read-{index}', result)
                path = store.directory(owner.id) / 'task_plan.json'
                assert path.stat().st_size <= config.TASK_PLAN_MAX_BYTES
                assert latest['sha256'] == hashlib.sha256(result.encode()).hexdigest()
                assert latest['id'] == owner.record['messages'][-1]['_event_id']
            current = await manager.task_plans.get(owner)
            assert current['revision'] == plan['revision']
            assert current['available_evidence'][-1]['id'] == latest['id']
            assert current['available_evidence'][-1]['source'] == 'executed'
            assert len(current['available_evidence']) < 24
            assert sum(message['role'] == 'tool' for message in owner.record['messages']) == 24
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_capacity_compaction_preserves_referenced_evidence_and_hash(store):
    async def run():
        manager = SessionManager(store, model=None)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            first_result = '原始验收结果：' + '中文' * 140
            first = await _paired_evidence(manager, owner, 'read-first', first_result)
            plan = await manager.task_plans.update(owner, plan['plan_id'], 'inspect',
                'in_progress', evidence_refs=[first['id']], expected_revision=plan['revision'])
            for index in range(18):
                await _paired_evidence(manager, owner, f'read-next-{index}', '后续内容' * 110)
            current = await manager.task_plans.get(owner)
            retained = next(item for item in current['available_evidence'] if item['id'] == first['id'])
            assert retained['sha256'] == hashlib.sha256(first_result.encode()).hexdigest()
            assert retained['source'] == 'executed'
            assert current['tasks'][0]['evidence_refs'] == [first['id']]
            assert (store.directory(owner.id) / 'task_plan.json').stat().st_size <= config.TASK_PLAN_MAX_BYTES
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('recovery', ['checkpoint', 'restart'])
def test_new_evidence_summary_shrinks_when_all_older_entries_are_referenced(store, monkeypatch, recovery):
    async def run():
        manager = SessionManager(store, model=None)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            first = await _paired_evidence(manager, owner, 'read-first', '已核验')
            plan = await manager.task_plans.update(owner, plan['plan_id'], 'inspect',
                'in_progress', evidence_refs=[first['id']], expected_revision=plan['revision'])
            plan_path = store.directory(owner.id) / 'task_plan.json'
            monkeypatch.setattr(config, 'TASK_PLAN_MAX_BYTES', plan_path.stat().st_size + 500)
            result = '最新中文结果' * 100
            newest = await _paired_evidence(manager, owner, 'read-newest', result)
            current = await manager.task_plans.get(owner)
            assert [item['id'] for item in current['available_evidence']] == [first['id'], newest['id']]
            assert newest['summary'] == result[:len(newest['summary'])]
            assert len(newest['summary']) < 400
            assert newest['sha256'] == hashlib.sha256(result.encode()).hexdigest()
            assert current['revision'] == plan['revision']
            assert plan_path.stat().st_size <= config.TASK_PLAN_MAX_BYTES
            events = [json.loads(line) for line in
                      (store.directory(owner.id) / 'audit.jsonl').read_text().splitlines()]
            evidence_events = [entry for entry in events if entry['event'] == 'plan_evidence_recorded']
            assert [(entry['phase'], entry['revision']) for entry in evidence_events[-2:]] == [
                ('requested', plan['revision']), ('committed', plan['revision'])]
            if recovery == 'checkpoint':
                owner.record['status'] = 'checkpoint'
                await manager.task_plans.finish_run(owner)
                recovered = await manager.task_plans.get(owner)
            else:
                manager.task_plans.cache.pop(owner.id)
                recovered = await manager.task_plans.get(owner)
            assert recovered['tasks'][0]['status'] == 'interrupted'
            assert recovered['tasks'][0]['evidence_refs'] == [first['id']]
            assert next(item for item in recovered['available_evidence']
                        if item['id'] == newest['id'])['sha256'] == newest['sha256']
            assert plan_path.stat().st_size <= config.TASK_PLAN_MAX_BYTES
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_full_referenced_index_checkpoints_after_tool_result_is_saved(store, monkeypatch):
    async def run():
        model_calls = 0

        async def model(history, **kwargs):
            nonlocal model_calls
            model_calls += 1
            if any(message.get('tool_call_id') == 'capacity-read' for message in history):
                return {'role': 'assistant', 'content': 'done'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [_call('capacity-read')]}

        manager = SessionManager(store, model=model)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            first = await _paired_evidence(manager, owner, 'read-first', '已验证结果')
            plan = await manager.task_plans.update(owner, plan['plan_id'], 'inspect',
                'in_progress', evidence_refs=[first['id']], expected_revision=plan['revision'])
            plan_path = store.directory(owner.id) / 'task_plan.json'
            monkeypatch.setattr(config, 'TASK_PLAN_MAX_BYTES', plan_path.stat().st_size + 80)
            (manager.workspace(owner) / 'input.txt').write_text('new result')
            manager.submit(owner, 'read input')
            await asyncio.wait_for(owner.task, 10)
            assert model_calls == 1
            assert owner.record['status'] == 'checkpoint'
            assert owner.record['last_run']['stop_reason'] == 'plan_evidence_capacity'
            assert any(message.get('tool_call_id') == 'capacity-read' and
                       'new result' in message['content'] for message in owner.record['messages'])
            assert (await manager.task_plans.get(owner))['tasks'][0]['evidence_refs'] == [first['id']]
            assert plan_path.stat().st_size <= config.TASK_PLAN_MAX_BYTES
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_capacity_checkpoint_pairs_pending_write_without_executing_it(store, monkeypatch):
    async def run():
        model_calls = 0
        executed = []
        pending_write = {'id': 'pending-append', 'type': 'function', 'function': {
            'name': 'append_file', 'arguments': json.dumps({
                'path': 'input.txt', 'content': 'SHOULD NOT BE WRITTEN', 'expected_chars': 10})}}

        async def model(history, **kwargs):
            nonlocal model_calls
            model_calls += 1
            return {'role': 'assistant', 'content': '',
                    'tool_calls': [_call('capacity-read'), pending_write]}

        manager = SessionManager(store, model=model)
        original_tool = manager._tool

        async def tracked_tool(session, call, **kwargs):
            executed.append(call['function']['name'])
            return await original_tool(session, call, **kwargs)

        monkeypatch.setattr(manager, '_tool', tracked_tool)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            first = await _paired_evidence(manager, owner, 'read-first', '已验证结果')
            await manager.task_plans.update(owner, plan['plan_id'], 'inspect',
                'in_progress', evidence_refs=[first['id']], expected_revision=plan['revision'])
            plan_path = store.directory(owner.id) / 'task_plan.json'
            monkeypatch.setattr(config, 'TASK_PLAN_MAX_BYTES', plan_path.stat().st_size + 80)
            target = manager.workspace(owner) / 'input.txt'
            target.write_text('new result')
            manager.submit(owner, 'read input then append')
            await asyncio.wait_for(owner.task, 10)
            assert model_calls == 1
            assert executed == ['read_file']
            assert target.read_text() == 'new result'
            assert owner.record['status'] == 'checkpoint'
            assert owner.record['last_run']['stop_reason'] == 'plan_evidence_capacity'
            results = {message['tool_call_id']: message for message in owner.record['messages']
                       if message['role'] == 'tool'}
            assert 'new result' in results['capacity-read']['content']
            assert results['pending-append']['_provenance'] == 'not_executed'
            assert '尚未执行' in results['pending-append']['content']
            _groups(owner.record['messages'])
            assert (await manager.task_plans.get(owner))['tasks'][0]['evidence_refs'] == [first['id']]
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('recovery', ['checkpoint', 'restart'])
def test_legacy_full_index_can_save_interruption_without_losing_reference(store, monkeypatch, recovery):
    async def run():
        manager = SessionManager(store, model=None)
        try:
            owner = manager.create()
            plan = await manager.task_plans.create(owner, 'inspect', [_task()], expected_revision=0)
            result = '旧的中文证据' * 100
            evidence = await _paired_evidence(manager, owner, 'read-first', result)
            plan = await manager.task_plans.update(owner, plan['plan_id'], 'inspect',
                'in_progress', evidence_refs=[evidence['id']], expected_revision=plan['revision'])
            plan_path = store.directory(owner.id) / 'task_plan.json'
            monkeypatch.setattr(config, 'TASK_PLAN_MAX_BYTES', plan_path.stat().st_size)
            if recovery == 'checkpoint':
                owner.record['status'] = 'checkpoint'
                await manager.task_plans.finish_run(owner)
            else:
                manager.task_plans.cache.pop(owner.id)
            current = await manager.task_plans.get(owner)
            assert current['revision'] == plan['revision'] + 1
            assert current['tasks'][0]['status'] == 'interrupted'
            assert current['tasks'][0]['evidence_refs'] == [evidence['id']]
            retained = next(item for item in current['available_evidence'] if item['id'] == evidence['id'])
            assert retained['sha256'] == hashlib.sha256(result.encode()).hexdigest()
            assert len(retained['summary']) < len(evidence['summary'])
            assert any(message.get('tool_call_id') == 'read-first' and message['content'] == result
                       for message in owner.record['messages'])
            assert plan_path.stat().st_size <= config.TASK_PLAN_MAX_BYTES
        finally:
            await manager.shutdown()
    asyncio.run(run())
