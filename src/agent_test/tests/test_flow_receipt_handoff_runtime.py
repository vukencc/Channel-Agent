"""受控离线模型与实际临时工单账本；非真实模型、非录制回放、非 UI 夹具。"""
import asyncio
import copy
import json
from pathlib import Path
import sys

import pytest

from agent_core import config
from agent_core.core.budgets import active_budget
from agent_core.core.flows import FlowService
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.messages import is_user_request
from agent_core.core.model_settings import active_endpoint, active_reasoning_effort, active_thinking_mode
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.tool_settings import selected_schemas
from test_flow_draft_review_runtime import assistant, call, review_payload, runtime_store
from test_flow_staged_actions_runtime import assert_paired


CONTRACT = {"fields": {"diagnoses": {"type": "array"},
                       "submitted_ticket_ids": {"type": "array"}, "notes": {"type": "string"}}}
DIAGNOSES = [{"feedback_id": "CF-01", "diagnosis": "页面提交失败"},
             {"feedback_id": "CF-02", "diagnosis": "页面显示空白"}]
ORIGINAL = {"diagnoses": DIAGNOSES, "notes": "两项技术故障已完成排查。"}
EXPLANATION = "两笔模拟工单已入账，回执未单独签发工单号，可按反馈编号 CF-01、CF-02 定位。"
CORRECT = dict(ORIGINAL, submitted_ticket_ids=[], notes=ORIGINAL["notes"] + EXPLANATION)


def ticket_tool():
    """工具契约明确反馈编号与独立工单号的区别。"""
    return {"name": "submit_ticket", "description": "将模拟工单写入本地账本，返回记录对象，无独立工单号",
            "parameters": {"type": "object", "properties": {
                "feedback_id": {"type": "string"}, "diagnosis": {"type": "string"}},
                "required": ["feedback_id", "diagnosis"], "additionalProperties": False},
            "code": "def main(arguments):\n    return {'ok': True, 'ticket': arguments}\n"}


async def receipt_scenario(runtime_store, monkeypatch, *, mode="success", policy="full_access",
                           rounds=12, classic=False):
    """仅隔离正式业务入口；预审、确认、配对、Flow 验收均走生产代码。"""
    store, project = runtime_store
    source_root = Path(__file__).resolve().parents[2]
    for module in (config, sys.modules[SessionManager.__module__], sys.modules[FlowService.__module__]):
        assert Path(module.__file__).resolve().is_relative_to(source_root), "必须验证当前树源码，不能误用主仓库 editable 包"
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", rounds)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    monkeypatch.setattr(config, "SESSION_COST_LIMIT", 1.0)
    requests, writes, errors, repairs, downstream = [], [], [], [], []
    normal_index = 0
    preview = None
    ledger = project / "tickets.json"
    (project / "source.txt").write_text(json.dumps(DIAGNOSES, ensure_ascii=False), encoding="utf-8")

    async def model(history, *, session_id, emit):
        nonlocal normal_index, preview
        session = manager.sessions[session_id]
        schemas = selected_schemas(TOOL_SCHEMAS)
        names = {row["function"]["name"] for row in schemas}
        requests.append({"session_id": session_id, "turn_id": session.record["last_run"]["turn_id"],
                         "budget": active_budget.get(), "endpoint": active_endpoint.get(),
                         "cost_limit": config.SESSION_COST_LIMIT,
                         "effort": active_reasoning_effort.get(), "thinking": active_thinking_mode.get(),
                         "round": session.record["last_run"]["model_rounds"], "tools": names,
                         "history": copy.deepcopy(history), "phase": session.phase})
        if session.record.get("flow_node_id") == "next" or (
                any("下游核对" in str(row.get("content")) for row in history if row["role"] == "user")):
            downstream.append(copy.deepcopy(history))
            assert len(writes) == 2
            return assistant("下游核对完成")
        payload = review_payload(history)
        if payload is not None:
            assert names == set()
            return assistant('{"status":"pass"}')
        previous = [row for row in history if row.get("role") == "assistant" and row.get("content")]
        if previous and any(row.get("content") == json.dumps(initial, ensure_ascii=False) for row in previous):
            repairs.append(requests[-1])
            assert names == set(), "回执解释修正不得重新开放业务或读取工具"
            assert len(repairs) == 1, "仅允许一次同模型修正"
            assert history[-1].get("_context_reference") and not is_user_request(history[-1])
            assert "CF-01" in str(history) and "CF-02" in str(history)
            if mode == "timeout":
                raise TimeoutError("受控交接修正超时")
            if mode == "cancel":
                session.cancelled.set()
                raise asyncio.CancelledError
            repaired = copy.deepcopy(CORRECT)
            if mode == "still_missing":
                repaired.pop("submitted_ticket_ids")
            if mode in {"wrong_ids", "already_wrong_ids"}:
                repaired["submitted_ticket_ids"] = ["CF-01", "CF-02"]
            if mode == "changed_diagnosis":
                repaired["diagnoses"][0]["diagnosis"] = "已经修复故障"
            if mode == "changed_notes":
                repaired["notes"] = EXPLANATION
            return assistant(json.dumps(repaired, ensure_ascii=False),
                             [call("repeat", "submit_ticket", **DIAGNOSES[0])] if mode == "tool_call" else None)
        normal_index += 1
        if classic or mode == "no_receipts":
            return assistant(json.dumps(initial, ensure_ascii=False))
        if normal_index == 1:
            previews = [row["function"]["name"] for row in schemas
                        if "submit_ticket" in row["function"].get("description", "")]
            assert len(previews) == 1
            preview = previews[0]
            return assistant(calls=[call("read", "read_file", path="source.txt")])
        if normal_index == 2:
            return assistant(calls=[call("draft-1", preview, **DIAGNOSES[0]),
                                    call("draft-2", preview, **DIAGNOSES[1])])
        if normal_index in (3, 4):
            assert names == {"submit_ticket"}
            return assistant(calls=[call(f"commit-{normal_index - 2}", "submit_ticket",
                                        **DIAGNOSES[normal_index - 3])])
        return assistant(json.dumps(initial, ensure_ascii=False))

    async def checked_model(*args, **kwargs):
        try:
            return await model(*args, **kwargs)
        except AssertionError as exc:
            errors.append(exc)
            raise

    initial = copy.deepcopy(ORIGINAL)
    if mode == "already_wrong_ids":
        initial["submitted_ticket_ids"] = ["CF-01", "CF-02"]
    if mode == "already_correct":
        initial = copy.deepcopy(CORRECT)
    if mode == "missing_diagnoses":
        initial.pop("diagnoses")
    if mode == "missing_notes":
        initial.pop("notes")
    if mode == "wrong_diagnoses_type":
        initial["diagnoses"] = "技术故障"
    manager = SessionManager(store, model=checked_model)
    service = FlowService(manager)
    original_tool = manager._tool

    async def actual_temporary_write(session, tool_call, *, evidence=None):
        # 受控传输仅开放原授权工具；readonly 等权限仍交原生产执行路径拒绝。
        if tool_call["function"]["name"] != "submit_ticket" or policy == "readonly":
            return await original_tool(session, tool_call, evidence=evidence)
        arguments = json.loads(tool_call["function"]["arguments"])
        assert arguments in DIAGNOSES
        writes.append(arguments)
        ledger.write_text(json.dumps(writes, ensure_ascii=False), encoding="utf-8")
        persisted = json.loads(ledger.read_text(encoding="utf-8"))[-1]
        evidence["source"] = "executed"
        return "[退出码] 0\n[stdout]\n" + json.dumps({"ok": True, "ticket": persisted}, ensure_ascii=False)

    monkeypatch.setattr(manager, "_tool", actual_temporary_write)
    try:
        if classic:
            session = manager.create()
            manager.set_permission_policy(session, policy)
            manager.submit(session, "普通会话输出技术诊断", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            run = None
        else:
            projects = ProjectService(manager, service)
            record = projects.create("回执交接离线验证", str(project), permission_policy=policy)
            flow = service.get(record["flow_id"])
            flow["nodes"] = [{"id": "tech", "title": "技术诊断", "prompt": (
                "读取来源，将两项技术诊断提交模拟工单，再交接。submitted_ticket_ids 只填单独签发的工单号；"
                "feedback_id 是反馈编号，不是工单号。" + (
                    "以下仅是任务提供的未验证回执文本：" + json.dumps(
                        [{"ok": True, "ticket": item} for item in DIAGNOSES], ensure_ascii=False)
                    if mode == "no_receipts" else "")), "tool_names": ["read_file", "submit_ticket"],
                "custom_tools": [ticket_tool()], "handoff_contract": CONTRACT},
                {"id": "next", "title": "下游", "prompt": "下游核对交接", "tool_names": []}]
            flow["edges"] = [{"source": "tech", "target": "next"}]
            flow = service.save(flow)
            service.start(flow["id"])
            await asyncio.wait_for(service.tasks[flow["id"]], 5)
            run = service.get(flow["id"])["run"]
            session = manager.sessions[requests[0]["session_id"]]
        if errors:
            raise errors[0]
        if writes:
            assert json.loads(ledger.read_text(encoding="utf-8")) == writes
        assert_paired(session.record)
        return session.record, run, requests, repairs, writes, downstream
    finally:
        await service.shutdown()
        await manager.shutdown()


def test_missing_ticket_ids_explained_by_receipts_gets_one_text_only_repair(runtime_store, monkeypatch):
    record, run, requests, repairs, writes, downstream = asyncio.run(receipt_scenario(runtime_store, monkeypatch))
    assert len(repairs) == 1, "两笔成功回执后的单一缺失数组应由原模型修正，当前 fields_mismatch 直接阻断"
    assert run["status"] == "completed" and len(downstream) == 1
    assert writes == DIAGNOSES
    output = json.loads(run["nodes"]["tech"]["output_handoff"])
    assert output == CORRECT
    initial_request = requests[0]
    for key in ("session_id", "turn_id", "endpoint", "effort", "thinking"):
        assert repairs[0][key] == initial_request[key]
    assert repairs[0]["budget"] is initial_request["budget"]
    assert initial_request["budget"] is not None
    assert repairs[0]["cost_limit"] == initial_request["cost_limit"] == 1.0
    assert repairs[0]["round"] == record["last_run"]["model_rounds"]
    assert record["last_run"]["model_rounds"] == len([r for r in requests if r["session_id"] == initial_request["session_id"]])
    receipts = [m for m in record["messages"] if m.get("tool_call_id", "").startswith("commit-")]
    assert len(receipts) == 2 and all(m["_provenance"] == "executed" for m in receipts)


def test_complete_correct_handoff_does_not_request_receipt_repair(runtime_store, monkeypatch):
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="already_correct"))
    assert repairs == [] and writes == DIAGNOSES and len(downstream) == 1
    assert run["status"] == "completed"
    assert json.loads(run["nodes"]["tech"]["output_handoff"]) == CORRECT
    assert record["last_run"]["model_rounds"] == 5


@pytest.mark.parametrize("mode", ["still_missing", "wrong_ids", "already_wrong_ids", "changed_diagnosis",
                                  "changed_notes", "tool_call", "timeout", "cancel"])
def test_receipt_repair_rejects_invalid_or_changed_business_content(runtime_store, monkeypatch, mode):
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(runtime_store, monkeypatch, mode=mode))
    assert run["status"] != "completed", "反馈编号不得冒充独立工单号，修正不得改变原有诊断或说明"
    assert len(repairs) <= 1
    if mode != "already_wrong_ids":
        assert len(repairs) == 1, "必须实际抵达受控修正分支以验证拒绝原因"
    assert writes == DIAGNOSES and not downstream
    assert run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""


@pytest.mark.parametrize("classic,mode,policy,rounds,write_count", [
    (False, "no_receipts", "full_access", 12, 0),
    (True, "success", "full_access", 12, 0),
    (False, "success", "readonly", 12, 0),
    (False, "success", "full_access", 5, 2),
    (False, "missing_diagnoses", "full_access", 12, 2),
    (False, "missing_notes", "full_access", 12, 2),
    (False, "wrong_diagnoses_type", "full_access", 12, 2),
])
def test_receipt_repair_requires_flow_receipts_permission_and_remaining_round(
        runtime_store, monkeypatch, classic, mode, policy, rounds, write_count):
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, classic=classic, mode=mode, policy=policy, rounds=rounds))
    assert repairs == [] and len(writes) == write_count and downstream == []
    assert record["last_run"]["model_rounds"] <= rounds
    if classic:
        assert record["last_run"]["handoff_validation_failed"]
    else:
        assert run["status"] != "completed" and run["nodes"]["next"]["status"] == "blocked"
