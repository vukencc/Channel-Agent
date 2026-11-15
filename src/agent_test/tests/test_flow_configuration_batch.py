"""Flow 配置合并保存的持久化边界。"""

import asyncio
from contextlib import contextmanager

import pytest

from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService


def test_configuration_batch_saves_once_and_public_setters_save(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    session = manager.create("测试")
    session.record["flow_id"] = "flow"
    saves = []
    original = manager.save
    monkeypatch.setattr(manager, "save", lambda value: (saves.append(value.id), original(value))[1])
    try:
        with manager.configuration_batch(session):
            manager.set_permission_policy(session, "readonly")
            manager.set_model_profile(session, None)
            manager.set_custom_tools(session, [])
            manager.set_tool_names(session, None)
        assert saves == [session.id]
        manager.set_permission_policy(session, None)
        assert saves == [session.id, session.id]
    finally:
        store.close()


def test_failure_flushes_partial_changes_and_resets_batch(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    session = manager.create("测试")
    other = manager.create("其他")
    saves = []
    original = manager.save
    monkeypatch.setattr(manager, "save", lambda value: (saves.append(value.id), original(value))[1])
    try:
        with pytest.raises(ValueError):
            with manager.configuration_batch(session):
                manager.set_permission_policy(session, "readonly")
                manager.set_permission_policy(other, "smart")
                with pytest.raises(RuntimeError):
                    with manager.configuration_batch(session):
                        pass
                manager.set_permission_policy(session, "invalid")
        assert saves == [other.id, session.id]
        assert store.load_record(store.directory(session.id) / "session.json")["permission_policy"] == "readonly"
        assert store.load_record(store.directory(other.id) / "session.json")["permission_policy"] == "smart"
        manager.set_permission_policy(session, None)
        assert saves == [other.id, session.id, session.id]
    finally:
        store.close()


def test_flow_reused_node_configuration_saves_once(tmp_path, monkeypatch):
    async def run():
        async def model(history, *, session_id, emit):
            return {"role": "assistant", "content": "完成"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        projects = ProjectService(manager, service)
        original_batch, original_save = manager.configuration_batch, manager.save
        saves, counts = [], []
        monkeypatch.setattr(manager, "save", lambda session: (saves.append(session.id), original_save(session))[1])

        @contextmanager
        def observed_batch(session):
            before = len(saves)
            with original_batch(session):
                yield
            counts.append((session.id, len(saves) - before))

        monkeypatch.setattr(manager, "configuration_batch", observed_batch)
        try:
            project = projects.create("项目", str(project_path))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "one", "title": "节点", "prompt": "执行", "reuse_session": True}]
            flow["edges"] = []
            flow = service.save(flow)
            for text in ("甲", "乙"):
                service.start(flow["id"], input_text=text)
                await asyncio.wait_for(service.tasks[flow["id"]], 5)
                assert service.get(flow["id"])["run"]["status"] == "completed"
            assert len(counts) == 2
            assert counts[0][0] == counts[1][0]
            assert [count for _, count in counts] == [1, 1]
        finally:
            await service.shutdown()
            await manager.shutdown()
            store.close()

    asyncio.run(run())
