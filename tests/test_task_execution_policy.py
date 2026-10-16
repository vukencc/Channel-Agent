"""Regression contracts for bounded, opt-in task stage continuation."""

import asyncio
import json
import threading

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.budgets import BudgetExceeded
from ai_agent_startup.core.llm import ModelConnectionError
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.task_execution import execution_state
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.command_jobs import register_command_jobs


def _call(identifier, path='input.txt'):
    return {'id': identifier, 'type': 'function',
            'function': {'name': 'read_file', 'arguments': json.dumps({'path': path})}}


def _reply(call=None, content=''):
    result = {'role': 'assistant', 'content': content}
    if call is not None:
        result['tool_calls'] = [call]
    return result


def _step():
    return {'key': 'inspect', 'title': 'Inspect the input',
            'acceptance': 'Input was read', 'depends_on': []}


@pytest.fixture
def execution_case(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 2)
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', False, raising=False)
    monkeypatch.setattr(config, 'TASK_MAX_CONTINUATIONS', 1, raising=False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


async def _active_plan(manager, session, *, status='in_progress'):
    plan = await manager.task_plans.create(session, 'Inspect input', [_step()], expected_revision=0)
    if status != 'pending':
        plan = await manager.task_plans.update(session, plan['plan_id'], 'inspect', 'in_progress',
                                               expected_revision=plan['revision'])
    if status == 'blocked':
        plan = await manager.task_plans.update(session, plan['plan_id'], 'inspect', 'blocked',
                                               block_reason='waiting for a person',
                                               expected_revision=plan['revision'])
    return plan


async def _submit(manager, session):
    manager.submit(session, 'Inspect the input and finish the plan.')
    await asyncio.wait_for(session.task, 10)
    await manager.flush()


def test_default_off_saves_checkpoint_at_base_round_limit(execution_case):
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply(_call(f'read-{len(calls)}'))
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'tool_budget'
            assert [item['source'] for item in session.record['last_run']['tools']] == ['executed'] * 2
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_opt_in_continues_same_user_turn_after_executed_tools(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            if len(calls) <= 3:
                return _reply(_call(f'read-{len(calls)}'))
            plan = await manager.task_plans.get(session)
            evidence = next(item for item in plan['available_evidence']
                            if item['tool_call_id'] == 'read-3' and item['source'] == 'executed')
            await manager.task_plans.update(session, plan['plan_id'], 'inspect', 'completed',
                                            result='Input inspected', evidence_refs=[evidence['id']],
                                            expected_revision=plan['revision'])
            return _reply(content='Input inspected and plan completed.')
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 4
            assert session.record['status'] == 'idle'
            assert (await manager.task_plans.get(session))['status'] == 'completed'
            assert [item['source'] for item in session.record['last_run']['tools']] == ['executed'] * 3
            assert len([item for item in session.record['messages'] if item['role'] == 'user']) == 1
            assert [call['id'] for message in session.record['messages']
                    for call in message.get('tool_calls', [])] == [
                        message['tool_call_id'] for message in session.record['messages']
                        if message['role'] == 'tool']
            assert len({item['event_id'] for item in session.record['last_run']['tools']}) == 3
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_continuation_cap_checkpoints_after_four_model_rounds(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply(_call(f'read-{len(calls)}'))
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 4
            assert len(session.record['last_run']['tools']) == 4
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'task_continuation_limit'
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['delegated', 'override'])
def test_child_or_explicit_round_override_keeps_hard_cap(execution_case, monkeypatch, kind):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply(_call(f'read-{len(calls)}'))
        manager = SessionManager(execution_case, model=model)
        try:
            parent = manager.create()
            session = manager.create(parent=parent) if kind == 'delegated' else parent
            if kind == 'override':
                session.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': 2}
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'tool_budget'
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_blocked_plan_does_not_auto_continue(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply(_call(f'read-{len(calls)}'))
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session, status='blocked')
            await _submit(manager, session)
            assert len(calls) == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'plan_waiting'
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_failed_tool_attempts_do_not_earn_continuation(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply({'id': f'unknown-{len(calls)}', 'type': 'function',
                           'function': {'name': 'missing_tool', 'arguments': '{}'}})
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] in {'no_execution_progress', 'tool_budget'}
            assert not any(item['source'] == 'executed' for item in session.record['last_run']['tools'])
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_premature_empty_replies_get_bounded_nudge_then_checkpoint(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            return _reply(content='')
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'no_execution_progress'
            assert len([item for item in session.record['messages'] if item['role'] == 'user']) == 1
            assert session.record['last_run']['tools'] == []
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('failure,reason', [(BudgetExceeded('limit'), 'cost_or_rate_budget'),
                                           (ModelConnectionError('offline'), 'model_connection')])
def test_budget_or_connection_failure_never_extends(execution_case, monkeypatch, failure, reason):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            raise failure
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 1
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == reason
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_cancelled_turn_never_auto_continues_or_executes_pending_call(execution_case, monkeypatch):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    async def run():
        calls = []
        manager = None
        session = None
        async def model(*_args, **_kwargs):
            calls.append(len(calls))
            manager.cancel(session)
            return _reply(_call('cancelled-read'))
        manager = SessionManager(execution_case, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / 'input.txt').write_text('real input')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert len(calls) == 1
            assert session.record['status'] == 'cancelled'
            assert not any(item['source'] == 'executed' for item in session.record['last_run']['tools'])
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('completed', [False, True])
def test_bound_child_waits_only_until_execution_is_drained(execution_case, monkeypatch, completed):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', True)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)

    async def run():
        async def model(*_args, **_kwargs):
            return _reply(content='Child inspection finished.')

        manager = SessionManager(execution_case, model=model)
        try:
            parent = manager.create()
            plan = await _active_plan(manager, parent)
            task_id = await manager.agent_tasks.start(
                parent, 'Inspect the isolated input.', ['read_file'], max_rounds=1,
                delay_seconds=0 if completed else 5)
            await manager.task_plans.bind(parent, plan['plan_id'], 'inspect',
                                          agent_task_id=task_id, expected_revision=plan['revision'])
            if completed:
                result = await manager.agent_tasks.wait(parent.id, task_id, timeout_seconds=3)
                assert result['status'] == 'completed'
                assert result['drained'] is True
            view = await manager.task_plans.get(parent)
            assert view['tasks'][0]['status'] == 'waiting'
            reference = view['tasks'][0]['execution_ref']
            assert reference['kind'] == 'agent_task'
            assert reference['id'] == task_id
            assert reference['drained'] is completed
            assert reference['ready_for_review'] is completed
            unfinished, waiting = execution_state(manager, parent)
            assert unfinished is True
            assert waiting is False
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_bound_native_command_job_is_actionable_while_queued_and_after_completion(
        execution_case, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True)
    registered = dict(TOOL_REGISTRY)
    register_command_jobs()

    async def run():
        async def model(*_args, **_kwargs):
            return _reply(content='unused')

        manager = SessionManager(execution_case, model=model,
                                 confirmation_handler=lambda *_args: True)
        try:
            session = manager.create()
            plan = await _active_plan(manager, session)
            # Hold the workspace lock until the plan has bound the actual queued job.
            with manager.command_jobs.guard(manager.workspace(session), threading.Event()):
                result = await manager._tool(session, _call_command_job())
                assert len(result) == 32
                job_id = result
                bound = await manager.task_plans.bind(
                    session, plan['plan_id'], 'inspect', job_id=job_id,
                    expected_revision=plan['revision'])
                assert bound['tasks'][0]['status'] == 'waiting'
                reference = bound['tasks'][0]['execution_ref']
                assert reference['kind'] == 'command_job'
                assert reference['id'] == job_id
                assert reference['runtime_status'] == 'queued'
                assert reference['drained'] is False
                assert execution_state(manager, session) == (True, False)
                future = manager.command_jobs.futures[job_id]

            for _ in range(200):
                running = manager.command_jobs.status(session.id, job_id)
                if running['status'] == 'running':
                    break
                await asyncio.sleep(.005)
            assert running['status'] == 'running', running
            assert execution_state(manager, session) == (True, False)
            await asyncio.wait_for(asyncio.wrap_future(future), 5)
            status = manager.command_jobs.status(session.id, job_id)
            assert status['status'] == 'completed', status['result']
            assert status['result'].startswith('[退出码] 0')
            assert '42' in manager.command_jobs.logs(session.id, job_id)
            view = await manager.task_plans.get(session)
            reference = view['tasks'][0]['execution_ref']
            assert view['tasks'][0]['status'] == 'waiting'
            assert reference['drained'] is True
            assert reference['ready_for_review'] is True
            assert execution_state(manager, session) == (True, False)
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(registered)


def _call_command_job():
    return {'id': 'start-native-job', 'type': 'function',
            'function': {'name': 'start_command_job', 'arguments': json.dumps({
                'command': 'python3 -c "import time; time.sleep(.2); print(42)"',
                'timeout': 2,
            })}}


@pytest.mark.parametrize('tool_name,arguments', [
    ('read_file', {'path': 'missing.txt'}),
    ('create_file', {'path': 'input.txt', 'content': 'must not replace existing file'}),
    ('run_command', {'command': 'true', 'network': True}),
])
def test_native_failure_or_blocked_result_does_not_earn_stage(execution_case, monkeypatch, tool_name, arguments):
    monkeypatch.setattr(config, 'AUTO_CONTINUE_TASKS', True)
    monkeypatch.setattr(config, 'COMMAND_NETWORK', 'off')

    async def run():
        calls = 0
        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            return _reply({'id': f'failure-{calls}', 'type': 'function',
                           'function': {'name': tool_name, 'arguments': json.dumps(arguments)}})
        manager = SessionManager(execution_case, model=model, confirmation_handler=lambda *_: True)
        try:
            session = manager.create()
            target = manager.workspace(session) / 'input.txt'
            target.write_text('original')
            await _active_plan(manager, session)
            await _submit(manager, session)
            assert calls == 2
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['automatic_continuations'] == 0
            assert all(not item['execution_progress'] for item in session.record['last_run']['tools'])
            assert target.read_text() == 'original'
        finally:
            await manager.shutdown()
    asyncio.run(run())
