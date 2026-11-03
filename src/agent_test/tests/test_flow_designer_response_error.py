import asyncio

import pytest

from agent_core.core.flow_designer import design_flow
from agent_core.core.llm import ModelResponseError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_incomplete_design_response_explains_output_limit_without_echo(tmp_path):
    """截断响应应指导精简输出，不应错误归因于连接配置或回显正文。"""
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def incomplete(history, *, session_id, emit):
        raise ModelResponseError('controlled-private-provider-body')

    try:
        manager = SessionManager(store, model=incomplete)
        with pytest.raises(ValueError, match='完整') as error:
            asyncio.run(design_flow(manager, [{'role': 'user', 'content': '创建客服流程'}]))
        assert '精简' in str(error.value)
        assert 'controlled-private-provider-body' not in str(error.value)
        assert error.value.__suppress_context__
        assert not (store.root / 'flows').exists()
    finally:
        store.close()
