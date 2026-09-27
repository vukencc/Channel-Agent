import json

from tools import TOOL_REGISTRY


def test_sixty_thousand_characters_saved_in_bounded_segments(sandbox_env):
    assert 'append_file' in TOOL_REGISTRY
    target = sandbox_env / 'long.txt'
    target.write_text('')
    for offset in range(0, 60000, 4000):
        result = TOOL_REGISTRY['append_file'].run(json.dumps({'path': 'long.txt', 'content': '中' * 4000, 'expected_chars': offset}))
        assert '[完成]' in result
    assert target.read_text() == '中' * 60000
    result = TOOL_REGISTRY['append_file'].run(json.dumps({'path': 'long.txt', 'content': '重复', 'expected_chars': 56000}))
    assert '不匹配' in result
    assert target.read_text() == '中' * 60000


def test_append_denial_preserves_file(sandbox_env, monkeypatch):
    from tools import sandbox
    monkeypatch.setattr(sandbox, 'confirmer', lambda *_: False)
    target = sandbox_env / 'a'
    target.write_text('原文')
    assert 'append_file' in TOOL_REGISTRY
    result = TOOL_REGISTRY['append_file'].run('{"path":"a","content":"新文","expected_chars":2}')
    assert '取消' in result and target.read_text() == '原文'


def test_agent_finishes_sixty_k_file_without_manual_continue(tmp_path, monkeypatch):
    import asyncio
    import config
    from core.sessions import SessionManager
    from core.storage import SessionStore
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        offset = 0
        calls = 0
        async def model(history, **kwargs):
            nonlocal offset, calls
            if history[-1]['role'] == 'user':
                name, arguments = 'create_file', {'path': 'long.txt', 'content': ''}
            elif offset < 60000:
                assert '[完成]' in history[-1]['content']
                name, arguments = 'append_file', {'path': 'long.txt', 'content': '中' * 4000, 'expected_chars': offset}
                offset += 4000
            else:
                assert 'next_offset=60000' in history[-1]['content']
                return {'role': 'assistant', 'content': '分段完成'}
            calls += 1
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': str(calls), 'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}
        manager = SessionManager(store, model=model)
        monkeypatch.setattr(manager, '_confirm', lambda *_: True)
        session = manager.create()
        manager.submit(session, '分段保存 60000 字符')
        await session.task
        assert session.record['status'] == 'idle'
        assert calls == 16
        assert (store.workspace(session.id) / 'long.txt').read_text() == '中' * 60000
        assert len([m for m in session.record['messages'] if m['role'] == 'tool']) == 16
        await manager.shutdown()
        store.close()
    asyncio.run(run())
