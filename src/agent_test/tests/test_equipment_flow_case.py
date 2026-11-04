"""业务用例的导入边界；不伪造或调用模型。"""
from pathlib import Path

import pytest

from agent_core.core.flows import Definition, FlowService, _order
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_test.scripts.equipment_flow_case import create_case, load_case


def test_case_has_five_ordered_roles_and_no_baked_workspace():
    case = load_case()
    definition = Definition(id="a" * 32, revision=0,
                            **{key: value for key, value in case.items()
                               if key not in {"schema_version", "case_id"}}).model_dump()
    order, _ = _order(definition)
    assert order == ["requirements", "backend", "frontend", "quality", "delivery"]
    assert definition["workspace_path"] is None
    assert all(node["workspace_path"] is None for node in definition["nodes"])
    assert "SQLite" in definition["handoff"]
    assert "409" in definition["handoff"] and "并发" in definition["handoff"]


def test_case_import_keeps_files_and_creates_independent_project(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "work")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        workspace = tmp_path / "project"
        workspace.mkdir()
        existing = workspace / "existing.txt"
        existing.write_text("保留原有项目文件", encoding="utf-8")
        ids = create_case(flows, projects, workspace)
        assert existing.read_text(encoding="utf-8") == "保留原有项目文件"
        assert list(workspace.iterdir()) == [existing]
        project = projects.get(ids["project"])
        template = flows.get(ids["template"])
        instance = flows.get(ids["flow"])
        assert project["workspace_path"] == str(workspace)
        assert template["project_id"] is None and template["workspace_path"] is None
        assert instance["project_id"] == project["id"] and instance["id"] != template["id"]
        assert instance["nodes"] == template["nodes"] and len(instance["nodes"]) == 5
        assert instance["run"] is None and manager.sessions == {}
        instance["nodes"][0]["prompt"] = "项目自己的修改"
        flows.save(instance)
        assert flows.get(ids["template"])["nodes"][0]["prompt"] != "项目自己的修改"
    finally:
        store.close()


def test_invalid_workspace_does_not_leave_imported_records(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "work")
    try:
        manager = SessionManager(store)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        with pytest.raises(PermissionError):
            create_case(flows, projects, Path(tmp_path / "missing"))
        assert projects.list() == [] and flows.list() == []
    finally:
        store.close()
