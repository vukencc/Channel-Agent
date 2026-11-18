"""Flow 运行时保留来源和检索证据，不代替业务验收。"""
import asyncio
import json

import pytest

from agent_core import config
from agent_core.core.execution_guidance import work_progress_context
from agent_core.core.flows import FlowService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.mark.parametrize("structured", [False, True])
def test_existing_flow_receives_source_grounding_at_model_entry(tmp_path, monkeypatch, structured):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    project = tmp_path / "project"
    project.mkdir()
    seen = []

    async def model(history, **kwargs):
        seen.append(history)
        return {"role": "assistant", "content": "完成"}

    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        try:
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "旧节点", "prompt": "处理交接", "tool_names": ["read_file"]}]
            flow["edges"] = []
            flow = service.save(flow)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            assert service.get(flow["id"])["run"]["status"] == "completed"
            text = "\n".join(m.get("content", "") or "" for m in seen[0] if m["role"] == "system")
            assert "逐字原述" in text and "推断" in text
            assert "不得将推断写成原话" in text
            assert "未归属" in text and "不同事项" in text
        finally:
            await service.shutdown()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_query_counts_retain_status_without_assigning_business_items():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    events = []
    for i, (query, status) in enumerate((("alpha", "executed"), ("beta", "executed"), ("alpha", "result_unknown"))):
        call_id, event_id = str(i), f"event-{i}"
        messages.extend([{"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": "rag_search", "arguments": json.dumps({"query": query})}}]},
                         {"role": "tool", "tool_call_id": call_id, "_event_id": event_id, "_provenance": status, "content": "result"}])
        events.append({"event_id": event_id, "tool_call_id": call_id, "name": "rag_search", "source": status})
    data = json.loads(work_progress_context({"messages": messages, "last_run": {"tools": events}}).split("\n")[-1])
    groups = {item["query"]: item for item in data["query_counts"]}
    assert groups["alpha"]["count"] == 2 and groups["alpha"]["statuses"] == {"executed": 1, "result_unknown": 1}
    assert groups["beta"]["statuses"] == {"executed": 1}
    assert all("issue" not in group for group in groups.values())


def test_item_refs_count_real_pairs_and_preserve_unassociated():
    from agent_core.tools.rag_search import RagSearchArgs
    assert RagSearchArgs(query="q").item_ref is None
    assert RagSearchArgs(query="q", item_ref="item-A").item_ref == "item-A"
    with pytest.raises(ValueError):
        RagSearchArgs(query="q", item_ref="x" * 65)
    with pytest.raises(ValueError):
        RagSearchArgs(query="q", item_ref=123)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    events = []
    for i, (ref, status) in enumerate((("A", "executed"), ("B", "result_unknown"), (None, "executed"), ("", "executed"), ("x" * 65, "not_executed"))):
        args = {"query": "same"}
        if ref is not None:
            args["item_ref"] = ref
        messages.extend([{"role": "assistant", "tool_calls": [{"id": str(i), "function": {"name": "rag_search", "arguments": json.dumps(args)}}]},
                         {"role": "tool", "tool_call_id": str(i), "_event_id": str(i), "_provenance": status, "content": "result"}])
        events.append({"event_id": str(i), "tool_call_id": str(i), "name": "rag_search", "source": status})
    text = work_progress_context({"messages": messages, "last_run": {"tools": events}})
    assert len(text) <= 900
    data = json.loads(text.split("\n")[-1])
    assert data["unassociated"] == 3
    assert {item["item_ref"]: item["statuses"] for item in data["item_counts"]} == {"A": {"executed": 1}, "B": {"result_unknown": 1}}
    assert data["omitted_item_refs"] == 0


def test_item_ref_ledger_bounded_and_reports_omissions():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
    for i in range(40):
        args = {"query": "same", "item_ref": "identifier-" + str(i) + "x" * 45}
        messages.extend([{"role": "assistant", "tool_calls": [{"id": str(i), "function": {"name": "rag_search", "arguments": json.dumps(args)}}]},
                         {"role": "tool", "tool_call_id": str(i), "_event_id": str(i), "_provenance": "executed", "content": "result"}])
    text = work_progress_context({"messages": messages, "last_run": {"tools": []}})
    data = json.loads(text.split("\n")[-1])
    assert len(text) <= 900
    assert data["omitted_item_refs"] + len(data["item_counts"]) == 40
    assert data["omitted_item_refs"] > 0


def test_item_ref_does_not_change_retrieval_arguments(monkeypatch):
    import importlib
    module = importlib.import_module("agent_core.tools.rag_search")
    seen = []
    def retrieval(query, **kwargs):
        seen.append((query, kwargs))
        return "result"
    monkeypatch.setattr(module, "_rag_search", retrieval)
    assert module.rag_search("same") == "result"
    assert module.rag_search("same", item_ref="A") == "result"
    assert seen[0] == seen[1]
    with pytest.raises(ValueError):
        module.rag_search("same", item_ref="x" * 65)
