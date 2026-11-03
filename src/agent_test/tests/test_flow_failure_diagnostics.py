import asyncio

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import ModelConnectionError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize("unknown_reason", [False, True])
def test_connection_checkpoint_is_reported_without_provider_details(tmp_path, monkeypatch, unknown_reason):
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    secret = "provider-body-secret"

    async def model(history, *, session_id, emit):
        raise ModelConnectionError(secret)

    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        original_drain = service._drain

        async def drain(session, *args, **kwargs):
            await original_drain(session, *args, **kwargs)
            if unknown_reason:
                session.record["last_run"]["stop_reason"] = secret

        monkeypatch.setattr(service, "_drain", drain)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"},
                         {"id": "b", "title": "B", "prompt": "B"}]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "failed"
        assert result["nodes"]["a"]["status"] == "failed"
        assert result["nodes"]["b"]["status"] == "blocked"
        assert manager.sessions[result["nodes"]["a"]["session_id"]].record["status"] == "checkpoint"
        if unknown_reason:
            assert result["error"] == "RuntimeError: 节点未正常完成或工具尚未排空"
        else:
            assert "模型连接中断" in result["error"]
            assert "检查点" in result["error"]
        assert result["nodes"]["a"]["error"] == result["error"]
        assert secret not in result["error"]
        await manager.flush()

    try:
        asyncio.run(run())
    finally:
        store.close()
