"""结构化上下文中工作区记忆的同轮交付与保底协议。"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.llm import ModelResponseError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.workspace_memory import WorkspaceMemoryDelta, WorkspaceMemoryStore


class Supervisor:
    settings = SimpleNamespace(enabled=True, check_interval=1, variance_threshold=.1,
                               timeout=20, max_input_chars=12000, max_chunks=24)

    async def inspect(self, snapshot):
        return {'compact': True, 'reason': 'test', 'values': [float(i % 2) for i in range(len(snapshot['chunks']))]}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        return '受控摘要'


@pytest.mark.parametrize('case', ['delivered', 'no_archive', 'changed', 'no_reader', 'reader_revoked', 'failed_first', 'failed_fitted'])
def test_structured_memory_delivery_gate(tmp_path, monkeypatch, sandbox_env, case):
    for name, value in {
        'ENABLE_STRUCTURED_CONTEXT': True, 'WORKSPACE_MEMORY_ENABLED': True,
        'CONTEXT_HISTORY_CHUNK_CHARS': 160, 'CONTEXT_HISTORY_KEEP_GROUPS': 2,
        'CONTEXT_COMPACT_RATIO': .01 if case != 'no_archive' else .95,
        'CONTEXT_COMPACT_MIN_NEW_GROUPS': 0, 'MODEL_RECOVERY_LIMIT': 1,
        'WORKSPACE_MEMORY_INJECT_CHARS': 6000, 'FILE_READ_CHARS': 6000,
        'ENABLE_TASK_PLANS': False, 'ENABLE_AGENT_TASKS': False,
        'ENABLE_COMMAND_JOBS': False,
    }.items():
        monkeypatch.setattr(config, name, value)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'input.txt').write_text('最新工具结果', encoding='utf-8')
    stat = project.stat()
    memory_store = WorkspaceMemoryStore(project, (stat.st_dev, stat.st_ino))
    memory_store.append(WorkspaceMemoryDelta(project=['项目事实'], progress='进度标记' * 250,
                        lessons=['经验标记' * 50]), update_id='seed', outcome='idle',
                        cancelled=threading.Event(), authorize=lambda: True, audit=lambda *a, **k: None)
    before = (project / '.channelagent' / 'memory.json').read_bytes()
    seen = []
    metrics = []
    frames = []
    original_chunks = {}
    manager = None

    async def model(history, *, session_id, emit):
        session = manager.sessions[session_id]
        seen.append(history)
        metrics.append(dict(session.record['last_run'].get('context', {})))
        frames.append(await manager.workspace_memory.frame(session))
        if len(seen) == 1:
            original_chunks.update({path: path.read_bytes() for path in
                                    (manager.store.directory(session.id) / 'context' / 'chunks').glob('*.json')})
        if case == 'failed_first' and len(seen) == 1:
            raise ModelResponseError('受控失败')
        if case == 'failed_fitted' and len(seen) == 2:
            raise ModelResponseError('受控失败')
        if len(seen) == (2 if case == 'failed_first' else 1):
            if case == 'reader_revoked':
                monkeypatch.setattr(manager, '_session_schemas', lambda s: [])
            if case == 'changed':
                memory_store.append(WorkspaceMemoryDelta(progress='新进度'), update_id='changed', outcome='idle',
                                    cancelled=threading.Event(), authorize=lambda: True, audit=lambda *a, **k: None)
            return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'latest', 'type': 'function',
                    'function': {'name': 'read_file', 'arguments': '{"path":"input.txt"}'}}]}
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        nonlocal manager
        store = SessionStore(tmp_path / 'state', sandbox_env)
        monkeypatch.setattr(store, 'memory_for_model', lambda *a, **k: '私有记忆标记')
        manager = SessionManager(store, model=model, permission_override='readonly')
        manager.history_context.supervisor = Supervisor()
        original_schemas = manager._session_schemas
        monkeypatch.setattr(manager, '_session_schemas', lambda s: [row for row in original_schemas(s)
                            if row['function']['name'] == ('list_files' if case == 'no_reader' else 'read_file')])
        monkeypatch.setattr(manager, '_effective_session_tools', lambda s, **k: None)
        session = manager.create(prompt='自定义角色', workspace_path=str(project))
        for i in range(8):
            session.record['messages'].extend([{'role': 'user', 'content': f'旧问题{i}' + '甲' * 200},
                                               {'role': 'assistant', 'content': f'旧回答{i}' + '乙' * 200}])
        try:
            manager.submit(session, '当前请求')
            await session.task
            assert session.record['status'] == 'idle', session.record.get('error')
            if case == 'failed_fitted':
                assert session.record['last_run']['recovery_context']['workspace_memory_fitted'] is True
            if case == 'delivered':
                manager.submit(session, '新一轮请求')
                await session.task
                assert '进度标记' in memory_of(seen[-1])
            for path in (store.directory(session.id) / 'context').rglob('*.json'):
                assert '私有记忆标记' not in path.read_text(encoding='utf-8')
            assert '私有记忆标记' not in json.dumps(session.record['messages'], ensure_ascii=False)
            assert all(path.read_bytes() == content for path, content in original_chunks.items())
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
    assert '进度标记' in memory_of(seen[0]) and '私有记忆标记' in memory_of(seen[0])
    assert memory_of(seen[0]) == '私有记忆标记' + frames[0].full
    if case != 'no_archive':
        assert metrics[0].get('archived_chunks', 0) > 0
    else:
        assert metrics[0].get('archived_chunks', 0) == 0
    second = seen[2 if case == 'failed_first' else 1]
    second_memory = memory_of(second)
    if case in {'delivered', 'failed_first', 'failed_fitted'}:
        if case == 'failed_first':
            assert '进度标记' in memory_of(seen[1])
        assert '进度标记' not in second_memory and '项目事实' in second_memory and '私有记忆标记' in second_memory
        assert second_memory == '私有记忆标记' + frames[2 if case == 'failed_first' else 1].floor
        assert any(call['id'] == 'latest' for message in second for call in message.get('tool_calls', []))
        assert any(message.get('tool_call_id') == 'latest' and '最新工具结果' in message.get('content', '') for message in second)
        assert metrics[2 if case == 'failed_first' else 1]['workspace_memory_fitted'] is True
        if case == 'failed_fitted':
            assert '进度标记' not in memory_of(seen[2]) and '私有记忆标记' in memory_of(seen[2])
    else:
        assert '进度标记' in second_memory
    if case != 'changed':
        assert (project / '.channelagent' / 'memory.json').read_bytes() == before


def memory_of(history):
    message = next(item for item in history if item.get('content', '').startswith('## 当前Session总结\n'))
    return json.loads(message['content'].split('\n', 2)[2])['user_memory']
