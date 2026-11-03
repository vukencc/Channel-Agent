"""真实Flow/会话/工具执行链路，模型回调受控，不代表provider测评。"""
import asyncio
import json

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import model_options, selected_model
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import selected_schemas
from agent_core.tools import all_schemas


def test_flow_node_settings_reach_requests_and_disabled_command_is_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "MODEL_PROFILES", {"preset": {"model": "preset-model", "parameters": {}}})
    monkeypatch.setattr(config, "MODEL_PARAMETERS", {})
    monkeypatch.setattr(config, "THINKING_MODE", "auto")
    monkeypatch.setattr(config, "REASONING_EFFORT", "")
    workspace = tmp_path / "selected"
    workspace.mkdir()
    store = SessionStore(tmp_path / "state", tmp_path / "defaults")
    seen = []

    async def run():
        async def controlled_model(history, *, session_id, emit):
            node = manager.sessions[session_id].record["flow_node_id"]
            catalog = {row["function"]["name"] for row in selected_schemas(all_schemas())}
            seen.append((node, selected_model(), model_options(), catalog))
            if node == "first" and not any(item["role"] == "tool" for item in history):
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "forbidden-command", "type": "function", "function": {
                        "name": "run_command", "arguments": json.dumps({"command": "touch SHOULD_NOT_EXIST"}),
                    },
                }]}
            if node == "first":
                result = next(item["content"] for item in history if item["role"] == "tool")
                assert "拒绝" in result and "工具子集" in result
            return {"role": "assistant", "content": "当前阶段完成，交接给下一节点。"}

        manager = SessionManager(store, model=controlled_model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        profile = manager.model_registry.add(name="节点模型", model="runtime-choice", base_url="http://127.0.0.1:9/v1")
        try:
            template = flows.create(title="配置执行验证", model_profile="preset")
            template.update(nodes=[
                {"id": "first", "title": "第一阶段", "prompt": "只读取和交接", "tool_names": [],
                 "model_profile": profile["id"], "thinking_mode": "enabled", "reasoning_effort": "high"},
                {"id": "second", "title": "第二阶段", "prompt": "读取上一阶段", "tool_names": ["rag_search"],
                 "thinking_mode": "disabled", "reasoning_effort": "high"},
            ], edges=[{"source": "first", "target": "second"}])
            template = flows.save(template)
            project = projects.create(title="验证项目", workspace_path=str(workspace), flow_id=template["id"])
            flows.start(project["flow_id"])
            await flows.tasks[project["flow_id"]]
            assert flows.get(project["flow_id"])["run"]["status"] == "completed"
            assert not (workspace / "SHOULD_NOT_EXIST").exists()
            first = [row for row in seen if row[0] == "first"]
            second = [row for row in seen if row[0] == "second"]
            assert len(first) == 2 and len(second) == 1
            assert all(row[1] == "runtime-choice" and row[2]["reasoning_effort"] == "high"
                       and row[2]["extra_body"]["thinking"]["type"] == "enabled"
                       and row[3] == {"read_file", "list_files"} for row in first)
            assert second[0][1] == "preset-model"
            assert "reasoning_effort" not in second[0][2]
            assert second[0][2]["extra_body"]["thinking"]["type"] == "disabled"
            assert second[0][3] == {"read_file", "list_files", "rag_search"}
        finally:
            await flows.shutdown()
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()
