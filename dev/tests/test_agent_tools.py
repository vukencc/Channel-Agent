"""Agent dispatch uses real CRUD tools and feeds their actual outcomes back."""
import asyncio
import json

import pytest

from core import agent
from tools import sandbox


@pytest.mark.parametrize('allowed', [True, False])
def test_agent_dispatches_crud_and_returns_actual_outcome(sandbox_env, monkeypatch, allowed):
    monkeypatch.setattr(sandbox, 'confirmer', lambda *args: allowed)
    replies = []
    class StopSession(Exception):
        pass
    def user_input(prompt):
        if replies:
            raise StopSession
        return '把 hello 保存为 notes/a.txt'
    async def model(history):
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
    monkeypatch.setattr('builtins.input', user_input)
    monkeypatch.setattr(agent, 'call_model', model)
    with pytest.raises(StopSession):
        asyncio.run(agent.session_loop({'prompt': agent.DEFAULT_PROMPT}))
    assert replies == ['called', 'observed']
    assert (sandbox_env / 'notes/a.txt').exists() is allowed
    if allowed:
        assert (sandbox_env / 'notes/a.txt').read_text() == 'hello'
