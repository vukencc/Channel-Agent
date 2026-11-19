"""Flow 默认私有历史策略的离线回归。"""
import asyncio

import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_frontend.web.context_status import context_status


@pytest.mark.parametrize("global_enabled,explicit,flow_setting,expected", [
    (False, False, None, True), (False, True, None, False),
    (False, False, False, False), (False, True, True, False),
    (True, True, False, True), (False, False, True, True),
])
def test_flow_context_selection(tmp_path, monkeypatch, sandbox_env, global_enabled, explicit, flow_setting, expected):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", global_enabled)
    monkeypatch.setattr(config, "STRUCTURED_CONTEXT_EXPLICIT", explicit, raising=False)
    monkeypatch.setattr(config, "FLOW_STRUCTURED_CONTEXT", flow_setting, raising=False)
    monkeypatch.setattr(config, "CONTEXT_SUPERVISOR_ENV_FILE", tmp_path / "absent")
    store = SessionStore(tmp_path / "state", sandbox_env)
    async def run():
        manager = SessionManager(store)
        ordinary = manager.create(prompt="ordinary")
        flow = manager.create(prompt="flow")
        flow.record["flow_id"] = "flow"
        service = manager.context_for_session(flow)
        assert (service is not None) is expected
        assert (manager.context_for_session(ordinary) is not None) is global_enabled
        assert context_status(flow, service)["structured"] is expected
        if service is not None:
            assert not service.supervisor.settings.enabled
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_automatic_archive_uses_local_summaries_with_dormant_supervisor(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'STRUCTURED_CONTEXT_EXPLICIT', False)
    monkeypatch.setattr(config, 'FLOW_STRUCTURED_CONTEXT', None)
    supervisor = tmp_path / 'old-supervisor.env'
    supervisor.write_text('SUPERVISOR_ENABLED=true\n', encoding='utf-8')
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', supervisor)
    store = SessionStore(tmp_path / 'state', sandbox_env)

    async def run():
        manager = SessionManager(store)
        try:
            session = manager.create(prompt='任务')
            session.record['flow_id'] = 'flow'
            assert manager.context_for_session(session).supervisor.settings.enabled is False
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_auto_flow_preserves_unread_batch_and_archives_delivered(tmp_path, monkeypatch, sandbox_env):
    for name, value in {
        'ENABLE_STRUCTURED_CONTEXT': False, 'STRUCTURED_CONTEXT_EXPLICIT': False,
        'FLOW_STRUCTURED_CONTEXT': None, 'CONTEXT_SUPERVISOR_ENV_FILE': tmp_path / 'absent',
        'MODEL_INPUT_CHARS': 12000, 'MODEL_INPUT_TOKENS': 16000,
        'CONTEXT_HISTORY_KEEP_GROUPS': 0, 'CONTEXT_HISTORY_CHUNK_CHARS': 1000,
        'CONTEXT_RESERVE_USER_CHARS': 0, 'CONTEXT_RESERVE_SEARCH_CHARS': 0,
        'CONTEXT_RESERVE_USER_TOKENS': 0, 'CONTEXT_RESERVE_SEARCH_TOKENS': 0,
    }.items():
        monkeypatch.setattr(config, name, value)
    store = SessionStore(tmp_path / 'state', sandbox_env)

    async def run():
        manager = SessionManager(store)
        session = manager.create(prompt='任务')
        session.record['flow_id'] = 'flow'
        session.record['last_run'] = {'turn_id': 'turn'}
        session.record['messages'].extend([
            {'role': 'user', 'content': '执行'},
            {'role': 'assistant', 'content': '已交付思考' * 2500, 'tool_calls': [
                {'id': 'old', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'old', 'content': '已交付回执' * 300},
        ])
        context = manager.context_for_session(session)
        try:
            context.mark_delivered(session, session.record['messages'])
            session.record['messages'].extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'new', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'new', 'content': '未读回执' * 500},
            ])
            history, metrics = await context.prepare(session, session.record['messages'], schemas=[])
            assert metrics['archived_chunks'] > 0
            assert not any(item.get('tool_call_id') == 'old' for item in history)
            assert any(item.get('tool_call_id') == 'new' and item['content'] == '未读回执' * 500 for item in history)
            assert not {'history_read', 'history_search'} & {row['function']['name'] for row in manager._session_schemas(session)}
            session.record['messages'][-1]['content'] = '未读回执' * 10000
            with pytest.raises(ContextBudgetError):
                await context.prepare(session, session.record['messages'], schemas=[])
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_actual_flow_uses_auto_context_from_first_request(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'STRUCTURED_CONTEXT_EXPLICIT', False)
    monkeypatch.setattr(config, 'FLOW_STRUCTURED_CONTEXT', None)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'absent')
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    project = tmp_path / 'project'
    project.mkdir()
    store = SessionStore(tmp_path / 'state', sandbox_env)
    calls = []

    async def model(history, **kwargs):
        calls.append(history)
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        manager = SessionManager(store, model=model)
        try:
            flows = FlowService(manager)
            flow = flows.create(workspace_path=str(project))
            flow['nodes'] = [{'id': 'node', 'title': '节点', 'prompt': '完成任务'}]
            flow = flows.save(flow)
            flows.start(flow['id'], input_text='输入')
            await asyncio.wait_for(flows.tasks[flow['id']], 8)
            assert flows.get(flow['id'])['run']['status'] == 'completed'
            session = next(s for s in manager.sessions.values() if s.record.get('flow_id') == flow['id'])
            assert session.record['last_run']['context']['structured'] is True
            assert len(calls) == 1
            assert any(m.get('content', '').startswith('## 用户输入\n') for m in calls[0])
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
