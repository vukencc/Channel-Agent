"""Permission modes at the session, headless, and tool boundaries."""

import asyncio
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.headless import run_headless
from ai_agent_startup.core.prompts import DEFAULT_PROMPT
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools import web_search


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def _call(name, arguments, identifier=None):
    return {'id': identifier or name,
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def _audit(store, session):
    path = store.directory(session.id) / 'audit.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_full_access_parent_creates_child_without_confirmation_and_inherits_policy(store):
    confirmations = []

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'child done'}

    async def run():
        manager = SessionManager(store, model=model,
                                 confirmation_handler=lambda *args: confirmations.append(args) or False)
        try:
            parent = manager.create('full parent')
            manager.set_permission_policy(parent, 'full_access')
            await manager.flush()
            response = await manager._tool(parent, _call('create_session', {
                'task': 'inspect', 'tool_names': ['read_file'], 'max_rounds': 1,
            }))
            created = json.loads(response)
            child = manager.sessions[created['session_id']]
            await manager.agent_tasks.tasks[created['task_id']]
            assert child.record['permission_policy'] == 'full_access'
            assert child.record['delegated_from'] == parent.id
            assert child.record['tool_names'] == ['read_file']
            assert store.workspace(child.id) != store.workspace(parent.id)
            assert confirmations == []
            assert any(row.get('event') == 'confirm' and row.get('action') == 'create_session'
                       and row.get('decision_source') == 'full_access' for row in _audit(store, parent))
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_smart_parent_create_session_requires_confirmation_and_denial_isolated(store):
    confirmations = []

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *args: confirmations.append(args) or False)
        try:
            smart = manager.create('smart parent')
            full = manager.create('full sibling')
            manager.set_permission_policy(smart, 'smart')
            manager.set_permission_policy(full, 'full_access')
            await manager.flush()
            response = await manager._tool(smart, _call('create_session', {
                'task': 'inspect', 'tool_names': [], 'max_rounds': 1,
            }))
            assert '取消' in response
            assert len(confirmations) == 1
            assert len(manager.sessions) == 2
            assert not manager.agent_tasks or not manager.agent_tasks.records
            assert smart.record['permission_policy'] == 'smart'
            assert full.record['permission_policy'] == 'full_access'
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_smart_parent_confirmed_child_inherits_smart_mode(store):
    confirmations = []

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'child done'}

    async def run():
        manager = SessionManager(store, model=model,
                                 confirmation_handler=lambda *args: confirmations.append(args) or True)
        try:
            parent = manager.create()
            manager.set_permission_policy(parent, 'smart')
            await manager.flush()
            created = json.loads(await manager._tool(parent, _call('create_session', {
                'task': 'inspect', 'tool_names': ['read_file'], 'max_rounds': 1,
            })))
            child = manager.sessions[created['session_id']]
            await manager.agent_tasks.tasks[created['task_id']]
            assert len(confirmations) == 1
            assert child.record['permission_policy'] == 'smart'
            assert child.record['tool_names'] == ['read_file']
            assert child.id != parent.id
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_headless_override_applies_to_existing_smart_session(store):
    async def model(history, **kwargs):
        if any(row['role'] == 'tool' for row in history):
            return {'role': 'assistant', 'content': 'done'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            _call('create_file', {'path': 'headless.txt', 'content': 'created'}, 'headless-write')]}

    async def run():
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.set_permission_policy(session, 'smart')
        await manager.shutdown()
        result = await run_headless(store, 'create a file', session_id=session.id,
                                    policy='full_access', model=model)
        assert result['status'] == 'idle'
        assert result['permission_policy'] == 'full_access'
        assert (store.workspace(session.id) / 'headless.txt').read_text() == 'created'
        assert any(row.get('event') == 'confirm' and row.get('decision_source') == 'full_access'
                   for row in _audit(store, session))

    asyncio.run(run())


def test_smart_web_search_confirms_even_when_legacy_toggle_is_off(store, monkeypatch):
    monkeypatch.setattr(config, 'WEB_SEARCH_CONFIRM', 'off')
    monkeypatch.setattr(config, 'WEB_SEARCH_API_KEY', 'test-only-token')
    requests = []
    confirmations = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            requests.append('created')

    monkeypatch.setattr(web_search.httpx, 'Client', FakeClient)

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *args: confirmations.append(args) or False)
        try:
            smart = manager.create()
            manager.set_permission_policy(smart, 'smart')
            await manager.flush()
            result = await manager._tool(smart, _call('web_search', {'query': 'sensitive terms'}))
            assert '取消' in result
            assert len(confirmations) == 1
            assert requests == []
            assert any(row.get('event') == 'confirm' and row.get('action') == 'web_search'
                       and row.get('allowed') is False for row in _audit(store, smart))
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_full_access_keeps_path_boundary_and_session_tool_subset(store):
    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: pytest.fail('unexpected confirmation'))
        try:
            parent = manager.create()
            manager.set_permission_policy(parent, 'full_access')
            manager.set_tool_names(parent, ['create_file'])
            await manager.flush()
            denied = await manager._tool(parent, _call('delete_file', {'path': 'x.txt'}))
            assert '拒绝' in denied
            escaped = await manager._tool(parent, _call('create_file', {
                'path': '../outside.txt', 'content': 'blocked',
            }))
            assert '拦截' in escaped
            assert not (store.workspace(parent.id).parent / 'outside.txt').exists()
            assert any(row.get('event') == 'tool_subset_denied' for row in _audit(store, parent))
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_full_access_fails_closed_when_command_isolation_unavailable(store, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: None)

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *_: pytest.fail('unexpected confirmation'))
        try:
            session = manager.create()
            manager.set_permission_policy(session, 'full_access')
            await manager.flush()
            result = await manager._tool(session, _call('run_command', {'command': 'touch marker.txt'}))
            assert '拦截' in result
            assert not (store.workspace(session.id) / 'marker.txt').exists()
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_request_permission_asks_for_unknown_risk_in_smart_and_audits_full_access(store):
    confirmations = []

    async def run():
        manager = SessionManager(store, confirmation_handler=lambda *args: confirmations.append(args) or False)
        try:
            smart = manager.create()
            full = manager.create()
            manager.set_permission_policy(smart, 'smart')
            manager.set_permission_policy(full, 'full_access')
            await manager.flush()
            arguments = {'action': 'share_summary', 'detail': 'include private customer note',
                         'reason': 'Content sensitivity cannot be determined from the local path'}
            denied = json.loads(await manager._tool(smart, _call('request_permission', arguments)))
            allowed = json.loads(await manager._tool(full, _call('request_permission', arguments)))
            assert denied == {'allowed': False, 'action': 'share_summary', 'policy': 'smart'}
            assert allowed == {'allowed': True, 'action': 'share_summary', 'policy': 'full_access'}
            assert len(confirmations) == 1
            assert smart.record['permission_policy'] == 'smart'
            assert full.record['permission_policy'] == 'full_access'
            assert any(row.get('event') == 'confirm' and row.get('allowed') is False
                       for row in _audit(store, smart))
            assert any(row.get('event') == 'confirm' and row.get('decision_source') == 'full_access'
                       for row in _audit(store, full))
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_permission_guide_is_dynamic_and_preserves_default_prompt(store):
    seen = {}

    async def model(history, **kwargs):
        seen[kwargs['session_id']] = history
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model)
        try:
            sessions = {}
            for policy in ('smart', 'full_access', 'standard'):
                session = manager.create(policy)
                manager.set_permission_policy(session, policy)
                sessions[policy] = session
                manager.submit(session, 'status')
                await session.task
            for policy, session in sessions.items():
                history = seen[session.id]
                system_text = '\n'.join(row['content'] for row in history if row['role'] == 'system')
                if policy == 'smart':
                    assert 'request_permission' in system_text
                    assert '高风险' in system_text and '确认' in system_text
                    assert '低风险' in system_text and '自动' in system_text
                    assert '拒绝' in system_text
                elif policy == 'full_access':
                    assert 'Full Access' in system_text or 'full_access' in system_text
                else:
                    assert 'request_permission' not in system_text
                assert session.record['messages'][0]['content'] == DEFAULT_PROMPT
        finally:
            await manager.shutdown()

    asyncio.run(run())
