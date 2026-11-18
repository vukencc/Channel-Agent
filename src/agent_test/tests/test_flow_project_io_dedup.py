"""同一操作中的路径探测及序列化次数。"""
from pathlib import Path

import pytest

from agent_core.core import flows as flows_module
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture
def services(tmp_path):
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    manager = SessionManager(store)
    flows = FlowService(manager)
    projects = ProjectService(manager, flows)
    project = projects.create("项目", None)
    yield flows, projects, project
    store.close()


def test_link_uses_single_flow_path_and_missing_is_invalid(services, monkeypatch):
    flows, projects, project = services
    original = flows._path
    calls = []
    def counted(identifier):
        calls.append(identifier)
        return original(identifier)
    monkeypatch.setattr(flows, "_path", counted)
    projects._check_link(project["id"], project["flow_id"])
    assert calls == [project["flow_id"]]
    original(project["flow_id"]).unlink()
    with pytest.raises(ValueError, match="关联无效"):
        projects._check_link(project["id"], project["flow_id"])


@pytest.mark.parametrize("kind", ["flow", "project"])
def test_write_has_one_existence_probe_and_flow_serializes_once(services, monkeypatch, kind):
    flows, projects, project = services
    service = flows if kind == "flow" else projects
    identifier = project["flow_id"] if kind == "flow" else project["id"]
    value = service.records[identifier].copy()
    path = service._path(identifier)
    exists = Path.exists
    probes = []
    def counted_exists(candidate):
        if candidate == path:
            probes.append(candidate)
        return exists(candidate)
    monkeypatch.setattr(Path, "exists", counted_exists)
    dumps = flows_module.json.dumps
    serializations = []
    def counted_dumps(*args, **kwargs):
        serializations.append(args[0])
        return dumps(*args, **kwargs)
    monkeypatch.setattr(flows_module.json, "dumps", counted_dumps)
    service._write(value)
    assert not probes
    if kind == "flow":
        assert len(serializations) == 1


@pytest.mark.parametrize("kind", ["flow", "project"])
def test_write_rejects_deleted_and_modified_records(services, kind):
    flows, projects, project = services
    service = flows if kind == "flow" else projects
    identifier = project["flow_id"] if kind == "flow" else project["id"]
    value = service.records[identifier].copy()
    path = service._path(identifier)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        service._write(value)
    path.unlink()
    with pytest.raises(PermissionError, match="外部删除"):
        service._write(value)
    assert not path.exists()


def test_link_propagates_permission_and_parse_errors(services, monkeypatch):
    flows, projects, project = services
    def denied(identifier):
        raise PermissionError("拒绝")
    monkeypatch.setattr(flows, "_verify_link_file", denied)
    with pytest.raises(PermissionError, match="拒绝"):
        projects._check_link(project["id"], project["flow_id"])
    monkeypatch.undo()
    flows._path(project["flow_id"]).write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        projects._check_link(project["id"], project["flow_id"])


def test_project_write_propagates_read_missing_error_when_file_exists(services, monkeypatch):
    _, projects, project = services
    def fail(identifier):
        raise FileNotFoundError("读取失败")
    monkeypatch.setattr(projects, "_read", fail)
    with pytest.raises(FileNotFoundError, match="读取失败"):
        projects._write(project)
