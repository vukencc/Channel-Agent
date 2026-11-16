import asyncio

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.prompts import default_prompt, rag_protocol
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize('structured', [False, True])
@pytest.mark.parametrize('rag_enabled', [False, True])
def test_flow_role_receives_rag_protocol_only_with_effective_capability(
        tmp_path, monkeypatch, sandbox_env, structured, rag_enabled):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', structured)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_RETRY_LIMIT', 4)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 16000)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    project = tmp_path / 'project'
    project.mkdir()
    seen = []

    async def model(history, *, session_id, emit):
        seen.append(history)
        return {'role': 'assistant', 'content': '完成'}

    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        try:
            flow = service.create(workspace_path=str(project))
            names = ['read_file', 'list_files'] + (['rag_search'] if rag_enabled else [])
            flow['nodes'] = [{'id': 'a', 'title': '节点', 'prompt': '自定义角色职责', 'tool_names': names}]
            flow['edges'] = []
            flow = service.save(flow)
            service.start(flow['id'])
            await service.tasks[flow['id']]
            assert service.get(flow['id'])['run']['status'] == 'completed'
            system = next(m['content'] for m in seen[0] if m['role'] == 'system')
            assert '自定义角色职责' in system
            assert ('同一问题最多重试 4 次' in system) == rag_enabled
            if rag_enabled:
                assert 'strict → normal → loose' in system and '无法确认' in system
                assert '明显无关' in system and '参考数据' in system and '继续既定交接' in system
            assert config.MODEL_INPUT_TOKENS == 16000
        finally:
            await service.shutdown()
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize('structured', [False, True])
def test_classic_prompt_does_not_duplicate_rag_protocol(tmp_path, monkeypatch, sandbox_env, structured):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', structured)
    monkeypatch.setattr(config, 'RAG_RETRY_LIMIT', 4)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    seen = []
    async def model(history, **kwargs):
        seen.extend(history)
        return {'role': 'assistant', 'content': '完成'}
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store, model=model)
        try:
            session = manager.create(prompt=default_prompt())
            manager.set_tool_names(session, ['rag_search'])
            manager.submit(session, '测试')
            await session.task
            system = '\n'.join(m['content'] for m in seen if m['role'] == 'system')
            assert system.count(rag_protocol()) == 1
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_runtime_rag_protocol_adds_only_missing_parts():
    from agent_core.core.prompts import rag_runtime_protocol

    base = default_prompt()
    missing = rag_runtime_protocol(base)
    assert rag_protocol() not in missing
    assert '明显无关' in missing and '参考数据' in missing
    assert rag_runtime_protocol(base + '\n' + missing) == ''
    assert rag_runtime_protocol(missing) == rag_protocol()


@pytest.mark.parametrize("structured", [False, True])
def test_each_node_receives_its_scope_after_handoff(tmp_path, monkeypatch, structured):
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    seen = []

    async def model(history, *, session_id, emit):
        seen.append(history)
        return {"role": "assistant", "content": "第一阶段实际输出" if len(seen) == 1 else "第二阶段完成"}

    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project), handoff="总体背景")
        flow["nodes"] = [
            {"id": "a", "title": "需求", "prompt": "只写规格"},
            {"id": "b", "title": "实现", "prompt": "只实现前端"},
        ]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        assert service.get(flow["id"])["run"]["status"] == "completed"
        for history, title, prompt in zip(seen, ("需求", "实现"), ("只写规格", "只实现前端")):
            assert any(prompt in m["content"] for m in history if m["role"] == "system")
            message = history[-1]["content"]
            assert prompt not in message
            assert message.index("总体背景") < message.index(title) < message.index("当前节点任务")
            assert "遵循系统消息中的当前节点任务与输出约束" in message
            assert "只执行当前节点" in message and "后续" in message
        assert "第一阶段实际输出" in seen[1][-1]["content"]
        await manager.flush()

    try:
        asyncio.run(run())
    finally:
        store.close()
