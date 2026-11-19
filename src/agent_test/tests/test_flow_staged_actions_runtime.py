"""两阶段动作的受控离线模型回归；真读临时文件，业务写入口单独隔离；非实模型/回放。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.messages import is_user_request
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.tool_settings import selected_schemas
from test_flow_draft_review_runtime import (
    CORRECTED, DISPUTED, SOURCE, assistant, call, review_payload,
    runtime_store, tool_spec,
)


async def staged_scenario(runtime_store, monkeypatch, mode="success"):
    """模型从实际授权 schema 发现草稿入口，绝不预告草稿别名。"""
    store, project = runtime_store
    reviews, normal_schemas, writes, model_assertions = [], [], [], []
    normal_index = 0
    preview_name = None
    approved = {"reason": CORRECTED, "item_ref": "case-1"}
    unidentified = mode in {"source_change", "two_unidentified"}
    second_approved = {"reason": "客户表示页面无法提交，建议记录故障排查。"}
    if unidentified:
        approved = {"reason": "客户表示页面无法提交，建议安排人工排查。"}
    if mode == "source_change":
        (project / "extra-source.txt").write_text("补充资料：本事项尚待排查。", encoding="utf-8")
    if mode == "blind_batch":
        approved["reason"] = "客户表示页面无法提交，建议安排人工排查。"
    permission = "readonly" if mode == "readonly" else "full_access"
    if mode == "timeout":
        monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)

    async def model(history, *, session_id, emit):
        nonlocal normal_index, preview_name
        session = manager.sessions[session_id]
        schemas = selected_schemas(TOOL_SCHEMAS)
        payload = review_payload(history)
        if payload is not None:
            assert schemas == [], "独立复核不得调用业务工具"
            assert len(history) == 2
            assert any(SOURCE in row["text"] for row in payload["sources"])
            reviews.append(copy.deepcopy(payload))
            if mode == "timeout":
                raise TimeoutError("受控离线复核超时")
            if DISPUTED in payload["draft"]:
                source = next(row for row in payload["sources"] if SOURCE in row["text"])
                target = next(row for row in payload["targets"] if DISPUTED in row["excerpt"])
                # 两侧引文确实来自请求，不编造来源或裁决协议。
                assert SOURCE in source["text"]
                assert target["excerpt"] in json.loads(payload["draft"])["reason"]
                return assistant(json.dumps({"status": "revise", "issues": [{
                    "reason": "客户未明确要求人工，应改为排查建议",
                    "target_id": target["id"], "draft_quote": target["excerpt"],
                    "source_id": source["id"], "quote": SOURCE,
                }]}, ensure_ascii=False))
            assert json.loads(payload["draft"]) in ([approved, second_approved] if unidentified else [approved])
            return assistant('{"status":"pass"}')

        normal_index += 1
        names = {schema["function"]["name"] for schema in schemas}
        normal_schemas.append(names)
        if normal_index == 1:
            assert "record_case" not in names, "正式动作必须等草稿获批后才宣告"
            previews = [schema["function"]["name"] for schema in schemas
                        if "record_case" in schema["function"].get("description", "")]
            assert len(previews) == 1, "从授权 schema 必须可发现唯一草稿别名"
            preview_name = previews[0]
            assert preview_name != "record_case"
            return assistant(calls=[call("source", "read_file", path="source.txt")])
        if unidentified:
            if normal_index == 2:
                return assistant(calls=[call("draft-first", preview_name, **approved)])
            if mode == "source_change":
                if normal_index == 3:
                    assert "record_case" in names
                    return assistant(calls=[call("extra-source", "read_file", path="extra-source.txt")])
                if normal_index == 4:
                    assert "record_case" not in names, "补读新来源后旧批准必须失效"
                    return assistant(calls=[call("stale-formal", "record_case", **approved)])
                if normal_index == 5:
                    return assistant(calls=[call("draft-renewed", preview_name, **approved)])
                if normal_index == 6:
                    assert "record_case" in names
                    return assistant(calls=[call("formal", "record_case", **approved)])
            else:
                if normal_index == 3:
                    return assistant(calls=[call("draft-second", preview_name, **second_approved)])
                if normal_index == 4:
                    assert "record_case" in names
                    return assistant(calls=[call("formal-first", "record_case", **approved),
                                            call("formal-second", "record_case", **second_approved)])
            return assistant("已核对回执。")
        if normal_index == 2:
            assert "record_case" not in names
            if mode == "direct_unapproved":
                return assistant(calls=[call("formal", "record_case", **approved)])
            args = approved if mode == "blind_batch" else dict(approved, reason=DISPUTED)
            calls = [call("draft-first", preview_name, **args)]
            if mode == "blind_batch":
                calls.append(call("blind-submit", "record_case", **approved))
            return assistant(calls=calls)
        if normal_index == 3 and mode not in {"blind_batch", "timeout", "direct_unapproved"}:
            assert "record_case" not in names, "争议草稿必须有效 revise 后继续隐藏正式动作"
            assert "客户未明确要求人工" in str(history)
            return assistant(calls=[call("draft-revised", preview_name, **approved)])
        if normal_index == 4 and mode not in {"blind_batch", "timeout", "direct_unapproved"}:
            assert "record_case" in names, "获批后下一轮应开放原名提交"
            if mode in {"early_handoff", "ignore_handoff"}:
                return assistant("提前交接：草稿已批准，本节点处理结束。")
            arguments = dict(approved)
            if mode == "changed_arguments":
                arguments["reason"] = "建议另行安排排查。"
            if mode == "changed_item":
                arguments["item_ref"] = "case-2"
            if mode == "cancel":
                session.cancelled.set()
            if mode == "revoke":
                # 部署层撤销生效于返回前；活跃会话的用户策略本身不可修改。
                manager.permission_override = "readonly"
            return assistant(calls=[call("formal", "record_case", **arguments)])
        if normal_index >= 5 and mode in {"early_handoff", "ignore_handoff"}:
            assert (manager.history_context is not None) == config.ENABLE_STRUCTURED_CONTEXT
            early = next(index for index, row in enumerate(history)
                         if row.get("content") == "提前交接：草稿已批准，本节点处理结束。")
            references = [row for row in history[early + 1:] if row["role"] == "user" and row.get("content")]
            assert references, "提前交接后须提供待提交上下文"
            assert all(row.get("_context_reference") and not is_user_request(row) for row in references), "获批参数仍是引用数据，不能变为新用户授权"
            if normal_index == 5 or mode == "ignore_handoff":
                assert "record_case" in names, "补交上下文给出的原名也须在本次实际目录中"

            def has_copyable_proposal(value):
                if isinstance(value, list):
                    return any(has_copyable_proposal(child) for child in value)
                if not isinstance(value, dict):
                    return False
                if "record_case" in value.values():
                    for child in value.values():
                        if isinstance(child, dict) and child == approved:
                            return True
                        if isinstance(child, str):
                            try:
                                if json.loads(child) == approved:
                                    return True
                            except ValueError:
                                pass
                return any(has_copyable_proposal(child) for child in value.values())

            decoder = json.JSONDecoder()
            copyable = False
            for row in references:
                text = row["content"]
                for index, char in enumerate(text):
                    if char not in "[{":
                        continue
                    try:
                        value, _ = decoder.raw_decode(text[index:])
                    except ValueError:
                        continue
                    copyable |= has_copyable_proposal(value)
            assert copyable, "补交轮输入须含可解析的原名与准确获批原参数，而非只有目录或数量"
            if mode == "ignore_handoff":
                return assistant("仍然提前交接，不提交正式工具。")
            if normal_index == 5:
                assert "record_case" in names
                return assistant(calls=[call("formal", "record_case", **approved)])
            receipt = next(row for row in history if row.get("tool_call_id") == "formal")
            assert receipt["_provenance"] == "executed"
            assert CORRECTED in receipt["content"]
            return assistant("已读取正式执行回执，本节点完成。")
        if normal_index == 5 and mode == "duplicate":
            return assistant(calls=[call("formal-repeat", "record_case", **approved)])
        return assistant("已核对回执。")

    async def checked_model(history, *, session_id, emit):
        # Flow 会把回调断言保存为运行失败；测试仍须报告真正的契约断点。
        try:
            return await model(history, session_id=session_id, emit=emit)
        except AssertionError as exc:
            model_assertions.append(exc)
            raise

    if mode == "cancel_before_model":
        from agent_core.core.flow_staged_actions import FlowStagedActions
        begin = FlowStagedActions.begin_request

        def stop_before_provider(staged):
            begin(staged)
            staged.session.cancelled.set()

        monkeypatch.setattr(FlowStagedActions, "begin_request", stop_before_provider)
    if mode == "audit_cancel":
        import agent_core.core.flow_staged_actions as staged_module
        original_append = staged_module.append_audit

        def fail_formal_result(path, line, **kwargs):
            if json.loads(line)["stage"] == "commit_result":
                raise OSError("受控审计写入失败")
            return original_append(path, line, **kwargs)

        monkeypatch.setattr(staged_module, "append_audit", fail_formal_result)
    manager = SessionManager(store, model=checked_model)
    service = FlowService(manager)
    projects = ProjectService(manager, service)
    original_tool = manager._tool

    async def controlled_business_write(session, tool_call, *, evidence=None):
        if (tool_call["function"]["name"] != "record_case"
                or (manager.permission_override or session.record["permission_policy"]) == "readonly"):
            return await original_tool(session, tool_call, evidence=evidence)
        arguments = json.loads(tool_call["function"]["arguments"])
        writes.append(arguments)
        (project / "staged-record.json").write_text(json.dumps(writes, ensure_ascii=False), encoding="utf-8")
        if mode == "audit_cancel":
            session.cancelled.set()
        evidence["source"] = "executed"
        return "已记录：" + arguments["reason"]

    monkeypatch.setattr(manager, "_tool", controlled_business_write)
    try:
        project_record = projects.create("两阶段离线验证", str(project), permission_policy=permission)
        flow = service.get(project_record["flow_id"])
        flow["nodes"] = [{"id": "a", "title": "原业务", "prompt": "读取原文并记录排查建议后交接",
                          "tool_names": ["read_file", "record_case"], "custom_tools": [tool_spec()]}]
        flow["edges"] = []
        flow = service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        if model_assertions:
            raise model_assertions[0]
        session = next(iter(manager.sessions.values()))
        effective_policy = manager.permission_override or session.record["permission_policy"]
        return session.record, service.get(flow["id"])["run"], reviews, normal_schemas, writes, preview_name, effective_policy
    finally:
        await service.shutdown()
        await manager.shutdown()


def assert_paired(record):
    calls = [call["id"] for row in record["messages"] for call in row.get("tool_calls", [])]
    receipts = [row["tool_call_id"] for row in record["messages"] if row["role"] == "tool"]
    assert len(calls) == len(set(calls))
    assert sorted(calls) == sorted(receipts)


def test_staged_review_revise_then_formal_same_arguments_execute_once(runtime_store, monkeypatch):
    record, result, reviews, schemas, writes, preview, policy = asyncio.run(staged_scenario(runtime_store, monkeypatch))
    assert result["status"] == "completed"
    assert len(reviews) == 2, "正式提交不得重复语义复核"
    assert DISPUTED in reviews[0]["draft"] and CORRECTED in reviews[1]["draft"]
    assert writes == [{"reason": CORRECTED, "item_ref": "case-1"}]
    assert json.loads((runtime_store[1] / "staged-record.json").read_text(encoding="utf-8")) == writes
    assert_paired(record)
    calls = {entry["id"]: entry for row in record["messages"] for entry in row.get("tool_calls", [])}
    assert calls["draft-first"]["function"]["name"] == calls["draft-revised"]["function"]["name"] == preview
    assert calls["formal"]["function"]["name"] == "record_case"
    assert json.loads(calls["formal"]["function"]["arguments"]) == json.loads(calls["draft-revised"]["function"]["arguments"])
    receipts = {row["tool_call_id"]: row for row in record["messages"] if row["role"] == "tool"}
    assert receipts["source"]["_provenance"] == "executed" and SOURCE in receipts["source"]["content"]
    assert receipts["source"]["_context_observation"]
    assert receipts["formal"]["_provenance"] == "executed"
    metrics = {row["tool_call_id"]: row for row in record["last_run"]["tools"]}
    for identifier in ("draft-first", "draft-revised"):
        assert metrics[identifier]["execution_progress"] is False
        assert receipts[identifier]["_provenance"] == "not_executed"
    assert record["last_run"]["model_rounds"] == len(schemas) + len(reviews)


@pytest.mark.parametrize("mode", ["changed_arguments", "changed_item", "blind_batch", "duplicate", "cancel", "revoke", "readonly", "timeout", "direct_unapproved"])
def test_staged_approval_cannot_authorize_other_or_repeated_execution(runtime_store, monkeypatch, mode):
    record, result, reviews, schemas, writes, preview, policy = asyncio.run(staged_scenario(runtime_store, monkeypatch, mode))
    if mode != "direct_unapproved":
        assert reviews, "故障注入必须到达草稿独立复核"
    assert writes == ([{"reason": CORRECTED, "item_ref": "case-1"}] if mode == "duplicate" else [])
    assert_paired(record)
    if mode != "duplicate":
        assert not (runtime_store[1] / "staged-record.json").exists()
    identifier = {"blind_batch": "blind-submit", "duplicate": "formal-repeat"}.get(mode, "formal")
    receipts = [row for row in record["messages"] if row.get("tool_call_id") == identifier]
    if mode not in {"cancel", "timeout"}:
        assert len(receipts) == 1 and receipts[0]["_provenance"] == "not_executed"
    if mode == "timeout":
        assert len(reviews) == 1 and result["status"] != "completed"
    elif mode == "blind_batch":
        assert len(reviews) == 1
    elif mode == "direct_unapproved":
        assert reviews == []
        assert record["flow_project_id"], "项目绑定的正式入口不能沿用旧版直接调用兼容分支"
    else:
        assert len(reviews) == 2, "正式提交失败也不得重新语义复核"
    if mode in {"readonly", "revoke"}:
        assert policy == "readonly"
        assert record["permission_policy"] == ("full_access" if mode == "revoke" else "readonly")


def test_unidentified_draft_can_be_reapproved_after_source_change(runtime_store, monkeypatch):
    record, result, reviews, schemas, writes, preview, policy = asyncio.run(
        staged_scenario(runtime_store, monkeypatch, "source_change"))
    assert result["status"] == "completed", result
    assert len(reviews) == 2, "失效草稿重新复核，正式提交不得重复复核"
    expected = {"reason": "客户表示页面无法提交，建议安排人工排查。"}
    assert [json.loads(row["draft"]) for row in reviews] == [expected, expected]
    assert not any("补充资料" in row["text"] for row in reviews[0]["sources"])
    assert any("补充资料" in row["text"] for row in reviews[1]["sources"])
    assert writes == [expected]
    assert json.loads((runtime_store[1] / "staged-record.json").read_text(encoding="utf-8")) == writes
    assert_paired(record)
    receipts = {row["tool_call_id"]: row for row in record["messages"] if row["role"] == "tool"}
    assert receipts["extra-source"]["_provenance"] == "executed"
    assert receipts["stale-formal"]["_provenance"] == "not_executed"
    assert receipts["formal"]["_provenance"] == "executed"
    events = {row["tool_call_id"]: row for row in record["last_run"]["action_preflights"]}
    assert events["stale-formal"]["status"] == "not_approved"
    assert events["draft-first"]["status"] == events["draft-renewed"]["status"] == "approved"
    assert events["draft-first"]["draft_id"] != events["draft-renewed"]["draft_id"]


def test_distinct_unidentified_approved_drafts_can_each_be_consumed(runtime_store, monkeypatch):
    record, result, reviews, schemas, writes, preview, policy = asyncio.run(
        staged_scenario(runtime_store, monkeypatch, "two_unidentified"))
    assert result["status"] == "completed", result
    assert len(reviews) == 2
    assert writes == [json.loads(row["draft"]) for row in reviews]
    assert len(writes) == 2 and writes[0] != writes[1]
    assert all(set(row) == {"reason"} for row in writes)
    assert_paired(record)
    receipts = {row["tool_call_id"]: row for row in record["messages"] if row["role"] == "tool"}
    assert all(receipts[identifier]["_provenance"] == "executed"
               for identifier in ("formal-first", "formal-second"))
    events = {row["tool_call_id"]: row for row in record["last_run"]["action_preflights"]}
    assert events["formal-first"]["draft_id"] == events["draft-first"]["draft_id"]
    assert events["formal-second"]["draft_id"] == events["draft-second"]["draft_id"]
    assert events["draft-first"]["draft_id"] != events["draft-second"]["draft_id"]


def test_stop_during_model_preparation_prevents_provider_call(runtime_store, monkeypatch):
    record, run, reviews, schemas, writes, *_ = asyncio.run(
        staged_scenario(runtime_store, monkeypatch, "cancel_before_model"))
    assert not schemas and not reviews and not writes
    assert record["status"] == "cancelled" and run["status"] != "completed"
    assert_paired(record)


def test_uncertain_result_audit_with_stop_preserves_checkpoint(runtime_store, monkeypatch):
    record, run, _, _, writes, *_ = asyncio.run(staged_scenario(runtime_store, monkeypatch, "audit_cancel"))
    assert len(writes) == 1 and run["status"] != "completed"
    assert record["status"] == "checkpoint"
    assert record["last_run"]["stop_reason"] == "flow_action_audit"
    assert record["run_history"][-1]["status"] == "checkpoint"
    assert_paired(record)
    formal = next(row for row in record["last_run"]["tools"] if row["tool_call_id"] == "formal")
    assert formal["source"] == "executed" and formal["outcome"] == "error"
    assert not formal["execution_progress"]


@pytest.mark.parametrize("descendant_kind", ["child", "branch"])
def test_project_flow_descendant_requires_preview_before_original(runtime_store, monkeypatch, descendant_kind):
    """真实创建父 Flow、子会话/分支及隔离工具；仅模型回复受控离线。"""
    store, workspace = runtime_store
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    catalogs = []
    attempted = False
    spec = {"name": "descendant_selector", "description": "读取选择的事项并记录探针",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                           "required": ["id"], "additionalProperties": False},
            "code": "from pathlib import Path\ndef main(arguments):\n    Path('descendant-executed').touch()\n    return arguments['id']\n"}

    async def scenario():
        async def model(history, *, session_id, emit):
            nonlocal attempted
            session = manager.sessions[session_id]
            if session.record.get("flow_project_id"):
                return assistant("父节点已完成。")
            catalogs.append(copy.deepcopy(selected_schemas(TOOL_SCHEMAS)))
            if not attempted:
                attempted = True
                return assistant(calls=[call("unapproved-descendant", "descendant_selector", id="case-1")])
            return assistant("已核对实际回执。")

        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        projects = ProjectService(manager, service)
        try:
            project = projects.create("后代边界验证", str(workspace), permission_policy="full_access",
                                      execution_environment="sandbox")
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "parent", "title": "父节点", "prompt": "准备后代能力验证",
                              "tool_names": [spec["name"]], "custom_tools": [spec]}]
            flow["edges"] = []
            service.save(flow)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            run = service.get(flow["id"])["run"]
            assert run["status"] == "completed", run
            parent = manager.sessions[run["nodes"]["parent"]["session_id"]]
            assert parent.record["flow_project_id"] == project["id"]
            if descendant_kind == "child":
                descendant = manager.create("真实子会话", "验证继承范围", parent=parent)
                # 与 AgentTasks 的已选能力继承记录一致，不增加权限或自定义执行入口。
                descendant.record.update(custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                    inherited_custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                    tool_names=[spec["name"]], inherited_tool_names=[spec["name"]],
                    permission_policy=parent.record["permission_policy"])
            else:
                descendant = await manager.fork_session(parent, through=0)
            assert not descendant.record.get("flow_project_id")
            assert descendant.record.get("flow_workspace_scope")
            assert manager.flow_workspace_scope(descendant)
            assert spec["name"] in manager._session_custom_registry(descendant)
            manager.submit(descendant, "直接尝试原名，应要求预审。")
            await descendant.task
            assert attempted and catalogs
            assert not (manager.workspace(descendant) / "descendant-executed").exists(), "后代原名未预审不得执行业务"
            names = {row["function"]["name"] for row in catalogs[0]}
            previews = [row for row in catalogs[0]
                        if spec["name"] in row["function"].get("description", "")]
            assert spec["name"] not in names
            assert len(previews) == 1, "Flow 后代的选择器也必须提供草稿 schema"
            assert previews[0]["function"]["parameters"] == spec["parameters"]
            assert_paired(descendant.record)
            receipt = next(row for row in descendant.record["messages"]
                           if row.get("tool_call_id") == "unapproved-descendant")
            assert receipt["_provenance"] == "not_executed"
            assert "[退出码] 0" not in receipt["content"]
            assert not descendant.tool_workers and not manager._active_tools
        finally:
            await service.shutdown()
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("structured", [False, True], ids=["legacy", "structured"])
def test_approved_action_early_handoff_gets_explicit_commit_opportunity(runtime_store, monkeypatch, structured):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    record, run, reviews, schemas, writes, *_ = asyncio.run(
        staged_scenario(runtime_store, monkeypatch, "early_handoff"))
    assert run["status"] == "completed", run
    expected = {"reason": CORRECTED, "item_ref": "case-1"}
    assert writes == [expected], "获批后提前交接须引导下一轮显式提交，不能由框架自动实写"
    assert json.loads((runtime_store[1] / "staged-record.json").read_text(encoding="utf-8")) == [expected]
    assert len(reviews) == 2, "正式提交不能重复语义审查"
    assert_paired(record)
    formal_calls = [entry for row in record["messages"] for entry in row.get("tool_calls", [])
                    if entry["function"]["name"] == "record_case"]
    assert len(formal_calls) == 1
    assert json.loads(formal_calls[0]["function"]["arguments"]) == expected
    formal_receipt = next(row for row in record["messages"] if row.get("tool_call_id") == formal_calls[0]["id"])
    assert formal_receipt["_provenance"] == "executed" and CORRECTED in formal_receipt["content"]
    assert record["last_run"]["model_rounds"] == len(schemas) + len(reviews) <= config.MAX_TOOL_ROUNDS
    assert run["nodes"]["a"]["output_handoff"].endswith("已读取正式执行回执，本节点完成。")


@pytest.mark.parametrize("structured", [False, True], ids=["legacy", "structured"])
def test_repeated_early_handoff_is_bounded_without_automatic_commit(runtime_store, monkeypatch, structured):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    record, run, reviews, schemas, writes, *_ = asyncio.run(
        staged_scenario(runtime_store, monkeypatch, "ignore_handoff"))
    assert len(schemas) >= 5, "首次提前交接应有下一轮补交机会"
    assert record["status"] == "checkpoint" and run["status"] != "completed"
    assert writes == [] and not (runtime_store[1] / "staged-record.json").exists()
    assert len(reviews) == 2
    assert record["last_run"]["model_rounds"] == len(schemas) + len(reviews) <= config.MAX_TOOL_ROUNDS
    assert not any(entry["function"]["name"] == "record_case"
                   for row in record["messages"] for entry in row.get("tool_calls", []))
    assert_paired(record)
