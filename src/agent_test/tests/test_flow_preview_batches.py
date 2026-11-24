"""用受控模型回调检查动作草稿的分批来源复核。"""
import asyncio
import json

import pytest

from agent_core.core.flow_review_claims import claim_targets, focal_targets
from agent_test.tests.test_flow_review_parallel import install_reviewer, setup_gate
from agent_test.tests.test_flow_whole_claim_batches import rejection, reply


DRAFT = json.dumps({"feedback_id": "F1", "message": "".join(
    f"客户明确要求核对第{i}项记录。" for i in range(1, 7)
)}, ensure_ascii=False, separators=(",", ":"))
ACTION = {"name": "send_reply", "fields": {"message": {"description": "发给该事项客户的回复"}}}
SOURCE = {"id": "s1", "kind": "read_file", "text": "原始证据"}


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("revise", [False, True])
def test_preview_batches_keep_full_evidence_and_require_every_result(monkeypatch, concurrency, revise):
    gate, _, histories, state = setup_gate(monkeypatch, concurrency)
    gate.session.flow_review_task = "按节点职责检查原始资料。"

    async def action(payload, attempt):
        await asyncio.sleep(0)
        if revise and payload["targets"][0]["id"] == "C5":
            return rejection(payload)
        return reply()

    install_reviewer(gate, histories, state, action)
    feedback = asyncio.run(gate.check(DRAFT, stage="preview", call_id="draft-call", action_context=ACTION))
    expected = focal_targets(claim_targets(DRAFT))
    assert len(expected) == 6
    assert [len(payload["targets"]) for payload in histories] == [4, 2]
    positions = ("id", "field", "start", "end")
    assert [tuple(row[key] for key in positions) for payload in histories for row in payload["targets"]] == [
        tuple(row[key] for key in positions) for row in expected
    ]
    for payload in histories:
        assert payload["draft"] == DRAFT
        assert payload["action_context"] == ACTION
        assert payload["sources"] == [SOURCE]
        assert payload["workflow_task"] == gate.session.flow_review_task
        assert "action_receipt_summary" not in payload
    assert state["peak"] == concurrency
    assert state["reserved"] == 2
    assert bool(feedback) is revise
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["stage"] == "preview" and event["tool_call_id"] == "draft-call"
    assert event["status"] == ("revise" if revise else "passed")
    assert [row["target_count"] for row in event["batches"]] == [4, 2]


def test_batched_preview_can_only_recover_once_for_the_whole_draft(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch, concurrency=1)

    async def action(payload, attempt):
        if attempt == 1:
            raise TimeoutError
        return reply()

    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(DRAFT, stage="preview", call_id="draft-call", action_context=ACTION))
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["status"] == "TimeoutError"
    assert [batch["attempts"] for batch in event["batches"]] == [["TimeoutError", "passed"], ["TimeoutError"]]
    assert gate.session.record["last_run"]["recovery_attempts"] == 1
    assert state["reserved"] == 3


def test_permission_change_during_batched_preview_blocks_approval(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch, concurrency=2)

    async def action(payload, attempt):
        await asyncio.sleep(0)
        gate.session.record["permission_policy"] = "readonly"
        return reply()

    install_reviewer(gate, histories, state, action)
    feedback = asyncio.run(gate.check(DRAFT, stage="preview", call_id="draft-call", action_context=ACTION))
    assert "上下文已改变" in feedback
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["status"] == "context_changed"
    assert state["active"] == 0
