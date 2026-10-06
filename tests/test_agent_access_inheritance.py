"""Delegated agents inherit their parent's access without gaining new authority."""

import asyncio
import json
import time

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.llm import TOOL_SCHEMAS
from ai_agent_startup.core import command_jobs
from ai_agent_startup.tools import TOOL_REGISTRY, all_schemas
from ai_agent_startup.tools import command
from ai_agent_startup.tools.sandbox import sandbox_root


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    from ai_agent_startup.tools.agent_tasks import register_agent_tasks
    from ai_agent_startup.tools.command_jobs import register_command_jobs

    previous_tools = dict(TOOL_REGISTRY)
    previous_schemas = list(TOOL_SCHEMAS)
    register_agent_tasks()
    register_command_jobs()
    TOOL_SCHEMAS[:] = all_schemas()
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(previous_tools)
    TOOL_SCHEMAS[:] = previous_schemas


def _call(name, arguments):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(arguments)}}


async def _done(*args, **kwargs):
    return {'role': 'assistant', 'content': 'done'}


async def _create(manager, parent, **arguments):
    response = await manager._tool(parent, _call('create_session', {
        'task': 'inspect project', 'max_rounds': 1, **arguments,
    }))
    result = json.loads(response)
    child = manager.sessions[result['session_id']]
    await asyncio.wait_for(manager.agent_tasks.tasks[result['task_id']], 5)
    return child


@pytest.mark.parametrize('policy', ['standard', 'smart', 'full_access'])
def test_omitted_tool_names_inherit_exact_registered_parent_set_and_policy(store, policy):
    async def run():
        manager = SessionManager(store, model=_done, confirmation_handler=lambda *_: True)
        try:
            parent = manager.create('parent')
            names = [name for name in ('read_file', 'create_file', 'create_session',
                                        'delegate', 'start_command_job', 'send_session_message')
                     if name in TOOL_REGISTRY]
            manager.set_tool_names(parent, names)
            manager.set_permission_policy(parent, policy)
            child = await _create(manager, parent)
            assert set(child.record['tool_names']) == set(names)
            assert child.record['permission_policy'] == policy
            assert child.record['delegated_from'] == parent.id
            assert child.record['budget_owner_id'] == parent.id
            assert child.record['workspace_owner_id'] == parent.id
            assert manager.workspace(child) == manager.workspace(parent)
            assert 'start_command_job' in child.record['tool_names']
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_unrestricted_parent_passes_entire_registered_set_to_both_delegate_tools(store):
    async def run():
        manager = SessionManager(store, model=_done, confirmation_handler=lambda *_: True)
        try:
            parent = manager.create('unrestricted parent')
            manager.set_tool_names(parent, None)
            expected = set(TOOL_REGISTRY)
            from_create = await _create(manager, parent)
            assert set(from_create.record['tool_names']) == expected
            identifier = await manager._tool(parent, _call('delegate', {
                'task': 'inspect through delegate', 'max_rounds': 1,
            }))
            await asyncio.wait_for(manager.agent_tasks.tasks[identifier], 5)
            from_delegate = manager.sessions[manager.agent_tasks.records[identifier]['child_id']]
            assert set(from_delegate.record['tool_names']) == expected
            assert from_delegate.record['permission_policy'] == from_create.record['permission_policy']
            assert manager.workspace(from_delegate) == manager.workspace(parent)
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('policy,confirm_job', [('standard', False), ('full_access', False)])
def test_child_command_job_uses_registered_chain_and_private_owner(store, monkeypatch, policy, confirm_job):
    def fake_execute(command_text, reason, argv, isolated, *, timeout=None, on_output=None):
        (sandbox_root() / 'job-output.txt').write_text(command_text)
        if on_output:
            on_output(b'job completed')
        return '[退出码] 0\njob completed'

    monkeypatch.setattr(command_jobs, 'isolated_command', lambda argv, **kwargs: argv)
    monkeypatch.setattr(command, '_execute', fake_execute)

    async def run():
        decision = {'allowed': True}
        confirmations = []
        def confirm(prompt, timeout):
            confirmations.append(prompt)
            return decision['allowed']
        manager = SessionManager(store, model=_done, confirmation_handler=confirm)
        try:
            parent = manager.create('job parent')
            manager.set_tool_names(parent, ['create_session', 'start_command_job', 'job_status', 'job_logs'])
            manager.set_permission_policy(parent, policy)
            child = await _create(manager, parent)
            decision['allowed'] = confirm_job
            args = {'command': 'printf job-completed', 'timeout': 2}
            before = len(manager.command_jobs.records)
            if policy == 'standard':
                with pytest.raises(PermissionError, match='未确认'):
                    await manager._tool(child, _call('start_command_job', args))
                assert len(manager.command_jobs.records) == before
                decision['allowed'] = True
            child_job = await manager._tool(child, _call('start_command_job', args))
            assert manager.command_jobs.records[child_job]['owner'] == child.id
            deadline = time.monotonic() + 3
            while manager.command_jobs.status(child.id, child_job)['status'] not in {'completed', 'failed'}:
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            status = json.loads(await manager._tool(child, _call('job_status', {'job_id': child_job})))
            assert status['status'] == 'completed'
            assert status['owner'] == child.id
            assert await manager._tool(child, _call('job_logs', {'job_id': child_job})) == 'job completed'
            assert (manager.workspace(parent) / 'job-output.txt').read_text() == args['command']
            with pytest.raises(PermissionError):
                await manager._tool(parent, _call('job_status', {'job_id': child_job}))
            parent_job = await manager._tool(parent, _call('start_command_job', args))
            assert manager.command_jobs.records[parent_job]['owner'] == parent.id
            with pytest.raises(PermissionError):
                await manager._tool(child, _call('job_status', {'job_id': parent_job}))
            assert not confirmations or policy == 'standard'
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_standard_confirmation_and_readonly_denial_remain_in_force(store):
    async def run():
        confirmations = []
        manager = SessionManager(store, model=_done,
                                 confirmation_handler=lambda *args: confirmations.append(args) or True)
        try:
            parent = manager.create()
            manager.set_tool_names(parent, ['create_session', 'read_file'])
            manager.set_permission_policy(parent, 'standard')
            child = await _create(manager, parent)
            assert child.record['permission_policy'] == 'standard'
            assert len(confirmations) == 1
            manager.set_permission_policy(parent, 'readonly')
            response = await manager._tool(parent, _call('create_session', {'task': 'denied'}))
            assert '拒绝' in response or '取消' in response
            assert len(confirmations) == 1
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_shared_child_file_access_stays_in_parent_sandbox_and_cannot_recurse(store):
    async def run():
        manager = SessionManager(store, model=_done, confirmation_handler=lambda *_: True)
        try:
            parent = manager.create()
            manager.set_tool_names(parent, ['create_session', 'create_file', 'read_file'])
            child = await _create(manager, parent)
            assert '完成' in await manager._tool(parent, _call('create_file', {
                'path': 'parent.txt', 'content': 'shared',
            }))
            assert await manager._tool(child, _call('read_file', {'path': 'parent.txt'})) == 'shared'
            assert '拦截' in await manager._tool(child, _call('read_file', {'path': '../escape.txt'}))
            assert '不可递归委派' in child.record['messages'][0]['content']
            with pytest.raises(PermissionError):
                await _create(manager, child, tool_names=['run_command'])
            with pytest.raises(PermissionError):
                await manager._tool(child, _call('create_session', {'task': 'nested'}))
            assert '拒绝' in await manager._tool(child, _call('delegate', {
                'task': 'nested', 'tool_names': [], 'max_rounds': 1,
            }))
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_child_recursive_attempts_are_denied_with_paired_tool_results(store):
    async def run():
        manager = None
        async def model(history, **kwargs):
            if any(message['role'] == 'tool' for message in history):
                return {'role': 'assistant', 'content': 'finished'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'try-create', 'type': 'function', 'function': {'name': 'create_session',
                    'arguments': json.dumps({'task': 'recursive child', 'max_rounds': 1})}},
                {'id': 'try-delegate', 'type': 'function', 'function': {'name': 'delegate',
                    'arguments': json.dumps({'task': 'recursive child', 'tool_names': [], 'max_rounds': 1})}},
            ]}
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        try:
            parent = manager.create()
            manager.set_tool_names(parent, ['create_session', 'delegate'])
            child = await _create(manager, parent, max_rounds=2)
            calls = [call['id'] for message in child.record['messages']
                     for call in message.get('tool_calls', [])]
            results = [message for message in child.record['messages'] if message['role'] == 'tool']
            assert calls == ['try-create', 'try-delegate']
            assert {message['tool_call_id'] for message in results} == set(calls)
            assert all('拒绝' in message['content'] or '失败' in message['content']
                       for message in results)
            assert len(manager.sessions) == 2
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_existing_isolated_child_binding_survives_reload(store):
    async def run():
        manager = SessionManager(store, model=_done)
        root = manager.create('root')
        child = manager.create('historical isolated child')
        child.record.update(delegated_from=root.id, workspace_mode='isolated',
                            workspace_owner_id=child.id)
        manager.save(child)
        assert store.workspace(child.id) != manager.workspace(root)
        (store.workspace(child.id) / 'legacy.txt').write_text('kept')
        await manager.flush()
        await manager.shutdown()
        manager = SessionManager(store, model=_done)
        try:
            root = manager.sessions[root.id]
            child = manager.sessions[child.id]
            assert manager.workspace(child) != manager.workspace(root)
            assert (manager.workspace(child) / 'legacy.txt').read_text() == 'kept'
        finally:
            await manager.shutdown()
    asyncio.run(run())
