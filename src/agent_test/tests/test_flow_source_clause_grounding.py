"""旧 Flow 的运行请求需要保留逐条来源和缺口的表达边界。"""
import asyncio
import json

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_observations import observe_text, validate_observation


@pytest.mark.parametrize("structured", [False, True])
def test_flow_model_entry_keeps_clause_and_missing_field_guidance(tmp_path, monkeypatch, structured):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    project = tmp_path / "project"
    project.mkdir()
    seen = []
    body = "[1] 来源: policy-a\n甲动作：成员可申请。\n\n[2] 来源: policy-b\n乙动作：管理员可申请。"
    a = body.index("甲动作")
    b = body.index("乙动作")
    async def model(history, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {"name": "rag_search", "arguments": json.dumps({"query": "操作", "item_ref": "case"})}}]}
        return {"role": "assistant", "content": "完成"}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        async def tool(session, call, *, evidence=None):
            evidence["source"] = "executed"
            return observe_text(body, reader_tool="rag_search", body_spans=((a, a + len("甲动作：成员可申请。")), (b, len(body))), recovery_notice="重新检索")
        manager._tool = tool
        try:
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "旧模板", "prompt": "处理并交接", "tool_names": ["rag_search"]}]
            flow["edges"] = []
            flow = service.save(flow)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            assert service.get(flow["id"])["run"]["status"] == "completed"
            system = "\n".join(m.get("content", "") or "" for m in seen[1] if m["role"] == "system")
            assert "同一来源条款" in system and "具体动作" in system
            assert "来源短原文" in system
            assert "原述未记录" in system and "资料未见" in system and "调查建议" in system
            assert "写操作参数" in system and "交接" in system
            result = next(m["content"] for m in seen[1] if m["role"] == "tool")
            assert body in result and "逐条核对" in result and "不可跨片段" in result
            saved = next(m for s in manager.sessions.values() for m in s.record["messages"] if m.get("tool_call_id") == "r")
            observation = validate_observation(saved["content"], saved["_context_observation"])
            assert observation is not None and observation.body_spans == ((a, a + len("甲动作：成员可申请。")), (b, len(body)))
        finally:
            await service.shutdown()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
