import pytest
from agent_core.core.flows import Node


def test_node_accepts_configuration():
    node = Node(id="a", title="A", prompt="task", tool_names=[], reasoning_effort="high", thinking_mode="disabled")
    assert node.tool_names == []


def test_designer_catalog_available():
    from agent_core.core.flow_templates import flow_tool_catalog
    assert set(flow_tool_catalog()) == {"tools", "required_tool_names", "reasoning_efforts", "thinking_modes"}


def test_template_round_trip_and_invalid_import_is_atomic(tmp_path):
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        manager = SessionManager(store)
        service = FlowService(manager)
        flow = service.create(title="Example", workspace_path="/private")
        flow["nodes"] = [{"id": "a", "title": "A", "prompt": "Task", "workspace_path": "/private", "handoff_file": "notes/a.txt"}]
        flow = service.save(flow)
        payload = service.export_template(flow)
        assert "workspace_path" not in payload and "workspace_path" not in payload["nodes"][0]
        imported = service.import_template(payload)
        assert imported["id"] != flow["id"] and imported["workspace_path"] is None
        before = set(service.records)
        payload["nodes"][0]["handoff_file"] = "../secret"
        with pytest.raises(ValueError):
            service.import_template(payload)
        assert set(service.records) == before
    finally:
        store.close()


def test_template_rejects_cycle_unknown_models_and_inherit_is_explicit(tmp_path):
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    try:
        service = FlowService(SessionManager(store))
        flow = service.create()
        payload = service.export_template(flow)
        payload["nodes"] = [{"id": "a", "title": "A", "prompt": "A"}, {"id": "b", "title": "B", "prompt": "B"}]
        payload["edges"] = [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}]
        before = set(service.records)
        with pytest.raises(ValueError, match="环"):
            service.import_template(payload)
        assert set(service.records) == before
        payload["edges"] = []
        payload["nodes"][0]["model_profile"] = "missing-profile"
        with pytest.raises(ValueError, match="profile"):
            service.import_template(payload)
        imported = service.import_template(payload, missing_models="inherit")
        assert imported["nodes"][0]["model_profile"] is None
    finally:
        store.close()


def test_portable_schema_has_no_workspace_and_strict_version():
    from agent_core.core.flow_templates import PortableTemplate
    schema = PortableTemplate.model_json_schema()
    assert "workspace_path" not in schema["$defs"]["PortableNode"]["properties"]
    assert schema["properties"]["format"]["const"] == "agent-flow"
    assert schema["properties"]["version"]["const"] == 1
    with pytest.raises(ValueError):
        PortableTemplate.model_validate({"format": "agent-flow", "version": True, "title": "A", "nodes": [], "edges": []})
