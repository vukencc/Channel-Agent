"""受控离线回调验证草稿闸门；真文件读取，隔离动态写入口；不是实模型或回放。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.budgets import BudgetExceeded, active_budget
from agent_core.core.flows import FlowService
from agent_core.core.instrumentation import tool_outcome
from agent_core.core.llm import RuntimeModelRequestError, TOOL_SCHEMAS
from agent_core.core.model_settings import active_endpoint, active_reasoning_effort, active_thinking_mode
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import selected_schemas


SOURCE = "页面无法提交，请帮忙排查。"
DISPUTED = "客户明确要求转人工处理。"
CORRECTED = "建议安排人工排查页面提交故障。"


def assistant(content="", calls=None):
    reply = {"role": "assistant", "content": content}
    if calls:
        reply["tool_calls"] = calls
    return reply


def call(identifier, name, **arguments):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


def review_payload(history):
    if len(history) != 2 or [m["role"] for m in history] != ["system", "user"]:
        return None
    try:
        payload = json.loads(history[1]["content"])
    except (TypeError, ValueError):
        return None
    allowed = {"draft", "sources", "workflow_task", "review_field_hints", "targets", "claim_texts",
               "action_context", "evidence_passages", "revision_context"}
    if not isinstance(payload, dict) or not {"draft", "sources"} <= set(payload) <= allowed:
        return None
    if "revision_context" in payload:
        # 受控模型只把来源逐字、位置对得上的新请求识别为修订复核。
        nav = payload["revision_context"]
        assert isinstance(nav, dict) and {"source_locations"} <= set(nav) <= {"source_locations", "edits"}
        fields = {row["field"] for row in payload["targets"]}
        sources = {row["id"]: row["text"] for row in payload["sources"]}
        assert 1 <= len(nav["source_locations"]) <= 5
        for loc in nav["source_locations"]:
            assert set(loc) == {"field", "source_id", "start", "end", "quote"}
            assert loc["field"] in fields and type(loc["start"]) is type(loc["end"]) is int
            raw = sources[loc["source_id"]]
            assert 0 <= loc["start"] < loc["end"] <= len(raw)
            assert raw[loc["start"]:loc["end"]] == loc["quote"]
    return payload


@pytest.fixture
def runtime_store(tmp_path, monkeypatch, sandbox_env):
    for name in ("RAG_ASSESS", "MEMORY_AUTO_EXTRACT", "WORKSPACE_MEMORY_ENABLED",
                 "ENABLE_STRUCTURED_CONTEXT", "AUTO_CONTINUE_TASKS"):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, "ENABLE_SESSION_BUDGETS", True)
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 12)
    (sandbox_env / "source.txt").write_text(SOURCE, encoding="utf-8")
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
    yield store, sandbox_env
    store.close()


async def scenario(runtime_store, monkeypatch, *, final=False, classic=False,
                   disputed=True, review_mode="revise", rounds=None, permission="full_access",
                   repair_tools=False, abandon=False, missing_source=False,
                   recovery_write=False, recovery_repeat=False, item_ref=None, two_writes=False, final_draft=DISPUTED):
    store, project = runtime_store
    seen, reviews, writes, scopes = [], [], [], []
    normal_index = 0
    write_name = "create_file" if classic else "record_case"

    def write_call(identifier, reason):
        arguments = {"reason": reason}
        if item_ref is not None:
            arguments["item_ref"] = item_ref
        return call(identifier, write_name, **arguments)

    async def model(history, *, session_id, emit):
        nonlocal normal_index
        snapshot = copy.deepcopy(history)
        session = manager.sessions[session_id]
        scope = (session_id, session.record["last_run"]["turn_id"], active_endpoint.get(),
                 active_budget.get(), active_reasoning_effort.get(), active_thinking_mode.get(),
                 session.record["permission_policy"])
        scopes.append(scope)
        payload = review_payload(history)
        if payload is not None:
            reviews.append(payload)
            assert "读取原文并记录排查建议后交接" in payload["workflow_task"]
            assert selected_schemas(TOOL_SCHEMAS) == []
            assert payload["sources"] and any(SOURCE in s["text"] for s in payload["sources"])
            assert "独立审查" in history[0]["content"]
            if review_mode == "cancel":
                session.cancelled.set()
                raise asyncio.CancelledError
            if review_mode == "budget":
                raise BudgetExceeded("受控复核预算耗尽")
            if review_mode in {"timeout_cancel", "timeout_budget"}:
                if len(reviews) == 1:
                    raise TimeoutError("受控首次复核超时")
                if review_mode == "timeout_cancel":
                    session.cancelled.set()
                    raise asyncio.CancelledError
                raise BudgetExceeded("受控恢复预算耗尽")
            if review_mode == "timeout_once" and len(reviews) == 1:
                raise TimeoutError("受控首次复核超时")
            if review_mode == "timeout_once":
                return assistant('{"status":"pass"}')
            if review_mode == "timeout":
                raise TimeoutError("受控复核超时")
            if review_mode == "http_error":
                raise RuntimeModelRequestError("受控 provider HTTP 503")
            if review_mode == "tools":
                return assistant('{"status":"pass"}', [call("malicious", "record_case", reason=DISPUTED)])
            if review_mode == "pass":
                return assistant('{"status":"pass"}')
            if review_mode != "reject_all" and CORRECTED in payload["draft"]:
                return assistant('{"status":"pass"}')
            source = next(s for s in payload["sources"] if SOURCE in s["text"])
            issue = {"reason": "客户未明确要求人工，应改为排查建议", "source_id": source["id"], "quote": SOURCE}
            if payload.get("targets"):
                target = payload["targets"][0]
                issue.update(target_id=target["id"], draft_quote=target["excerpt"])
            else:
                issue["field"] = "reason"
            return assistant(json.dumps({"status": "revise", "issues": [issue]}, ensure_ascii=False))
        seen.append(snapshot)
        normal_index += 1
        if missing_source:
            if normal_index == 1:
                return assistant(DISPUTED)
            names = {schema["function"]["name"] for schema in selected_schemas(TOOL_SCHEMAS)}
            if normal_index == 2:
                assert names and names <= {"read_file", "list_files"}
                calls = [call("recover-source", "read_file", path="source.txt")]
                if recovery_write:
                    calls.append(call("recovery-write", "record_case", reason=CORRECTED))
                return assistant(calls=calls)
            assert names == set()
            if recovery_repeat:
                return assistant(calls=[call("repeat-read", "read_file", path="source.txt")])
            return assistant(CORRECTED)
        if normal_index == 1:
            return assistant(calls=[call("read-original", "read_file", path="source.txt")])
        if normal_index == 2:
            calls = [write_call("write-original", (
                CORRECTED if final or not disputed else DISPUTED))]
            if two_writes:
                calls.append(write_call("write-second", CORRECTED))
            return assistant(calls=calls)
        if normal_index == 3 and not final and disputed and not classic and review_mode != "pass":
            if abandon:
                return assistant("已完成记录，全部处理结束。")
            return assistant(calls=[write_call("write-corrected", CORRECTED)])
        if normal_index == 3 and final:
            return assistant(final_draft)
        if normal_index == 4 and final:
            assert selected_schemas(TOOL_SCHEMAS) == []
            if repair_tools:
                return assistant(calls=[call("repeat-success", "record_case", reason=CORRECTED)])
        return assistant(CORRECTED if final else "已记录一次排查建议。")

    manager = SessionManager(store, model=model)
    service = FlowService(manager)
    original_tool = manager._tool

    async def isolated_write(session, tool_call, *, evidence=None):
        if tool_call["function"]["name"] != write_name:
            return await original_tool(session, tool_call, evidence=evidence)
        if permission == "readonly":
            return await original_tool(session, tool_call, evidence=evidence)
        # 仅隔离动态执行入口；参数记录与真实临时写入用于证明执行次数。
        assert session.record["permission_policy"] == permission
        arguments = json.loads(tool_call["function"]["arguments"])
        writes.append(arguments)
        (project / "recorded.json").write_text(json.dumps(writes, ensure_ascii=False), encoding="utf-8")
        evidence["source"] = "executed"
        return "已记录：" + arguments["reason"]

    monkeypatch.setattr(manager, "_tool", isolated_write)
    try:
        if classic:
            session = manager.create()
            manager.set_workspace_path(session, str(project))
            manager.set_permission_policy(session, permission)
            manager.set_tool_names(session, ["read_file", "create_file"])
            manager.submit(session, "读取原文并记录排查建议")
            await session.task
            result = None
        else:
            flow = service.create(workspace_path=str(project), permission_policy=permission)
            flow["nodes"] = [{"id": "a", "title": "原业务", "prompt": "读取原文并记录排查建议后交接",
                              "tool_names": ["read_file", "record_case"], "custom_tools": [tool_spec()],
                              "reasoning_effort": "high", "thinking_mode": "enabled"}]
            flow["edges"] = []
            flow = service.save(flow)
            if rounds is not None:
                original_submit = manager.submit

                def submit(session, *args, **kwargs):
                    manager.set_limits(session, {"MAX_TOOL_ROUNDS": rounds})
                    return original_submit(session, *args, **kwargs)

                monkeypatch.setattr(manager, "submit", submit)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            result = service.get(flow["id"])["run"]
            session = next(iter(manager.sessions.values()))
        return session.record, result, seen, reviews, writes, scopes
    finally:
        await service.shutdown()
        await manager.shutdown()


def tool_spec():
    return {"name": "record_case", "description": "记录排查理由",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"},
                                                              "item_ref": {"type": "string"}},
                           "required": ["reason"]},
            "code": "def main(arguments):\n    return arguments['reason']\n"}


def test_disputed_dynamic_arguments_rejected_then_execute_once(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, item_ref="case-1"))
    assert len(reviews) == 2, "拒稿改写去掉触发词后仍须独立复审，成功回执后解除"
    assert DISPUTED in reviews[0]["draft"] and CORRECTED in reviews[1]["draft"]
    assert all(json.loads(review["draft"])["item_ref"] == "case-1" for review in reviews)
    assert writes == [{"reason": CORRECTED, "item_ref": "case-1"}]
    messages = record["messages"]
    rejected = [m for m in messages if m.get("tool_call_id") == "write-original"]
    assert len(rejected) == 1 and rejected[0]["_provenance"] == "not_executed"
    assert rejected[0]["content"].startswith("[已拒绝]")
    assert "客户未明确要求人工" in rejected[0]["content"]
    assert next(m for m in messages if m.get("tool_call_id") == "write-corrected")["_provenance"] == "executed"
    assert len({scope[0] for scope in scopes}) == len({scope[1] for scope in scopes}) == 1
    assert all(scope == scopes[0] for scope in scopes)
    assert scopes[0][4:] == ("high", "enabled", "full_access")
    assert record["last_run"]["model_rounds"] == len(scopes)
    read_receipt = next(m for m in messages if m.get("tool_call_id") == "read-original")
    assert read_receipt["_provenance"] == "executed"
    assert SOURCE in read_receipt["content"] and read_receipt["_context_observation"]
    assert json.loads((runtime_store[1] / "recorded.json").read_text(encoding="utf-8")) == writes
    assert result["status"] == "completed"


def test_disputed_final_handoff_repairs_body_without_repeating_success(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(runtime_store, monkeypatch, final=True))
    assert len(reviews) == 2, "被拒交接改写去掉触发词后仍须独立复审"
    assert reviews[0]["draft"] == DISPUTED and reviews[1]["draft"] == CORRECTED
    assert writes == [{"reason": CORRECTED}]
    assert result["nodes"]["a"]["output_handoff"].endswith(CORRECTED)
    assert DISPUTED not in result["nodes"]["a"]["output_handoff"]
    assert result["status"] == "completed"
    assert "客户未明确要求人工" in str(seen[-1])


@pytest.mark.parametrize("classic,disputed", [(True, True), (False, False)])
def test_ordinary_session_and_uncontroversial_arguments_bypass_review(runtime_store, monkeypatch, classic, disputed):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, classic=classic, disputed=disputed))
    assert reviews == []
    assert writes == [{"reason": DISPUTED if classic else CORRECTED}]
    assert not any(m.get("_provenance") == "not_executed" for m in record["messages"])


@pytest.mark.parametrize("mode", ["tools", "cancel", "budget", "timeout", "http_error"])
def test_review_failure_cannot_execute_disputed_write(runtime_store, monkeypatch, mode):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, review_mode=mode))
    assert reviews, "故障注入必须到达独立复核入口"
    assert all(write["reason"] != DISPUTED for write in writes)
    assert not any(m.get("tool_call_id") == "malicious" and m.get("_provenance") == "executed"
                   for m in record["messages"])
    if mode in {"tools", "cancel", "budget", "timeout", "http_error"}:
        assert writes == []
        assert result["status"] != "completed"
    identifiers = [c["id"] for m in record["messages"] for c in m.get("tool_calls", [])]
    receipts = [m["tool_call_id"] for m in record["messages"] if m["role"] == "tool"]
    assert sorted(identifiers) == sorted(receipts)


def test_selected_round_exhaustion_never_executes_unreviewed_claim(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, rounds=2))
    assert writes == []
    assert len(scopes) <= 2
    assert result["status"] != "completed"


def test_valid_pass_preserves_original_arguments(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, review_mode="pass"))
    assert reviews
    assert writes == [{"reason": DISPUTED}]
    assert next(m for m in record["messages"] if m.get("tool_call_id") == "write-original")["_provenance"] == "executed"


def test_review_keeps_selected_readonly_permission(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, review_mode="pass", permission="readonly"))
    assert reviews
    assert writes == []
    assert all(scope[-1] == "readonly" for scope in scopes)
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == "write-original")
    assert tool_outcome(receipt["content"]) != "completed", receipt
    assert not (runtime_store[1] / "recorded.json").exists()


def test_handoff_repair_cannot_reissue_successful_tool(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, repair_tools=True))
    assert reviews
    assert writes == [{"reason": CORRECTED}]
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == "repeat-success")
    assert receipt["_provenance"] == "not_executed"
    assert result["status"] != "completed"


def test_rejected_write_cannot_be_abandoned_with_completion_claim(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, abandon=True))
    assert len(reviews) == 1
    assert writes == []
    assert record["status"] == "checkpoint"
    assert result["status"] != "completed"
    assert not result["nodes"]["a"]["output_handoff"]
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == "write-original")
    assert receipt["_provenance"] == "not_executed"


def test_rephrased_write_stays_unexecuted_until_independent_approval(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, review_mode="reject_all"))
    assert len(reviews) == 2
    assert CORRECTED in reviews[1]["draft"]
    assert writes == []
    for identifier in ("write-original", "write-corrected"):
        receipt = next(m for m in record["messages"] if m.get("tool_call_id") == identifier)
        assert receipt["_provenance"] == "not_executed"
    assert record["status"] == "checkpoint"
    assert result["status"] != "completed"


def test_missing_source_handoff_gets_one_readonly_recovery_batch(runtime_store, monkeypatch):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, missing_source=True))
    assert len(reviews) == 1
    assert reviews[0]["draft"] == CORRECTED
    assert writes == []
    source = next(m for m in record["messages"] if m.get("tool_call_id") == "recover-source")
    assert source["_provenance"] == "executed" and SOURCE in source["content"]
    assert result["status"] == "completed"
    assert result["nodes"]["a"]["output_handoff"].endswith(CORRECTED)


@pytest.mark.parametrize("write,repeat,identifier", [
    (True, False, "recovery-write"), (False, True, "repeat-read"),
])
def test_source_recovery_cannot_write_or_repeat_read_batch(runtime_store, monkeypatch, write, repeat, identifier):
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, missing_source=True, recovery_write=write, recovery_repeat=repeat))
    assert writes == []
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == identifier)
    assert receipt["_provenance"] == "not_executed"
    assert result["status"] != "completed"


async def classification_scenario(runtime_store, *, need_source, lookup_command_transport=None):
    """真实临时资料和动态读取命令；仅模型判定使用离线回调。"""
    store, project = runtime_store
    task = "将页面提交故障分类为技术问题，路由人工排查；分类不表示客户明确要求人工。"
    draft = "客户表示页面无法提交，分类为技术问题并路由人工排查。"
    rows = [{"id": "CF-01", "text": SOURCE}, {"id": "CF-02", "text": "未选择的物流反馈"}]
    (project / "feedback.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (project / "unrelated.txt").write_text("当日排班：值班A。", encoding="utf-8")
    lookup = {"name": "lookup_feedback", "description": "按事项选择读取反馈文件",
              "parameters": {"type": "object", "properties": {
                  "path": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}},
                  "required": ["path", "ids"]},
              "code": "def main(arguments):\n    import json\n    from pathlib import Path\n    rows = json.loads(Path(arguments['path']).read_text(encoding='utf-8'))\n    return [row for row in rows if row['id'] in arguments['ids']]\n"}
    if lookup_command_transport is not None:
        lookup_command_transport(lookup, project)
    reviews = []
    normal_index = 0

    async def model(history, *, session_id, emit):
        nonlocal normal_index
        payload = review_payload(history)
        if payload is not None:
            assert selected_schemas(TOOL_SCHEMAS) == []
            assert task in payload["workflow_task"]
            assert "不是客户意愿" in history[0]["content"]
            assert not any(task in row["text"] for row in payload["sources"])
            reviews.append(payload)
            if need_source and len(reviews) == 1:
                assert any("当日排班" in row["text"] for row in payload["sources"])
                assert not any(SOURCE in row["text"] for row in payload["sources"])
                return assistant(json.dumps({"status": "need_source", "field": "客户故障描述",
                                             "needed": "source.txt中的客户原文"}, ensure_ascii=False))
            if need_source:
                assert any(row["kind"] == "read_file" and SOURCE in row["text"] for row in payload["sources"])
            else:
                reports = [row for row in payload["sources"] if row["kind"] == "custom_result"]
                assert len(reports) == 1 and reports[0]["scope"] == "tool_report"
                assert reports[0]["tool"] == "lookup_feedback" and reports[0]["path"] == "feedback.json"
                assert json.loads(reports[0]["text"]) == [rows[0]]
            return assistant('{"status":"pass"}')
        normal_index += 1
        if normal_index == 1:
            if need_source:
                return assistant(calls=[call("unrelated", "read_file", path="unrelated.txt")])
            return assistant(calls=[call("selected-feedback", "lookup_feedback", path="feedback.json", ids=["CF-01"])])
        if need_source and normal_index == 3:
            names = {schema["function"]["name"] for schema in selected_schemas(TOOL_SCHEMAS)}
            assert names and names <= {"read_file", "list_files"}
            return assistant(calls=[call("requested-source", "read_file", path="source.txt")])
        if need_source and normal_index == 4:
            assert selected_schemas(TOOL_SCHEMAS) == []
        return assistant(draft)

    manager = SessionManager(store, model=model)
    service = FlowService(manager)
    try:
        flow = service.create(workspace_path=str(project), permission_policy="readonly" if need_source else "full_access")
        flow["nodes"] = [{"id": "triage", "title": "分类", "prompt": task,
                          "tool_names": ["read_file", "lookup_feedback"], "custom_tools": [lookup]}]
        flow["edges"] = []
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        session = next(iter(manager.sessions.values()))
        audit = (store.directory(session.id) / "audit.jsonl").read_text(encoding="utf-8")
        return session.record, service.get(flow["id"])["run"], reviews, audit, draft
    finally:
        await service.shutdown()
        await manager.shutdown()


def test_selected_dynamic_report_and_node_task_reach_independent_review(runtime_store, lookup_command_transport):
    record, result, reviews, audit, draft = asyncio.run(classification_scenario(
        runtime_store, need_source=False, lookup_command_transport=lookup_command_transport))
    assert len(reviews) == 1
    assert result["status"] == "completed"
    assert result["nodes"]["triage"]["output_handoff"].endswith(draft)
    assert "custom_tool_execution" in audit and "lookup_feedback" in audit
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == "selected-feedback")
    assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
    assert "未选择的物流反馈" not in receipt["content"]


def test_need_source_after_unrelated_read_preserves_readonly_policy(runtime_store):
    record, result, reviews, audit, draft = asyncio.run(classification_scenario(runtime_store, need_source=True))
    assert len(reviews) == 2
    assert result["status"] == "completed"
    assert record["permission_policy"] == "readonly"
    assert result["nodes"]["triage"]["output_handoff"].endswith(draft)
    source = next(m for m in record["messages"] if m.get("tool_call_id") == "requested-source")
    assert source["_provenance"] == "executed" and SOURCE in source["content"]
    assert {event["name"] for event in record["last_run"]["tools"]} == {"read_file"}


@pytest.mark.parametrize("large", [False, True])
def test_final_review_timeout_recovers_without_repeating_write(runtime_store, monkeypatch, large):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    draft = json.dumps({"scope": DISPUTED, "background": "文" * 1100}, ensure_ascii=False) if large else DISPUTED
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="timeout_once", two_writes=True, final_draft=draft))
    assert len(reviews) == 2 and reviews[0] == reviews[1]
    assert writes == [{"reason": CORRECTED}, {"reason": CORRECTED}]
    assert result["status"] == "completed"
    assert result["nodes"]["a"]["output_handoff"].endswith(draft)
    if large:
        assert reviews[0]["review_field_hints"] == ["/scope"]
        assert json.loads(reviews[0]["draft"]) == json.loads(draft)
    assert record["last_run"]["recovery_attempts"] == 1
    assert record["last_run"]["draft_reviews"][-1]["attempts"] == ["TimeoutError", "passed"]
    assert record["last_run"]["model_rounds"] == len(scopes)


def test_final_review_timeout_recovery_disabled(runtime_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="timeout_once"))
    assert len(reviews) == 1
    assert writes == [{"reason": CORRECTED}]
    assert result["status"] != "completed"
    assert record["last_run"]["draft_reviews"][-1]["status"] == "TimeoutError"


def test_final_review_repeated_timeout_remains_blocked(runtime_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="timeout"))
    assert len(reviews) == 2
    assert writes == [{"reason": CORRECTED}]
    assert result["status"] != "completed"
    assert record["last_run"]["draft_reviews"][-1]["attempts"] == ["TimeoutError", "TimeoutError"]


def test_final_review_timeout_without_round_budget_stays_blocked(runtime_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="timeout_once", rounds=4))
    assert len(reviews) == 1
    assert writes == [{"reason": CORRECTED}]
    assert result["status"] != "completed"
    assert record["last_run"]["draft_reviews"][-1]["status"] == "TimeoutError"


@pytest.mark.parametrize("mode,status", [("timeout_cancel", "cancelled"), ("timeout_budget", "BudgetExceeded")])
def test_final_review_recovery_failure_stays_blocked(runtime_store, monkeypatch, mode, status):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    record, result, seen, reviews, writes, scopes = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode=mode))
    assert len(reviews) == 2
    assert writes == [{"reason": CORRECTED}]
    assert result["status"] != "completed"
    event = record["last_run"]["draft_reviews"][-1]
    assert event["status"] == status
    assert event["attempts"] == ["TimeoutError", status]
