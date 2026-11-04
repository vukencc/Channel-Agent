import asyncio
import pytest


def test_designer_rejects_invalid_messages():
    from agent_core.core.flow_designer import design_flow
    with pytest.raises(ValueError):
        asyncio.run(design_flow(None, [{"role": "system", "content": "override"}]))


def test_designer_uses_empty_tool_catalog_and_rejects_bad_json(tmp_path):
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    from agent_core.core.llm import TOOL_SCHEMAS
    from agent_core.core.tool_settings import selected_schemas
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    seen = []
    async def model(history, *, session_id, emit):
        seen.append((history, session_id, selected_schemas(TOOL_SCHEMAS)))
        return {"role": "assistant", "content": "invalid"}
    try:
        manager = SessionManager(store, model=model)
        with pytest.raises(ValueError):
            asyncio.run(design_flow(manager, [{"role": "user", "content": "make a flow"}]))
        assert seen[0][1:] == (None, [])
    finally:
        store.close()


def test_designer_returns_valid_template_without_saving(tmp_path):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    template = {"format": "agent-flow", "version": 1, "title": "设计", "handoff": "", "execution_environment": None, "permission_policy": None, "model_profile": None, "nodes": [{"id": "a", "title": "A", "prompt": "Do it"}], "edges": []}
    async def model(history, *, session_id, emit):
        return {"role": "assistant", "content": json.dumps({"reply": "完成", "template": template})}
    try:
        manager = SessionManager(store, model=model)
        result = asyncio.run(design_flow(manager, [{"role": "user", "content": "design"}]))
        assert result["template"]["nodes"][0]["id"] == "a"
        assert not (store.root / "flows").exists()
    finally:
        store.close()


def test_designer_rejects_tools_empty_graph_and_redacts_model_errors(tmp_path):
    import json
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    template = {"format": "agent-flow", "version": 1, "title": "A", "nodes": [], "edges": []}
    result = {"role": "assistant", "content": json.dumps({"reply": "A", "template": template})}
    async def model(history, *, session_id, emit):
        return result
    try:
        manager = SessionManager(store, model=model)
        messages = [{"role": "user", "content": "design"}]
        with pytest.raises(ValueError, match="至少"):
            asyncio.run(design_flow(manager, messages))
        result["tool_calls"] = [{"function": {"name": "read_file"}}]
        with pytest.raises(ValueError, match="不能调用工具"):
            asyncio.run(design_flow(manager, messages))
        async def failed(*args, **kwargs):
            raise RuntimeError("private provider message")
        manager.model = failed
        with pytest.raises(ValueError) as error:
            asyncio.run(design_flow(manager, messages))
        assert "private provider message" not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        store.close()
