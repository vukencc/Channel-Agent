"""通过真实 Flow 入口验证按事项复核，模型回调为受控夹具。"""
import asyncio
import copy
import json
import sys
import types

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.flows import FlowService
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.tool_settings import selected_schemas
from agent_test.tests.test_flow_draft_review_runtime import (
    CORRECTED, DISPUTED, assistant, call, review_payload, runtime_store, scenario,
)


def test_handoff_reviews_each_item(runtime_store, monkeypatch):
    draft = json.dumps({"items": [{"id": "a", "reason": "客户明确要求处理。"},
                                  {"id": "b", "reason": "客户明确要求排查。"}]}, ensure_ascii=False)

    def units(draft, sources, *, required_ids=frozenset()):
        data = json.loads(draft)
        return [{"item_id": item["id"], "draft": json.dumps(item, ensure_ascii=False),
                 "sources": sources} for item in data["items"]]

    monkeypatch.setitem(sys.modules, "agent_core.core.flow_review_items", types.SimpleNamespace(review_units=units))
    record, result, _, reviews, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="pass", final_draft=draft))
    assert len(reviews) == 2
    assert [json.loads(row["draft"])["id"] for row in reviews] == ["a", "b"]
    assert len(writes) == 1
    assert record["last_run"]["draft_reviews"][-1]["status"] == "passed"


def test_rejected_item_is_rechecked_and_unchanged_pass_is_reused(runtime_store, monkeypatch):
    draft = json.dumps({"items": [{"id": "a", "reason": CORRECTED + DISPUTED},
                                  {"id": "b", "reason": DISPUTED}]}, ensure_ascii=False)
    required_seen = []

    def units(current, sources, *, required_ids=frozenset()):
        required_seen.append(required_ids)
        if current == CORRECTED:
            items = [{"id": "a", "reason": CORRECTED + DISPUTED},
                     {"id": "b", "reason": CORRECTED}]
        else:
            items = json.loads(current)["items"]
        return [{"item_id": item["id"], "draft": json.dumps(item, ensure_ascii=False),
                 "sources": sources} for item in items]

    monkeypatch.setitem(sys.modules, "agent_core.core.flow_review_items", types.SimpleNamespace(review_units=units))
    record, result, _, reviews, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, final_draft=draft))
    assert required_seen == [frozenset(), frozenset({"b"})]
    assert [json.loads(row["draft"])["id"] for row in reviews] == ["a", "b", "b"]
    assert len(writes) == 1
    events = record["last_run"]["draft_reviews"]
    assert [event["status"] for event in events] == ["revise", "passed"]
    assert [unit["status"] for unit in events[-1]["units"]] == ["reused", "passed"]
    assert events[-1]["attempts"] == ["passed"]


def test_timeout_recovery_is_shared_and_unapproved_handoff_is_blocked(runtime_store, monkeypatch):
    draft = json.dumps({"items": [{"id": "a", "reason": DISPUTED},
                                  {"id": "b", "reason": DISPUTED}]}, ensure_ascii=False)
    def units(current, sources, *, required_ids=frozenset()):
        return [{"item_id": item["id"], "draft": json.dumps(item, ensure_ascii=False),
                 "sources": sources} for item in json.loads(current)["items"]]

    monkeypatch.setitem(sys.modules, "agent_core.core.flow_review_items", types.SimpleNamespace(review_units=units))
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    original = SessionManager._call_model
    calls = []

    async def controlled(self, history, **kwargs):
        if review_payload(history):
            calls.append(1)
            if len(calls) in (1, 3):
                raise TimeoutError("受控超时")
        return await original(self, history, **kwargs)

    monkeypatch.setattr(SessionManager, "_call_model", controlled)
    record, result, _, _, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="pass", final_draft=draft))
    assert len(calls) == 3
    assert len(writes) == 1 and result["status"] != "completed"
    assert record["last_run"]["recovery_attempts"] == 1
    event = record["last_run"]["draft_reviews"][-1]
    assert event["status"] == "TimeoutError"
    assert event["attempts"] == ["TimeoutError", "passed", "TimeoutError"]


async def real_projection_scenario(runtime_store, monkeypatch, mode):
    """实际分割器与持久化 Flow；只有模型为受控回调。"""
    store, project = runtime_store
    rows = [{"id": f"F{i:03}", "text": f"客户反馈第{i}项页面问题。"} for i in range(1, 8)]
    source = json.dumps({"ok": True, "path": "feedback.json", "total": 7, "count": 7,
                         "skipped": 0, "items": rows}, ensure_ascii=False)
    (project / "receipt.txt").write_text("已接收资料。", encoding="utf-8")
    grouped = {"source_path": "feedback.json", "technical": copy.deepcopy(rows[:2]),
               "service": copy.deepcopy(rows[2:4]), "account": copy.deepcopy(rows[4:6]),
               "other": copy.deepcopy(rows[6:])}
    grouped["technical"][0]["reason"] = "客户明确要求人工处理。"
    if mode == "round_budget":
        grouped["technical"][1]["reason"] = "客户明确要求人工处理。"
    first_draft = json.dumps(grouped, ensure_ascii=False)
    revised = copy.deepcopy(grouped)
    if mode == "need_source":
        revised["technical"][0]["reason"] = "建议安排排查。"
    final_draft = json.dumps(revised, ensure_ascii=False)
    reviews, normal = [], 0

    async def model(history, *, session_id, emit):
        nonlocal normal
        payload = review_payload(history)
        if payload is not None:
            assert "当前事项" in history[0]["content"]
            assert "共享备注" in history[0]["content"]
            assert selected_schemas(TOOL_SCHEMAS) == []
            reviews.append(payload)
            if mode == "cross_quote" and len(reviews) == 1:
                # 引用另一事项，复核协议必须拒绝此引文。
                return assistant(json.dumps({"status": "revise", "issues": [{
                    "field": "reason", "reason": "检查客户原话", "source_id": "U1",
                    "quote": rows[1]["text"]}]}, ensure_ascii=False))
            if mode == "need_source" and len(reviews) == 1:
                return assistant(json.dumps({"status": "need_source", "field": "reason",
                                             "needed": "原始客户反馈"}, ensure_ascii=False))
            return assistant('{"status":"pass"}')
        normal += 1
        if normal == 1:
            return assistant(calls=[call("receipt", "read_file", path="receipt.txt")])
        if normal == 2:
            return assistant(first_draft)
        if mode == "cross_quote":
            assert "审查结果无效或来源不足" in history[-1]["content"]
        return assistant(final_draft)

    manager = SessionManager(store, model=model)
    service = FlowService(manager)
    try:
        flow = service.create(workspace_path=str(project), permission_policy="readonly")
        flow["nodes"] = [{"id": "a", "title": "分类", "prompt": "分类反馈并保留原文。",
                          "tool_names": ["read_file"]}]
        flow["edges"] = []
        flow = service.save(flow)
        if mode == "round_budget":
            original_submit = manager.submit
            def submit(session, *args, **kwargs):
                manager.set_limits(session, {"MAX_TOOL_ROUNDS": 3})
                return original_submit(session, *args, **kwargs)
            monkeypatch.setattr(manager, "submit", submit)
        service.start(flow["id"], input_text=source)
        await service.tasks[flow["id"]]
        session = next(iter(manager.sessions.values()))
        persisted = json.loads((store.directory(session.id) / "session.json").read_text(encoding="utf-8"))
        return service.get(flow["id"])["run"], persisted, reviews, first_draft, final_draft, rows
    finally:
        await service.shutdown()
        await manager.shutdown()


def test_actual_projection_rejects_other_item_quote_and_preserves_complete_handoff(runtime_store, monkeypatch):
    result, persisted, reviews, first, final, rows = asyncio.run(real_projection_scenario(
        runtime_store, monkeypatch, "cross_quote"))
    assert len(reviews) == 2
    assert all(json.loads(review["draft"])["technical"][0]["id"] == "F001" for review in reviews)
    assert all(rows[1]["text"] not in row["text"] for row in reviews[0]["sources"])
    original = json.dumps({"ok": True, "path": "feedback.json", "total": 7, "count": 7,
                           "skipped": 0, "items": rows}, ensure_ascii=False)
    projection = next(row["text"] for row in reviews[0]["sources"] if row["id"] == "U1")
    assert rows[0]["text"] in projection and projection in original
    assert result["status"] == "completed"
    assert result["nodes"]["a"]["output_handoff"].endswith(final)
    assert persisted["last_run"]["draft_reviews"][-1]["status"] == "passed"


def test_actual_projection_need_source_forces_modified_item_review(runtime_store, monkeypatch):
    result, persisted, reviews, _, final, _ = asyncio.run(real_projection_scenario(
        runtime_store, monkeypatch, "need_source"))
    assert len(reviews) == 2 and "建议安排排查" in reviews[1]["draft"]
    assert result["status"] == "completed"
    assert result["nodes"]["a"]["output_handoff"].endswith(final)
    assert [event["status"] for event in persisted["last_run"]["draft_reviews"]] == ["revise", "passed"]


def test_actual_projection_round_budget_does_not_publish_partial_handoff(runtime_store, monkeypatch):
    result, persisted, reviews, _, _, _ = asyncio.run(real_projection_scenario(
        runtime_store, monkeypatch, "round_budget"))
    assert len(reviews) == 1
    assert result["status"] != "completed"
    assert not result["nodes"]["a"].get("output_handoff")
    event = persisted["last_run"]["draft_reviews"][-1]
    assert event["status"] == "round_budget"
    assert [unit["status"] for unit in event["units"]] == ["passed", "round_budget"]


def test_disputed_tool_uses_exact_sentence_and_checked_source(runtime_store, monkeypatch):
    record, result, _, reviews, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, item_ref="case-1"))
    first = reviews[0]
    assert [target["excerpt"] for target in first["targets"]] == [DISPUTED]
    target = first["targets"][0]
    assert target["field"] == "/reason"
    receipt = next(m for m in record["messages"] if m.get("tool_call_id") == "write-original")
    assert receipt["_provenance"] == "not_executed" and target["id"] in receipt["content"]
    assert writes == [{"reason": CORRECTED, "item_ref": "case-1"}]
    assert result["status"] == "completed"


def test_unverifiable_review_is_corrected_before_any_business_rework(runtime_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    original = SessionManager._call_model
    reviews = []

    async def wrong_quote(self, history, **kwargs):
        payload = review_payload(history)
        if payload:
            reviews.append(payload)
            return assistant(json.dumps({"status": "revise", "issues": [{
                "target_id": "invented", "draft_quote": "虚构的表述", "source_id": "unknown",
                "quote": "虚构的引文", "reason": "需要修改"}]}, ensure_ascii=False))
        return await original(self, history, **kwargs)

    monkeypatch.setattr(SessionManager, "_call_model", wrong_quote)
    record, result, _, _, writes, _ = asyncio.run(scenario(runtime_store, monkeypatch))
    assert len(reviews) == 2 and reviews[0] == reviews[1]
    assert writes == [] and result["status"] != "completed"
    event = record["last_run"]["draft_reviews"][-1]
    assert event["status"] == "invalid_verdict"
    assert event["attempts"] == ["invalid_verdict", "invalid_verdict"]
    assert record["last_run"]["recovery_attempts"] == 1
    attempts = [m.get("tool_call_id") for m in record["messages"] if m["role"] == "tool"]
    assert "write-original" in attempts and "write-corrected" not in attempts


def test_duplicate_handoff_fields_cannot_hide_a_source_claim(runtime_store, monkeypatch):
    draft = '{"note":"客户明确要求转人工处理。","note":"等待排查"}'
    record, result, _, reviews, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="pass", final_draft=draft))
    assert result["nodes"]["a"]["output_handoff"].endswith(CORRECTED)
    assert draft not in result["nodes"]["a"]["output_handoff"]
    assert writes == [{"reason": CORRECTED}]
    assert [e["status"] for e in record["last_run"]["draft_reviews"]] == ["ambiguous_draft", "passed"]


def test_full_flow_review_receives_complete_focal_sentence_and_context(runtime_store, monkeypatch):
    claim = "客户明确表示" + "同意继续核对之前登记的信息，" * 5 + "并希望继续排查。"
    draft = json.dumps({"summary": "用户反馈页面延迟。", "reason": claim}, ensure_ascii=False)
    record, result, _, reviews, writes, _ = asyncio.run(scenario(
        runtime_store, monkeypatch, final=True, review_mode="pass", final_draft=draft))
    assert len(reviews) == 1
    payload = reviews[0]
    assert json.loads(payload["draft"]) == json.loads(draft)
    assert payload["claim_texts"] == {"C1": claim} and payload["targets"][0]["field"] == "/reason"
    event = record["last_run"]["draft_reviews"][-1]
    assert event["target_count"] == 1 and event["background_count"] == 1
    assert len(writes) == 1 and result["status"] == "completed"
