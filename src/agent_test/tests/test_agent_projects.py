import asyncio
import json
from pathlib import Path

import pytest

from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.session_service import open_session
from agent_core.core.storage import SessionStore


def test_import_is_independent_and_workspace_is_authoritative(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        template = flows.create(workspace_path="/obsolete")
        template["nodes"] = [{"id": "a", "title": "A", "prompt": "do", "workspace_path": "/obsolete-node"}]
        template = flows.save(template)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        first = projects.create("first", str(workspace), flow_id=template["id"])
        second = projects.create("second", None, flow_id=template["id"])
        instance = flows.get(first["flow_id"])
        assert instance["project_id"] == first["id"]
        assert instance["workspace_path"] is None and instance["nodes"][0]["workspace_path"] is None
        instance["nodes"][0]["title"] = "changed"
        instance = flows.save(instance)
        assert flows.get(template["id"])["nodes"][0]["title"] == "A"
        assert flows.get(second["flow_id"])["nodes"][0]["title"] == "A"
        instance["project_id"] = second["id"]
        with pytest.raises(ValueError):
            flows.save(instance)
        async def run():
            with pytest.raises(ValueError, match="项目"):
                flows.start(second["flow_id"])
            started = flows.start(first["flow_id"])
            assert started["run"]["nodes"]["a"]["workspace_path"] == str(workspace)
            flows.cancel(first["flow_id"])
            await flows.tasks[first["flow_id"]]
        asyncio.run(run())
        assert ProjectService(manager, FlowService(manager)).get(first["id"])["flow_id"] == first["flow_id"]
    finally:
        store.close()


def test_invalid_import_leaves_no_project_or_instance(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        template = flows.create(model_profile="missing-profile")
        with pytest.raises(ValueError, match="profile"):
            projects.create("bad", None, flow_id=template["id"])
        assert projects.list() == []
        assert [item["id"] for item in flows.list()] == [template["id"]]
        assert list(projects.directory.glob("*.json")) == []
    finally:
        store.close()


def test_project_flow_rejects_workspace_override_and_identity_change(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        project = projects.create("test", None)
        flow = flows.get(project["flow_id"])
        flow["workspace_path"] = str(tmp_path)
        with pytest.raises(ValueError, match="工作区"):
            flows.save(flow)
        flow["workspace_path"] = None
        flow["project_id"] = None
        with pytest.raises(ValueError, match="所属项目"):
            flows.save(flow)
        project["flow_id"] = "a" * 32
        with pytest.raises(ValueError, match="身份"):
            projects.save(project)
    finally:
        store.close()


def test_project_workspace_identity_and_reservations(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        project = projects.create("test", str(old))
        old_identity = manager.workspaces.validate_path(str(old))[1]
        new_identity = manager.workspaces.validate_path(str(new))[1]
        for identity in (old_identity, new_identity):
            manager.project_reservations[identity] = project["flow_id"]
            with pytest.raises(ValueError, match="预约"):
                projects.update(project["id"], workspace_path=str(new))
            if identity == old_identity:
                with pytest.raises(ValueError, match="预约"):
                    projects.delete(project["id"])
            del manager.project_reservations[identity]
        old.rename(tmp_path / "replaced")
        old.mkdir()
        with pytest.raises(PermissionError, match="身份"):
            projects.update(project["id"], title="changed")
        with pytest.raises(PermissionError, match="身份"):
            projects.delete(project["id"])
    finally:
        store.close()


def test_failed_project_create_and_external_change_preserve_flow(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        original_write = store.atomic_write
        def fail_project(path, text):
            if path.parent == projects.directory:
                raise OSError("controlled failure")
            return original_write(path, text)
        monkeypatch.setattr(store, "atomic_write", fail_project)
        with pytest.raises(OSError, match="controlled"):
            projects.create("test", None)
        assert flows.list() == [] and list(flows.directory.glob("*.json")) == []
        monkeypatch.setattr(store, "atomic_write", original_write)
        project = projects.create("test", None)
        path = projects._path(project["id"])
        changed = json.loads(path.read_text())
        changed["title"] = "external"
        path.write_text(json.dumps(changed))
        with pytest.raises(PermissionError, match="变化"):
            projects.delete(project["id"])
        assert json.loads(flows._path(project["flow_id"]).read_text())["project_id"] == project["id"]
        assert json.loads(path.read_text())["title"] == "external"
    finally:
        store.close()


def test_delete_failure_restores_flow_and_busy_session_blocks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        project = projects.create("test", str(workspace))
        session = open_session(manager, workspace_path=str(workspace))
        session.tool_workers.add("controlled-worker")
        with pytest.raises(ValueError, match="活动"):
            projects.delete(project["id"])
        session.tool_workers.clear()
        project_path = projects._path(project["id"])
        original_unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == project_path:
                raise OSError("controlled unlink failure")
            return original_unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(OSError, match="controlled unlink"):
            projects.delete(project["id"])
        assert projects.get(project["id"]) == project
        assert flows.get(project["flow_id"])["project_id"] == project["id"]
        reloaded_flows = FlowService(manager)
        ProjectService(manager, reloaded_flows)
        assert reloaded_flows.get(project["flow_id"])["project_id"] == project["id"]
    finally:
        store.close()


def test_project_run_inherits_template_settings_without_mutation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        monkeypatch.setattr(manager.model_registry, "contains", lambda value: value == "template-model")
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        template = flows.create(handoff="template background", model_profile="template-model")
        template["nodes"] = [{"id": "a", "title": "A", "prompt": "do"}]
        template = flows.save(template)
        project = projects.create("test", str(workspace), template["id"], handoff="project background")
        async def run():
            for _ in range(2):
                result = flows.start(project["flow_id"])
                assert result["handoff"] == "template background"
                assert result["model_profile"] == "template-model"
                assert result["run"]["execution_settings"]["handoff"] == "template background\n\nproject background"
                assert result["run"]["execution_settings"]["model_profile"] == "template-model"
                flows.cancel(project["flow_id"])
                await flows.tasks[project["flow_id"]]
            assert flows.get(project["flow_id"])["handoff"] == "template background"
        asyncio.run(run())
    finally:
        store.close()


def test_broken_associations_are_isolated_on_reload(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        good = projects.create("good", None)
        missing_flow = projects.create("missing flow", None)
        missing_project = projects.create("missing project", None)
        corrupt = projects.create("corrupt", None)
        flows._path(missing_flow["flow_id"]).unlink()
        projects._path(missing_project["id"]).unlink()
        projects._path(corrupt["id"]).write_text("broken")
        loaded_flows = FlowService(manager)
        loaded_projects = ProjectService(manager, loaded_flows)
        assert [p["id"] for p in loaded_projects.list()] == [good["id"]]
        assert [f["id"] for f in loaded_flows.list()] == [good["flow_id"]]
        assert loaded_projects.errors
        for identifier in (missing_flow["id"], missing_project["id"], corrupt["id"]):
            with pytest.raises((KeyError, ValueError, PermissionError)):
                loaded_projects.get(identifier)
        for identifier in (missing_flow["flow_id"], missing_project["flow_id"], corrupt["flow_id"]):
            with pytest.raises((KeyError, ValueError, PermissionError)):
                loaded_flows.get(identifier)
    finally:
        store.close()


def test_unrun_or_orphaned_project_workspace_is_protected_from_cleanup(tmp_path, monkeypatch):
    from agent_core import config
    from agent_core.core.maintenance import inspect_cache

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sentinel = workspace / "keep.txt"
    sentinel.write_text("user data")
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        project = projects.create("test", str(workspace))
        monkeypatch.setattr(config, "RAG_CACHE_DIR", workspace)
        with pytest.raises(PermissionError):
            inspect_cache(manager, remove=True)
        flows._path(project["flow_id"]).unlink()
        restored = SessionManager(store)
        with pytest.raises(PermissionError):
            inspect_cache(restored, remove=True)
        assert sentinel.read_text() == "user data"
    finally:
        store.close()
