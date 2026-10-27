"""Per-session delegation schemas agree with the enforced child round cap."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from openai import pydantic_function_tool

from agent_core import config
from agent_core.core import llm
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools.task_plans import PlanUpdateArgs


def _rounds_schema(tools):
    row = next(item for item in tools if item['function']['name'] == 'create_session')
    return row['function']['parameters']['properties']['max_rounds']


@pytest.fixture
def schema_store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def test_real_model_requests_use_each_session_round_cap_without_mutating_global_schema(
        schema_store, monkeypatch):
    captured = {}
    original = copy.deepcopy(llm.TOOL_SCHEMAS)

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **_kwargs):
            return self

        async def create(self, **kwargs):
            captured[kwargs['extra_headers']['x-opencode-session']] = copy.deepcopy(kwargs['tools'])

            async def stream():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(
                    finish_reason='stop', delta=SimpleNamespace(
                        content='done', reasoning_content=None, tool_calls=None))])

            return stream()

    monkeypatch.setattr(llm, 'endpoint_client', lambda _endpoint: Client())
    monkeypatch.setattr(llm, 'model_options', lambda: {})
    monkeypatch.setattr(llm, 'selected_model', lambda: 'stub-model')

    async def run():
        manager = SessionManager(schema_store, model=llm.call_model)
        try:
            sessions = [manager.create(), manager.create()]
            for session, cap in zip(sessions, (4, 9)):
                session.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': cap}
                session.record['tool_names'] = ['create_session']
            history = [{'role': 'system', 'content': 'test'},
                       {'role': 'user', 'content': 'inspect schema'}]
            answers = await asyncio.gather(*(manager._call_model(
                history, session_id=session.id, emit=lambda *_args: None) for session in sessions))
            assert all(answer['content'] == 'done' for answer in answers)
            assert set(captured) == {session.id for session in sessions}
            for session, cap in zip(sessions, (4, 9)):
                schema = _rounds_schema(captured[session.id])
                assert schema['maximum'] == cap
                assert schema['default'] == 0
                assert schema['minimum'] == 0
            assert llm.TOOL_SCHEMAS == original
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_structured_context_receives_same_session_bounded_schemas(schema_store, monkeypatch,
                                                                  tmp_path):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env')
    captured = {}
    original = copy.deepcopy(llm.TOOL_SCHEMAS)

    async def model(*_args, **_kwargs):
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(schema_store, model=model)
        try:
            async def prepare(session, history, *, schemas, **_kwargs):
                captured[session.id] = copy.deepcopy(schemas)
                return history, {'structured': True}

            monkeypatch.setattr(manager.history_context, 'prepare', prepare)
            sessions = [manager.create(), manager.create()]
            for session, cap in zip(sessions, (4, 9)):
                session.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': cap}
                session.record['tool_names'] = ['create_session']
                manager.submit(session, f'Check bounded context schema for cap {cap}')
            await asyncio.wait_for(asyncio.gather(*(session.task for session in sessions)), 10)
            assert all(session.record['status'] == 'idle' for session in sessions)
            for session, cap in zip(sessions, (4, 9)):
                schema = _rounds_schema(captured[session.id])
                assert schema['maximum'] == cap
                assert schema['default'] == 0
                assert schema['minimum'] == 0
            assert llm.TOOL_SCHEMAS == original
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_over_cap_create_session_tool_rejects_without_creating_child(schema_store):
    async def model(*_args, **_kwargs):
        return {'role': 'assistant', 'content': 'unused'}

    async def run():
        manager = SessionManager(schema_store, model=model,
                                 confirmation_handler=lambda *_args: True)
        try:
            parent = manager.create()
            parent.record['budget_overrides'] = {'MAX_TOOL_ROUNDS': 4}
            call = {'id': 'over-cap', 'type': 'function',
                    'function': {'name': 'create_session', 'arguments': json.dumps({
                        'title': 'Excessive child', 'task': 'Read a file', 'max_rounds': 8})}}
            with pytest.raises(ValueError, match='轮次|上限'):
                await manager._tool(parent, call)
            assert set(manager.sessions) == {parent.id}
            assert manager.agent_tasks is None or manager.agent_tasks.records == {}
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_plan_update_schema_explains_completion_transition_and_evidence_identity():
    schema = pydantic_function_tool(PlanUpdateArgs, name='plan_update')
    properties = schema['function']['parameters']['properties']
    assert 'completed' in properties['result']['description']
    assert 'plan_get.available_evidence.id' in properties['evidence_refs']['description']
    status_guide = properties['status']['description']
    assert all(transition in status_guide for transition in
               ('pending', 'in_progress', 'completed'))
