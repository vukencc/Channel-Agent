"""运行时协议仅依据实际提供的工具 schema；模型为受控离线替身。"""
import asyncio

import pytest

from agent_core import config
from agent_core.core.prompts import FILE_WORKFLOW_GUIDE, runtime_file_workflow_guide
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.llm import ModelResponseError


def _schemas(*names):
    return [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}} for name in names]


@pytest.mark.parametrize("structured", [True, False])
@pytest.mark.parametrize("stable_prefix", [True, False])
@pytest.mark.parametrize("names", [("read_file", "list_files", "send_reply"), ("read_file", "list_files", "create_file"), ("read_file", "run_command")])
def test_request_uses_selected_file_protocol(tmp_path, sandbox_env, monkeypatch, structured, stable_prefix, names):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    monkeypatch.setattr(config, "MODEL_STABLE_PREFIX", stable_prefix)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    seen = []

    async def model(history, **kwargs):
        seen.append(history)
        return {"role": "assistant", "content": "完成"}

    async def run():
        store = SessionStore(tmp_path / "state", sandbox_env)
        manager = SessionManager(store, model=model)
        try:
            session = manager.create(prompt="自定义角色")
            monkeypatch.setattr(manager, "_session_schemas", lambda session: _schemas(*names))
            monkeypatch.setattr(manager, "_effective_session_tools", lambda session, **kwargs: list(names))
            manager.submit(session, "检查内容")
            await session.task
            assert seen, session.record.get("error")
            guide = "\n".join(m.get("content", "") or "" for m in seen[0])
            if "create_file" in names or "run_command" in names:
                assert FILE_WORKFLOW_GUIDE in guide
            else:
                assert "next_offset" in guide and "search" in guide
                assert "如实" in guide
                assert all(name not in guide for name in ("create_file", "append_file", "edit_file", "update_file", "delete_file", "run_command", "prepare_project_environment"))
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


@pytest.mark.parametrize('structured', [True, False])
@pytest.mark.parametrize('stable_prefix', [True, False])
def test_recovery_does_not_advertise_unavailable_file_mutations(tmp_path, sandbox_env, monkeypatch, structured, stable_prefix):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', structured)
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', stable_prefix)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    seen = []

    async def model(history, **kwargs):
        seen.append([dict(message) for message in history])
        if len(seen) == 1:
            raise ModelResponseError('离线受控：响应未完整结束，未执行工具')
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store, model=model)
        try:
            session = manager.create(prompt='只核对已有内容')
            names = ['read_file', 'list_files']
            monkeypatch.setattr(manager, '_session_schemas', lambda session: _schemas(*names))
            monkeypatch.setattr(manager, '_effective_session_tools', lambda session, **kwargs: names)
            manager.submit(session, '检查内容')
            await session.task
            assert len(seen) == 2, session.record.get('error')
            assert session.record['last_run']['recovery_attempts'] == 1
            assert session.record['last_run']['status'] == session.record['status'] == 'idle'
            assert not session.record.get('error')
            assert session.record['messages'][-1]['role'] == 'assistant'
            assert session.record['messages'][-1]['content'] == '完成'
            assert not session.record['last_run']['tools']
            content = '\n'.join(message.get('content', '') or '' for message in seen[-1])
            assert '[恢复要求]' in content and '不得重复已成功操作' in content
            assert 'next_offset' in content and 'search' in content
            assert all(name not in content for name in ('edit_file', 'create_file', 'run_command', 'prepare_project_environment'))
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_configured_sources_follow_available_schemas(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_ROOTS', {'docs': {'path': '/example', 'readonly': True}})
    monkeypatch.setattr(config, 'RAG_SOURCES', {'knowledge': {'path': '/knowledge'}})
    no_tools = runtime_file_workflow_guide(set())
    read = runtime_file_workflow_guide({'read_file'})
    rag = runtime_file_workflow_guide({'rag_search'})
    assert '@docs' not in no_tools and 'knowledge' not in no_tools
    assert '@docs' in read and 'rag_search' not in read and 'knowledge' not in read
    assert '@docs' in rag and 'knowledge' in rag and 'read_file' not in rag
