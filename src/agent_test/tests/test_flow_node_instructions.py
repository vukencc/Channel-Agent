import asyncio

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


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
            assert message.index("总体背景") < message.index(title) < message.index(prompt)
            assert "只执行当前节点" in message and "后续" in message
        assert "第一阶段实际输出" in seen[1][-1]["content"]
        await manager.flush()

    try:
        asyncio.run(run())
    finally:
        store.close()
