import asyncio

import pytest

from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_save_reload_revision_and_graph(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        service = FlowService(SessionManager(store))
        flow = service.create()
        old = dict(flow)
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "do", "x": 0, "y": 0}]
        saved = service.save(flow)
        assert FlowService(service.manager).get(flow["id"])["nodes"][0]["id"] == "a"
        with pytest.raises(ValueError):
            service.save(old)
        saved["edges"] = [{"source": "a", "target": "a"}]
        with pytest.raises(ValueError):
            service.save(saved)
    finally:
        store.close()


def test_handoff_reaches_next_node(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "handoff.txt").write_text("文件内容", encoding="utf-8")
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    seen = []
    async def model(history, *, session_id, emit):
        seen.append(history[-1]["content"])
        return {"role": "assistant", "content": "完成总结"}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [
            {"id": "a", "title": "A", "prompt": "任务 A", "x": 0, "y": 0, "handoff_file": "handoff.txt"},
            {"id": "b", "title": "B", "prompt": "任务 B", "x": 1, "y": 0},
        ]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "completed"
        assert "文件内容" in seen[0]
        assert "首次接触" in seen[0] and "先阅读" in seen[0]
        assert "完成总结" in seen[1]
        await manager.flush()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_corrupt_and_symlink_records_are_not_overwritten(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        service = FlowService(SessionManager(store))
        flow = service.create()
        path = service.directory / (flow["id"] + ".json")
        path.write_text("broken", encoding="utf-8")
        with pytest.raises(ValueError):
            service.save(flow)
        assert path.read_text() == "broken"
        path.unlink()
        target = tmp_path / "target"
        target.write_text("untouched")
        path.symlink_to(target)
        with pytest.raises(PermissionError):
            service.save(flow)
        assert target.read_text() == "untouched"
    finally:
        store.close()


def test_cancel_blocks_downstream_and_releases_reservation(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        started = asyncio.Event()
        async def model(history, *, session_id, emit):
            started.set()
            await asyncio.Event().wait()
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": key, "title": key, "prompt": key} for key in ("a", "b")]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await asyncio.wait_for(started.wait(), 2)
        service.cancel(flow["id"])
        await asyncio.wait_for(service.tasks[flow["id"]], 2)
        result = service.get(flow["id"])["run"]
        assert result["status"] == "cancelled"
        assert result["nodes"]["b"]["status"] == "blocked"
        assert not result["nodes"]["a"]["output_handoff"]
        assert not manager.project_reservations
        await manager.flush()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_failure_does_not_pass_handoff(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    calls = []
    async def model(history, *, session_id, emit):
        calls.append(session_id)
        raise RuntimeError("controlled failure")
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": key, "title": key, "prompt": key} for key in ("a", "b")]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "failed"
        assert result["nodes"]["a"]["status"] == "failed"
        assert result["nodes"]["a"]["output_handoff"] == ""
        assert result["nodes"]["b"]["status"] == "blocked"
        assert len(calls) == 1
        await manager.flush()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_cycle_nonfinite_and_project_conflict(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    try:
        service = FlowService(SessionManager(store))
        flow = service.create(workspace_path=str(first))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"},
                         {"id": "b", "title": "B", "prompt": "B", "workspace_path": str(second)}]
        flow["edges"] = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
        with pytest.raises(ValueError):
            service.save(flow)
        flow["edges"].pop()
        flow["nodes"][0]["x"] = float("nan")
        with pytest.raises(ValueError):
            service.save(flow)
        flow["nodes"][0]["x"] = 0
        flow = service.save(flow)
        async def start():
            with pytest.raises(ValueError, match="冲突"):
                service.start(flow["id"])
        asyncio.run(start())
        assert not service.manager.project_reservations
    finally:
        store.close()


def test_failed_start_does_not_save_definition_or_revision(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        service = FlowService(SessionManager(store))
        flow = service.create()
        draft = dict(flow)
        draft["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
        async def start():
            with pytest.raises(ValueError, match="项目"):
                service.start(flow["id"], draft)
        asyncio.run(start())
        assert service.get(flow["id"]) == flow
        assert FlowService(service.manager).get(flow["id"]) == flow
        assert service.save(draft)["revision"] == flow["revision"] + 1
    finally:
        store.close()


def test_checkpoint_and_concurrent_reservation(tmp_path, monkeypatch):
    from agent_core import config
    from agent_core.core.llm import ModelConnectionError
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def model(history, *, session_id, emit):
        raise ModelConnectionError("controlled connection failure")
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flows = []
        for _ in range(2):
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}, {"id": "b", "title": "B", "prompt": "B"}]
            flow["edges"] = [{"source": "a", "target": "b"}]
            flows.append(service.save(flow))
        service.start(flows[0]["id"])
        with pytest.raises(ValueError, match="预约"):
            service.start(flows[1]["id"])
        await service.tasks[flows[0]["id"]]
        result = service.get(flows[0]["id"])["run"]
        assert result["status"] == "failed"
        assert result["nodes"]["a"]["output_handoff"] == ""
        assert result["nodes"]["b"]["status"] == "blocked"
        assert manager.sessions[result["nodes"]["a"]["session_id"]].record["status"] == "checkpoint"
        assert not manager.project_reservations
        await manager.flush()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_handoff_symlink_and_invalid_ids_are_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "outside"
    secret.write_text("outside")
    (project / "link").symlink_to(secret)
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    calls = []
    async def model(history, *, session_id, emit):
        calls.append(session_id)
        return {"role": "assistant", "content": "unexpected"}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        with pytest.raises(ValueError):
            service.get("../outside")
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "../bad", "title": "A", "prompt": "A"}]
        with pytest.raises(ValueError):
            service.save(flow)
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A", "handoff_file": "link"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "failed"
        assert calls == []
        await manager.flush()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_real_core_read_tool_finishes_before_handoff(tmp_path, sandbox_env, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "ENABLE_TASK_PLANS", False)
    project = tmp_path / "project"
    project.mkdir()
    (project / "input.txt").write_text("受控工具内容", encoding="utf-8")
    store = SessionStore(tmp_path / "state", workspace_root=sandbox_env)
    seen = []
    async def model(history, *, session_id, emit):
        if history[-1]["role"] == "tool":
            seen.append(history[-1]["content"])
            return {"role": "assistant", "content": "读取验证完成"}
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "function": {"name": "read_file", "arguments": "{\"path\":\"input.txt\"}"}}]}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "读取文件"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "completed"
        assert "受控工具内容" in seen[0]
        assert "读取验证完成" in result["nodes"]["a"]["output_handoff"]
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_different_projects_run_concurrently_and_unknown_profile_is_atomic(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        entered = set()
        both = asyncio.Event()
        async def model(history, *, session_id, emit):
            entered.add(session_id)
            if len(entered) == 2:
                both.set()
            await both.wait()
            return {"role": "assistant", "content": "完成"}
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flows = []
        for project in (first, second):
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
            flows.append(service.save(flow))
        draft = dict(flows[0], model_profile="unknown-profile")
        with pytest.raises(ValueError, match="profile"):
            service.start(flows[0]["id"], draft)
        assert service.get(flows[0]["id"]) == flows[0]
        service.start(flows[0]["id"])
        service.start(flows[1]["id"])
        try:
            await asyncio.wait_for(both.wait(), 1)
        finally:
            await service.shutdown()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_turn_drains_real_background_job_before_release(tmp_path, monkeypatch, cancel):
    import threading
    from agent_core import config
    from agent_core.core import command_jobs
    from agent_core.tools import command
    from agent_core.tools.sandbox import ToolContext, tool_context
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "ENABLE_COMMAND_JOBS", True)
    monkeypatch.setattr(command_jobs, "isolated_command", lambda argv, **kwargs: argv)
    entered, release = threading.Event(), threading.Event()
    def execute(*args, **kwargs):
        entered.set()
        if not release.wait(4):
            raise TimeoutError("test did not release job")
        return "[退出码] 0"
    monkeypatch.setattr(command, "_execute", execute)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        manager = None
        async def model(history, *, session_id, emit):
            session = manager.sessions[session_id]
            context = ToolContext(project, store.directory(session.id) / "audit.jsonl", lambda *_: True,
                                  session.cancelled, session_id=session.id, root_identity=manager.workspaces.identity(session),
                                  job_manager=manager.command_jobs)
            with tool_context(context):
                manager.command_jobs.start("printf done", timeout=2)
            assert await asyncio.to_thread(entered.wait, 2)
            if not cancel:
                raise RuntimeError("controlled failure")
            return {"role": "assistant", "content": "done"}
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
        flow = service.save(flow)
        service.start(flow["id"])
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            if cancel:
                service.cancel(flow["id"])
            await asyncio.sleep(.1)
            assert not service.tasks[flow["id"]].done()
            assert manager.project_reservations
            assert any(event.is_set() for event in manager.command_jobs.events.values())
            release.set()
            await asyncio.wait_for(service.tasks[flow["id"]], 3)
            assert not manager.project_reservations
            assert service.get(flow["id"])["run"]["status"] == ("cancelled" if cancel else "failed")
        finally:
            release.set()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_failed_parent_cancels_and_drains_isolated_child(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        manager = None
        child_started = asyncio.Event()
        child_finished = asyncio.Event()
        async def model(history, *, session_id, emit):
            session = manager.sessions[session_id]
            if session.record.get("delegated_from"):
                child_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(.05)
                    child_finished.set()
            child = manager.create("child", parent=session, workspace_mode="isolated")
            manager.submit(child, "child task")
            await child_started.wait()
            raise RuntimeError("controlled failure")
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await asyncio.wait_for(service.tasks[flow["id"]], 3)
        assert child_finished.is_set()
        assert not manager.project_reservations
        assert service.get(flow["id"])["run"]["status"] == "failed"
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_prior_node_cannot_submit_during_next_node(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        manager = None
        second_started = asyncio.Event()
        release = asyncio.Event()
        async def model(history, *, session_id, emit):
            session = manager.sessions[session_id]
            if session.record["flow_node_id"] == "b":
                second_started.set()
                await release.wait()
            return {"role": "assistant", "content": "完成"}
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": key, "title": key, "prompt": key} for key in ("a", "b")]
        flow["edges"] = [{"source": "a", "target": "b"}]
        flow = service.save(flow)
        service.start(flow["id"])
        try:
            await asyncio.wait_for(second_started.wait(), 2)
            prior_id = service.get(flow["id"])["run"]["nodes"]["a"]["session_id"]
            with pytest.raises((ValueError, PermissionError)):
                manager.submit(manager.sessions[prior_id], "冲突任务")
            release.set()
            await service.tasks[flow["id"]]
            assert not manager.project_reservation_nodes
            second_started.clear()
            release.clear()
            service.start(flow["id"])
            await asyncio.wait_for(second_started.wait(), 2)
            with pytest.raises((ValueError, PermissionError)):
                manager.submit(manager.sessions[prior_id], "前一轮冲突任务")
        finally:
            release.set()
            await service.shutdown()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_unrelated_archive_does_not_block_flow_cleanup(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def run():
        model_started, finish_model = asyncio.Event(), asyncio.Event()
        archive_waiting, finish_archive = asyncio.Event(), asyncio.Event()
        async def model(history, *, session_id, emit):
            model_started.set()
            await finish_model.wait()
            return {"role": "assistant", "content": "完成"}
        manager = SessionManager(store, model=model)
        unrelated = manager.create("unrelated")
        service = FlowService(manager)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await asyncio.wait_for(model_started.wait(), 2)
        original_flush = manager.flush
        async def held_flush():
            archive_waiting.set()
            await finish_archive.wait()
            await original_flush()
        monkeypatch.setattr(manager, "flush", held_flush)
        archive = asyncio.create_task(manager.archive_sessions([unrelated]))
        try:
            await asyncio.wait_for(archive_waiting.wait(), 2)
            assert unrelated.deleting
            finish_model.set()
            await asyncio.wait_for(service.tasks[flow["id"]], 2)
            assert service.get(flow["id"])["run"]["status"] == "completed"
            assert not manager.project_reservations
            assert not manager.project_reservation_nodes
        finally:
            finish_model.set()
            finish_archive.set()
            await archive
            monkeypatch.setattr(manager, "flush", original_flush)
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_cleanup_error_preserves_reservation_and_reports_failure(tmp_path, monkeypatch):
    from agent_core import config
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    project = tmp_path / "project"
    project.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    async def model(history, *, session_id, emit):
        return {"role": "assistant", "content": "完成"}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        async def fail_cleanup(*args, **kwargs):
            raise PermissionError("受控清理故障")
        monkeypatch.setattr(service, "_drain", fail_cleanup)
        flow = service.create(workspace_path=str(project))
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        result = service.get(flow["id"])["run"]
        assert result["status"] == "failed"
        assert "清理未完成" in result["error"]
        assert manager.project_reservations
        assert not manager.project_reservation_nodes
        assert FlowService(manager).get(flow["id"])["run"]["status"] == "failed"
        await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
