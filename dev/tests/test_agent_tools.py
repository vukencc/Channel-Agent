"""Agent dispatch uses real CRUD tools and feeds their actual outcomes back."""
import asyncio
import json

import pytest

from core.sessions import SessionManager
from core.storage import SessionStore
from tools import sandbox


@pytest.mark.parametrize('allowed', [True, False])
def test_agent_dispatches_crud_and_returns_actual_outcome(sandbox_env, monkeypatch, allowed):
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: allowed)
    replies = []
    async def model(history, **kwargs):
        if not replies:
            replies.append('called')
            return {'role': 'assistant', 'content': None, 'tool_calls': [{
                'id': 'create-1', 'type': 'function', 'function': {
                    'name': 'create_file', 'arguments': json.dumps({
                        'path': 'notes/a.txt', 'content': 'hello', 'reason': '用户要求保存'})}}]}
        result = history[-1]
        assert result['role'] == 'tool'
        assert result['tool_call_id'] == 'create-1'
        assert ('[完成]' if allowed else '[已取消]') in result['content']
        replies.append('observed')
        return {'role': 'assistant', 'content': '已处理工具返回结果。'}
    async def run():
        store = SessionStore(sandbox_env.parent / 'state', sandbox_env)
        manager = SessionManager(store, model=model)
        monkeypatch.setattr(manager, '_confirm', lambda *args: allowed)
        session = manager.create()
        workspace = store.workspace(session.id)
        manager.submit(session, '把 hello 保存为 notes/a.txt')
        await session.task
        assert session.record['status'] == 'idle'
        await manager.shutdown()
        store.close()
        return workspace
    workspace = asyncio.run(run())
    assert replies == ['called', 'observed']
    assert (workspace / 'notes/a.txt').exists() is allowed
    if allowed:
        assert (workspace / 'notes/a.txt').read_text() == 'hello'
