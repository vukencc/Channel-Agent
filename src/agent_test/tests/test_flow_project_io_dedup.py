"""同一操作中的路径探测及序列化次数。"""
from pathlib import Path

import pytest

from agent_core.core import flows as flows_module
from agent_core.core import projects as projects_module
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


def test_link_reuses_only_successful_validation_but_always_opens(services, monkeypatch):
    flows, projects, project = services
    identifier = project["flow_id"]
    validate = flows_module.Definition.model_validate
    opened = flows_module.opened_path
    validations, opens = [], []
    def counted_validate(value, *args, **kwargs):
        validations.append(value)
        return validate(value, *args, **kwargs)
    def counted_open(*args, **kwargs):
        opens.append(args)
        return opened(*args, **kwargs)
    monkeypatch.setattr(flows_module.Definition, "model_validate", counted_validate)
    monkeypatch.setattr(flows_module, "opened_path", counted_open)
    projects.get(project["id"])
    projects.get(project["id"])
    assert len(opens) == 2
    assert len(validations) == 1
    path = flows._path(identifier)
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        projects.get(project["id"])
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        projects.get(project["id"])
    assert len(opens) == 4
    assert len(validations) == 2


def test_project_get_reads_project_once_and_returns_isolated_copy(services, monkeypatch):
    _, projects, project = services
    opened = projects_module.opened_path
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[1])
        return opened(*args, **kwargs)
    monkeypatch.setattr(projects_module, "opened_path", counted)
    result = projects.get(project["id"])
    assert calls == [projects._path(project["id"])]
    result["title"] = "修改"
    assert projects.records[project["id"]]["title"] == project["title"]


def test_link_revalidates_each_written_revision_and_clears_on_delete(services, monkeypatch):
    flows, projects, project = services
    identifier = project["flow_id"]
    validate = flows_module.Definition.model_validate
    calls = []
    def counted(value, *args, **kwargs):
        calls.append(value)
        return validate(value, *args, **kwargs)
    monkeypatch.setattr(flows_module.Definition, "model_validate", counted)
    projects.get(project["id"])
    assert len(calls) == 1
    for revision in (1, 2):
        value = flows.records[identifier].copy()
        value["revision"] = revision
        flows._write(value)
        flows.records[identifier] = value
        calls.clear()
        projects.get(project["id"])
        projects.get(project["id"])
        assert len(calls) == 1
    projects.delete(project["id"])
    assert identifier not in flows._verified_link_identities


def test_link_rejects_valid_external_content_and_unvalidated_write(services):
    flows, projects, project = services
    identifier = project["flow_id"]
    projects.get(project["id"])
    path = flows._path(identifier)
    value = flows.records[identifier].copy()
    value["title"] = "外部修改"
    path.write_text(flows_module.json.dumps(value), encoding="utf-8")
    with pytest.raises(PermissionError, match="外部变化"):
        projects.get(project["id"])
    # 写入公布的身份不能代替对新字节的验证。
    path.unlink()
    flows.records.pop(identifier)
    flows._write({"id": identifier})
    flows.records[identifier] = value
    with pytest.raises(ValueError):
        projects.get(project["id"])

def test_linked_flow_get_checks_each_path_once(services, monkeypatch):
    flows, _, project = services
    identifier = project["flow_id"]
    path = flows._path
    checked = []

    def count(identifier):
        checked.append(identifier)
        return path(identifier)

    monkeypatch.setattr(flows, "_path", count)
    assert flows.get(identifier)["project_id"] == project["id"]
    assert checked == [identifier]
    path(identifier).unlink()
    with pytest.raises(ValueError, match="关联无效"):
        flows.get(identifier)

    for invalid in (None, [], "bad"):
        with pytest.raises(ValueError, match="无效 Flow ID"):
            flows.get(invalid)
