import asyncio
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core import llm
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_session_subset_is_sent_and_unselected_tool_is_denied(tmp_path, monkeypatch):
    captured = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        manager = SessionManager(store, model=llm._open_stream)
        session = manager.create()
        manager.set_tool_names(session, ['read_file'])
        async def run():
            await manager._call_model([], session_id=session.id)
            result = await manager._tool(session, {'id': 'blocked', 'function': {
                'name': 'list_files', 'arguments': '{}'}})
            assert '会话工具子集' in result
            manager.set_tool_names(session, [])
            await manager._call_model([], session_id=session.id)
            await manager.shutdown()
        asyncio.run(run())
        assert [row['function']['name'] for row in captured[0]['tools']] == ['read_file']
        assert 'tools' not in captured[1] and 'tool_choice' not in captured[1]
        assert store.load_all()[0]['tool_names'] == []
        assert 'tool_subset_denied' in (store.directory(session.id) / 'audit.jsonl').read_text()
    finally:
        store.close()


def test_stable_prefix_keeps_memory_and_budget_at_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', True, raising=False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    captured = []
    async def model(history, **kwargs):
        captured.append(history)
        return {'role': 'assistant', 'content': '测试桩回答'}
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store, model=model)
        session = manager.create(prompt='固定系统提示')
        try:
            store.remember(session.id, '用户偏好中文')
            manager.submit(session, '你好')
            await session.task
            store.remember(session.id, '用户偏好简洁')
            manager.submit(session, '继续')
            await session.task
            assert captured[0][0] == captured[1][0]
            assert '会话记忆' not in captured[0][0]['content']
            assert '本轮剩余模型交互次数' not in captured[0][0]['content']
            assert captured[0][-1]['role'] == 'system'
            assert '用户偏好中文' in captured[0][-1]['content']
            assert session.record['messages'][0]['content'] == '固定系统提示'
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_unknown_subset_fails_closed(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        manager = SessionManager(store)
        session = manager.create()
        with pytest.raises(ValueError, match='未知工具'):
            manager.set_tool_names(session, ['no_such_tool'])
    finally:
        store.close()


def test_tool_selection_is_context_local(monkeypatch):
    from ai_agent_startup.core.tool_settings import model_tools, selected_schemas
    async def task(names):
        with model_tools(names):
            await asyncio.sleep(0)
            assert [row['function']['name'] for row in selected_schemas(llm.TOOL_SCHEMAS)] == names
    async def run():
        await asyncio.gather(task(['read_file']), task(['rag_search']), task([]))
    asyncio.run(run())


def test_compaction_and_summary_keep_stable_system_prefix(monkeypatch):
    from ai_agent_startup.core.context import build_model_history, prepare_model_history, history_size
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', True)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1800)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 2000)
    messages = [{'role': 'system', 'content': '固定前缀'}]
    for index in range(10):
        messages += [{'role': 'user', 'content': str(index)}, {'role': 'assistant', 'content': '历史' * 200}]
    messages.append({'role': 'user', 'content': '继续'})
    history, metrics = build_model_history(messages)
    assert metrics['omitted_turns'] > 0
    assert history[0] == messages[0]
    assert metrics['sent_chars'] == history_size(history)
    async def judge(prompt):
        return '摘要测试桩，不调用外部模型'
    history, metrics = asyncio.run(prepare_model_history(messages, judge=judge, cache={}))
    assert history[0] == messages[0]
    assert metrics['summary'] == 'applied'
    assert metrics['sent_chars'] == history_size(history)
