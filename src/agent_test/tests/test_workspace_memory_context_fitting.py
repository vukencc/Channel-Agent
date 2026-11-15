import asyncio
import json
import threading

import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.llm import ModelResponseError
from agent_core.core.context import ContextBudgetError, build_model_history
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import bounded_schemas
from agent_core.core.workspace_memory import WorkspaceMemoryDelta, WorkspaceMemoryStore
from agent_core.tools.file_crud import read_file
from agent_core.tools.sandbox import tool_context


@pytest.mark.parametrize('case', ['delivered', 'changed', 'no_reader', 'unread', 'failed_partial'])
@pytest.mark.parametrize('environment', ['sandbox', 'local'])
def test_previously_delivered_memory_fits_latest_tool_batch(tmp_path, monkeypatch, sandbox_env, case, environment):
    monkeypatch.setattr(config, "WORKSPACE_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY", False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)
    monkeypatch.setattr(config, "MODEL_INPUT_CHARS", 10000)
    monkeypatch.setattr(config, "MODEL_INPUT_TOKENS", 8000)
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', False)
    monkeypatch.setattr(config, 'WORKSPACE_MEMORY_INJECT_CHARS', 6000)
    monkeypatch.setattr(config, 'FILE_READ_CHARS', 6000)
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 20000)
    result_count = 0
    project = tmp_path / "project"
    project.mkdir()
    status = project.stat()
    store = WorkspaceMemoryStore(project, (status.st_dev, status.st_ino))
    store.append(WorkspaceMemoryDelta(project=['项目事实 literal\\u4e2d 中文 {"x":1}'], progress="进度" * 500,
                 lessons=["经验" * 200]), update_id="seed", outcome="idle",
                 cancelled=threading.Event(), authorize=lambda: True, audit=lambda *a, **k: None)
    before = (project / ".channelagent" / "memory.json").read_bytes()
    seen = []
    manager = None
    async def model(history, **kwargs):
        nonlocal result_count
        seen.append(json.dumps(history, ensure_ascii=False))
        if case == 'failed_partial':
            kwargs['emit']('content', '部分响应')
            raise ModelResponseError('incomplete')
        if len(seen) == 1:
            session = manager.sessions[kwargs['session_id']]
            initial_frame = await manager.workspace_memory.frame(session)
            if case == 'delivered':
                frame = initial_frame
                schemas = manager._session_schemas(session)
                assert manager.workspace_memory.can_reread(session, frame, schemas)
                runtime = manager.workspace_memory
                context = runtime._context(session, runtime._store(session), asyncio.get_running_loop(), 'readonly')
                with tool_context(context):
                    assert '项目事实' in read_file('.channelagent/memory.json')
                original = manager.workspaces.protected_paths
                monkeypatch.setattr(manager.workspaces, 'protected_paths', lambda: [project / '.channelagent'])
                assert not runtime.can_reread(session, frame, schemas)
                monkeypatch.setattr(manager.workspaces, 'protected_paths', original)
                identity = manager.workspaces.identity
                monkeypatch.setattr(manager.workspaces, 'identity', lambda session: (0, 0))
                assert not runtime.can_reread(session, frame, schemas)
                monkeypatch.setattr(manager.workspaces, 'identity', identity)
            if case == 'changed':
                store.append(WorkspaceMemoryDelta(progress='新进度'), update_id='changed', outcome='idle',
                             cancelled=threading.Event(), authorize=lambda: True, audit=lambda *a, **k: None)
            frame = await manager.workspace_memory.frame(session)
            current_history = [dict(message) for message in history]
            # 变化场景重新渲染新快照，不能把旧快照误用作预算基准。
            if case == 'changed':
                current_history[0]['content'] = current_history[0]['content'].replace(initial_frame.full, frame.full)
            schemas_now = bounded_schemas(manager._session_schemas(session), manager._effective_session_tools(session))
            call = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'batch', 'function': {'name': 'read_file', 'arguments': '{"path":"input.txt","limit":6000}'}}]}
            fits = []
            for count in range(100, 1501, 25):
                batch = [*current_history, call, {'role': 'tool', 'tool_call_id': 'batch', 'content': '工具结果' * count + '余' * 100}]
                floor = [dict(message) for message in batch]
                floor[0]['content'] = floor[0]['content'].replace(frame.full, frame.floor)
                try:
                    build_model_history(batch, schemas=schemas_now)
                except ContextBudgetError:
                    try:
                        build_model_history(floor, schemas=schemas_now)
                    except ContextBudgetError:
                        continue
                    fits.append(count)
            assert fits, '未能构造完整记忆超预算且保底记忆可容纳的工具批次'
            result_count = fits[len(fits) // 2]
            (project / 'input.txt').write_text('工具结果' * result_count, encoding='utf-8')
            return call
        return {"role": "assistant", "content": "done"}
    async def run():
        nonlocal manager
        session_store = SessionStore(tmp_path / "state", tmp_path / "private")
        monkeypatch.setattr(session_store, 'memory_for_model', lambda *a, **k: '私有会话记忆独占')
        manager = SessionManager(session_store, model=model, permission_override="readonly")
        monkeypatch.setattr(manager, 'execution_environment', lambda session: environment)
        monkeypatch.setattr(manager, '_effective_session_tools', lambda session, **kwargs: None)
        schemas = manager._session_schemas
        def selected_schemas(session):
            selected = [s for s in schemas(session) if s['function']['name'] == 'read_file']
            assert selected
            return [] if case == 'no_reader' else selected
        monkeypatch.setattr(manager, '_session_schemas', selected_schemas)
        async def tool(session, call, *, evidence):
            evidence["source"] = "executed"
            return "工具结果" * result_count
        if case != 'delivered':
            monkeypatch.setattr(manager, "_tool", tool)
        session = manager.create(prompt='测试系统提示', workspace_path=str(project))
        manager.submit(session, "读取文件" + ('请求' * 4000 if case == 'unread' else ''))
        await session.task
        assert session.record["status"] == ('idle' if case == 'delivered' else 'error' if case == 'failed_partial' else 'checkpoint')
        assert all('私有会话记忆独占' not in str(message) for message in session.record['messages'])
        await manager.shutdown()
        session_store.close()
    asyncio.run(run())
    assert len(seen) == (2 if case == 'delivered' else 0 if case == 'unread' else 1)
    if case == 'delivered':
        assert '项目事实 literal\\\\u4e2d 中文' in seen[1] and "工具结果" * result_count in seen[1]
        assert '私有会话记忆独占' in seen[1]
        assert "近期进度" in seen[0] and "省略" in seen[1]
    if case != 'changed':
        assert (project / ".channelagent" / "memory.json").read_bytes() == before
