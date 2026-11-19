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
from agent_core.core import flow_draft_gate
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
REVIEW_REVISED = dict(CORRECT, notes="已读取两项技术故障记录。" + EXPLANATION)


def ticket_tool():
    """工具契约明确反馈编号与独立工单号的区别。"""
    return {"name": "submit_ticket", "description": "将模拟工单写入本地账本，返回记录对象，无独立工单号",
            "parameters": {"type": "object", "properties": {
                "feedback_id": {"type": "string"}, "diagnosis": {"type": "string"}},
                "required": ["feedback_id", "diagnosis"], "additionalProperties": False},
            "code": "def main(arguments):\n    return {'ok': True, 'ticket': arguments}\n"}


async def receipt_scenario(runtime_store, monkeypatch, *, mode="success", policy="full_access",
                           rounds=12, classic=False, observations=None, followup=False, tamper=None):
    """仅隔离正式业务入口；预审、确认、配对、Flow 验收均走生产代码。"""
    store, project = runtime_store
    source_root = Path(__file__).resolve().parents[2]
    for module in (config, sys.modules[SessionManager.__module__], sys.modules[FlowService.__module__]):
        assert Path(module.__file__).resolve().is_relative_to(source_root), "必须验证当前树源码，不能误用主仓库 editable 包"
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", rounds)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    monkeypatch.setattr(config, "SESSION_COST_LIMIT", 1.0)
    if observations is not None:
        # 受控分类接缝强制抵达独立模型复核，后续组装、来源、判定与发布仍走生产代码。
        original_needs_review = flow_draft_gate.needs_review
        monkeypatch.setattr(flow_draft_gate, "needs_review", lambda draft: (
            '"diagnoses"' in draft or original_needs_review(draft)))
    requests, writes, errors, repairs, downstream = [], [], [], [], []
    normal_index = 0
    preview = None
    followup_active = False
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
        if followup_active:
            return assistant(json.dumps(ORIGINAL, ensure_ascii=False))
        if session.record.get("flow_node_id") == "next" or (
                any("下游核对" in str(row.get("content")) for row in history if row["role"] == "user")):
            downstream.append(copy.deepcopy(history))
            assert len(writes) == 2
            return assistant("下游核对完成")
        payload = review_payload(history)
        if payload is None and len(history) == 2 and history[0]["role"] == "system":
            try:
                candidate = json.loads(history[1]["content"])
            except (TypeError, ValueError):
                candidate = None
            if isinstance(candidate, dict) and {"draft", "sources"} <= candidate.keys():
                payload = candidate
        if payload is not None:
            assert names == set()
            if observations is not None:
                observations.setdefault("reviews", []).append(copy.deepcopy(payload))
            if mode in {"receipt_patch_review_timeout", "receipt_patch_review_cancel"} and len(writes) == 2:
                if mode.endswith("cancel"):
                    session.cancelled.set()
                    raise asyncio.CancelledError
                raise TimeoutError("受控完整交接语义审查超时")
            if (mode in {"receipt_patch_review_revise", "receipt_patch_review_revise_success"}
                    and len(writes) == 2 and json.loads(payload["draft"]) == CORRECT):
                source = payload["sources"][0]
                issue = {"reason": "来源只证明已读取故障记录，不能宣称已完成排查", "source_id": source["id"],
                         "quote": source["text"]}
                if payload.get("targets"):
                    target = next((row for row in payload["targets"] if "已完成排查" in row["excerpt"]),
                                  payload["targets"][0])
                    issue.update(target_id=target["id"], draft_quote=target["excerpt"])
                else:
                    issue["field"] = "notes"
                return assistant(json.dumps({"status": "revise", "issues": [issue]}, ensure_ascii=False))
            return assistant('{"status":"pass"}')
        if mode in {"receipt_patch_review_revise", "receipt_patch_review_revise_success"} and any(
                "仅修订上一份交接" in row.get("content", "") for row in history if row.get("role") == "user"):
            observations["semantic_revision_history"] = copy.deepcopy(history)
            if mode == "receipt_patch_review_revise_success":
                assert names == set(), "语义修订不能重开业务工具"
                return assistant(json.dumps(REVIEW_REVISED, ensure_ascii=False))
            invalid_full = copy.deepcopy(CORRECT)
            invalid_full["submitted_ticket_ids"] = ["CF-01", "CF-02"]
            return assistant(json.dumps(invalid_full, ensure_ascii=False))
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
            if mode.startswith("receipt_patch"):
                patch = {"set": {"submitted_ticket_ids": []}, "append_notes": EXPLANATION}
                if mode == "receipt_patch_wrong_ids":
                    patch["set"]["submitted_ticket_ids"] = ["CF-01", "CF-02"]
                if mode == "receipt_patch_protected":
                    patch["set"]["diagnoses"] = []
                if mode == "receipt_patch_replace_notes":
                    patch["notes"] = EXPLANATION
                if mode == "receipt_patch_missing_ids":
                    patch["set"] = {}
                if mode == "receipt_patch_string_ids":
                    patch["set"]["submitted_ticket_ids"] = ""
                if mode == "receipt_patch_nonstring_notes":
                    patch["append_notes"] = [EXPLANATION]
                if mode == "receipt_patch_duplicate_key":
                    return assistant('{"set":{"submitted_ticket_ids":["CF-01"],"submitted_ticket_ids":[]},'
                                     '"append_notes":' + json.dumps(EXPLANATION, ensure_ascii=False) + '}')
                return assistant(json.dumps(patch, ensure_ascii=False))
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
    original_drain = service._drain

    async def tampered_drain(session, *args, **kwargs):
        await original_drain(session, *args, **kwargs)
        derived = session.record.get("last_run", {}).get("handoff_derived")
        if tamper and derived and not observations.get("tampered"):
            index = derived["original_index" if tamper == "original" else "reply_index"]
            changed = copy.deepcopy(ORIGINAL) if tamper == "original" else {
                "set": {"submitted_ticket_ids": []}, "append_notes": EXPLANATION + "额外未审说明。"}
            if tamper == "original":
                changed["diagnoses"][0]["diagnosis"] = "已经修复故障"
            session.record["messages"][index]["content"] = json.dumps(changed, ensure_ascii=False)
            observations["tampered"] = True

    if tamper:
        monkeypatch.setattr(service, "_drain", tampered_drain)
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
        if observations is not None:
            observations["first_record"] = copy.deepcopy(session.record)
        if followup:
            # 同一会话的新普通轮次没有本轮办理回执，不得借用上轮派生交接。
            followup_active = True
            manager.submit(session, "新一轮普通技术诊断", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert_paired(session.record)
            observations["followup_record"] = copy.deepcopy(session.record)
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


def test_model_repairs_only_issued_fields_without_copying_diagnoses(runtime_store, monkeypatch):
    record, run, requests, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch"))
    assert len(repairs) == 1 and repairs[0]["tools"] == set() and writes == DIAGNOSES
    assert run["status"] == "completed" and len(downstream) == 1
    assert json.loads(run["nodes"]["tech"]["output_handoff"]) == CORRECT
    assert record["last_run"]["handoff_finalization"]["status"] == "passed"
    assert repairs[0]["budget"] is requests[0]["budget"]
    assert repairs[0]["session_id"] == requests[0]["session_id"]
    # 普通节点的系统指令要求输出整份交接，修正轮需要明确自身的局部输出格式。
    system_messages = [m["content"] for m in repairs[0]["history"] if m.get("role") == "system"]
    assert any('"set"' in text and '"append_notes"' in text for text in system_messages)
    assert not any('"append_notes"' in m.get("content", "") for m in record["messages"] if m.get("role") == "system")
    model_patches = [json.loads(m["content"]) for m in record["messages"]
                     if m.get("role") == "assistant" and '"append_notes"' in (m.get("content") or "")]
    assert model_patches == [{"set": {"submitted_ticket_ids": []}, "append_notes": EXPLANATION}]
    decoder = json.JSONDecoder()
    assert any(decoder.raw_decode(text[index:])[0] == CORRECT
               for m in downstream[0] if isinstance((text := m.get("content")), str)
               for index in range(len(text)) if text[index] == "{"
               and text[index:].startswith('{"diagnoses"'))


@pytest.mark.parametrize("mode", ["receipt_patch_wrong_ids", "receipt_patch_protected",
                                  "receipt_patch_replace_notes", "receipt_patch_missing_ids"])
def test_model_receipt_patch_cannot_bypass_receipts_or_protected_fields(runtime_store, monkeypatch, mode):
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode=mode))
    assert len(repairs) == 1 and repairs[0]["tools"] == set() and writes == DIAGNOSES
    assert not downstream and run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""
    assert record["last_run"]["handoff_finalization"]["status"] == "failed"


def test_receipt_patch_keeps_original_messages_and_reviews_complete_business_object(runtime_store, monkeypatch):
    observations = {}
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch", observations=observations))
    assert run["status"] == "completed" and len(downstream) == 1 and writes == DIAGNOSES
    texts = [row.get("content") for row in record["messages"] if row.get("role") == "assistant"]
    assert json.dumps(ORIGINAL, ensure_ascii=False) in texts
    patch = {"set": {"submitted_ticket_ids": []}, "append_notes": EXPLANATION}
    assert json.dumps(patch, ensure_ascii=False) in texts
    derived = record["last_run"]["handoff_derived"]
    assert derived["turn_id"] == record["last_run"]["turn_id"]
    assert record["messages"][derived["original_index"]]["content"] == json.dumps(ORIGINAL, ensure_ascii=False)
    assert record["messages"][derived["reply_index"]]["content"] == json.dumps(patch, ensure_ascii=False)
    assert derived["original_index"] < derived["reply_index"]
    assert len(repairs) == 1
    complete_reviews = [row for row in observations["reviews"] if EXPLANATION in row["draft"]]
    assert complete_reviews, "独立语义审查必须实际收到追加说明后的完整交接"
    assert all(json.loads(row["draft"]) == CORRECT for row in complete_reviews)
    assert all(row["sources"] for row in complete_reviews)


@pytest.mark.parametrize("mode", ["receipt_patch_review_timeout", "receipt_patch_review_cancel"])
def test_receipt_patch_review_failure_never_publishes_partial_handoff(runtime_store, monkeypatch, mode):
    observations = {}
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode=mode, observations=observations))
    assert len(repairs) == 1 and writes == DIAGNOSES
    assert any(json.loads(row["draft"]) == CORRECT for row in observations.get("reviews", []))
    assert not downstream and run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""
    assert not record["last_run"].get("handoff_derived"), "审查未通过不能发布可被 Flow 消费的派生结果"


def test_receipt_patch_semantic_revision_rejects_wrong_full_object_without_old_fallback(runtime_store, monkeypatch):
    observations = {}
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch_review_revise", observations=observations))
    assert len(repairs) == 1 and writes == DIAGNOSES
    assert any(json.loads(row["draft"]) == CORRECT for row in observations["reviews"])
    revision = observations.get("semantic_revision_history")
    assert revision, "必须实际抵达有效语义 revise 后的主模型修订轮"
    assert EXPLANATION in revision[-1]["content"] and "diagnoses" in revision[-1]["content"]
    assert not downstream and run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""
    assert not record["last_run"].get("handoff_derived")


def test_receipt_patch_semantic_revision_publishes_new_full_handoff_after_recheck(runtime_store, monkeypatch):
    observations = {}
    record, run, requests, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch_review_revise_success", observations=observations))
    assert run["status"] == "completed" and len(downstream) == 1
    assert len(repairs) == 1 and writes == DIAGNOSES
    reviews = [json.loads(row["draft"]) for row in observations["reviews"]
               if "diagnoses" in json.loads(row["draft"])]
    assert reviews == [CORRECT, REVIEW_REVISED], "修订前后完整业务对象必须分别得到独立判定"
    assert observations.get("semantic_revision_history")
    assert json.loads(run["nodes"]["tech"]["output_handoff"]) == REVIEW_REVISED
    assert not record["last_run"].get("handoff_derived"), "语义修订新全稿通过后不能发布旧局部组装值"
    assert json.loads(record["messages"][-1]["content"]) == REVIEW_REVISED
    assert json.dumps(ORIGINAL, ensure_ascii=False) in [row.get("content") for row in record["messages"]]
    receipts = [row for row in record["messages"] if row.get("tool_call_id", "").startswith("commit-")]
    assert len(receipts) == 2 and all(row["_provenance"] == "executed" for row in receipts)
    tech_requests = [row for row in requests if row["session_id"] == repairs[0]["session_id"]]
    for row in tech_requests:
        for key in ("turn_id", "endpoint", "effort", "thinking"):
            assert row[key] == repairs[0][key]
        assert row["budget"] is repairs[0]["budget"]
    decoder = json.JSONDecoder()
    received = [decoder.raw_decode(text[index:])[0]
                for message in downstream[0] if isinstance((text := message.get("content")), str)
                for index in range(len(text)) if text[index:].startswith('{"diagnoses"')]
    assert REVIEW_REVISED in received and CORRECT not in received


def test_receipt_patch_is_not_reused_by_next_ordinary_turn(runtime_store, monkeypatch):
    observations = {}
    _, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch", observations=observations, followup=True))
    assert run["status"] == "completed" and len(downstream) == 1
    assert len(repairs) == 1 and writes == DIAGNOSES
    first = observations["first_record"]["last_run"]
    second = observations["followup_record"]["last_run"]
    assert first["turn_id"] != second["turn_id"]
    assert second["handoff_validation_failed"]
    assert not second.get("handoff_derived")


@pytest.mark.parametrize("tamper", ["original", "patch"])
def test_receipt_patch_rejects_messages_changed_after_review_before_flow_publish(
        runtime_store, monkeypatch, tamper):
    observations = {}
    _, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode="receipt_patch", observations=observations, tamper=tamper))
    assert observations.get("tampered"), "受控替换必须发生在完整交接通过来源审查之后"
    assert len(repairs) == 1 and writes == DIAGNOSES
    assert any(json.loads(row["draft"]) == CORRECT for row in observations["reviews"])
    assert not downstream and run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""


@pytest.mark.parametrize("mode", ["receipt_patch_string_ids", "receipt_patch_nonstring_notes",
                                  "receipt_patch_duplicate_key"])
def test_receipt_patch_rejects_ambiguous_or_invalid_value_types(runtime_store, monkeypatch, mode):
    record, run, _, repairs, writes, downstream = asyncio.run(receipt_scenario(
        runtime_store, monkeypatch, mode=mode))
    assert len(repairs) == 1 and repairs[0]["tools"] == set() and writes == DIAGNOSES
    assert not downstream and run["nodes"]["next"]["status"] == "blocked"
    assert run["nodes"]["tech"]["output_handoff"] == ""
    assert record["last_run"]["handoff_finalization"]["status"] == "failed"
