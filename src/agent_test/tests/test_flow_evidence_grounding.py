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
            assert "上游自述" in text and "不得将上限当实际调用数" in text
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


@pytest.mark.parametrize("structured", [False, True])
def test_rag_receipt_reaches_model_for_complete_parallel_batch(tmp_path, monkeypatch, structured):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    store = SessionStore(tmp_path / "state", tmp_path / "workspace")
    project = tmp_path / "project"
    project.mkdir()
    seen = []
    async def model(history, **kwargs):
        seen.append(history)
        if len(seen) == 1:
            calls = []
            for i, ref in enumerate(("A", "B", "A", None)):
                args = {"query": "same"}
                if ref is not None:
                    args["item_ref"] = ref
                calls.append({"id": str(i), "type": "function", "function": {"name": "rag_search", "arguments": json.dumps(args)}})
            return {"role": "assistant", "content": "", "tool_calls": calls}
        return {"role": "assistant", "content": "完成"}
    async def run():
        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        async def tool(session, call, *, evidence=None):
            from agent_core.core.tool_observations import observe_text
            evidence["source"] = "result_unknown" if call["id"] == "2" else "executed"
            if call["id"] == "2":
                return "[超时]"
            return observe_text("原始检索正文", reader_tool="rag_search", body_spans=((0, 6),), recovery_notice="重新检索")
        manager._tool = tool
        try:
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "节点", "prompt": "检索", "tool_names": ["rag_search"]}]
            flow["edges"] = []
            flow = service.save(flow)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            assert service.get(flow["id"])["run"]["status"] == "completed"
            results = {m["tool_call_id"]: m["content"] for m in seen[1] if m["role"] == "tool"}
            assert len(results) == 4
            for key in ("0", "2"):
                assert "请求 2" in results[key] and "成功返回 1" in results[key] and "超时或结果未知 1" in results[key]
            assert "请求 1" in results["1"] and "成功返回 1" in results["1"]
            assert "未关联" in results["3"] and "请求 1" in results["3"]
            assert "原始检索正文" in results["0"]
            assert "不等于命中" in results["0"] and "上限不是实际次数" in results["0"]
            from agent_core.core.tool_observations import validate_observation
            records = [m for session in manager.sessions.values() for m in session.record["messages"]
                       if m.get("tool_call_id") == "0"]
            assert len(records) == 1
            assert validate_observation(records[0]["content"], records[0]["_context_observation"]) is not None
        finally:
            await service.shutdown()
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_batch_receipts_reject_ambiguous_pairing_and_scan_once(monkeypatch):
    from agent_core.core import execution_guidance as guidance
    calls = [{"id": ident, "function": {"name": "rag_search", "arguments": json.dumps({"query": "q", "item_ref": "A"})}} for ident in ("duplicate", "duplicate", "unique")]
    calls[2]["function"]["arguments"] = json.dumps({"query": "q", "item_ref": "B"})
    results = [("ok", {"event_id": f"e{i}", "name": "rag_search", "tool_call_id": call["id"], "source": "executed", "outcome": "completed"}) for i, call in enumerate(calls)]
    record = {"messages": [{"role": "user", "content": "task"}, {"role": "assistant", "tool_calls": calls}], "last_run": {"tools": []}}
    original = guidance._retrieval_rows
    count = []
    def counted(record):
        count.append(1)
        return original(record)
    monkeypatch.setattr(guidance, "_retrieval_rows", counted)
    receipts = guidance.retrieval_batch_receipts(record, calls, results)
    assert len(count) == 1
    assert "无法核实" in receipts[0] and "无法核实" in receipts[1]
    assert "请求 1" in receipts[2]
    results[2][1]["event_id"] = results[0][1]["event_id"]
    receipts = guidance.retrieval_batch_receipts(record, calls, results)
    assert all("无法核实" in receipt for receipt in receipts)
