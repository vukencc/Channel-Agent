"""受控复核回调验证收据摘要边界。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_core.core import flow_draft_gate as gate_module
from agent_core.core.flow_draft_gate import FlowDraftGate


class ReceiptIndex:
    def __init__(self):
        self.calls = 0
        self.revision = 1
        self.facts = [{"count": 2, "simulated": True, "keys": ["case_id"]}]

    def review_facts(self):
        self.calls += 1
        return self.facts


def make_gate(monkeypatch):
    index = ReceiptIndex()
    histories = []

    async def model(history, **kwargs):
        histories.append(history)
        return {"role": "assistant", "content": '{"status":"pass"}'}

    manager = SimpleNamespace(_call_model=model, _observe_phase=lambda *a: None, notify=lambda: None)
    session = SimpleNamespace(record={"last_run": {}}, flow_review_task="", phase="", id="s",
                              cancelled=asyncio.Event(), last_model_event_at=0)
    monkeypatch.setattr(gate_module, "review_sources", lambda *a, **kw: [
        {"id": "s1", "kind": "read_file", "text": "原始证据"}])
    gate = FlowDraftGate(manager, session, lambda: True, receipt_index=index)
    return gate, index, histories


@pytest.mark.parametrize("stage,kind,expected", [("handoff", None, True), ("handoff", "shared_claim", True),
                                                 ("handoff", "item", False), ("preview", None, False)])
def test_receipt_summary_only_for_final_or_shared_review(monkeypatch, stage, kind, expected):
    gate, index, histories = make_gate(monkeypatch)
    asyncio.run(gate._check_one("客户明确要求处理", stage=stage, force=True, unit_kind=kind))
    payload = json.loads(histories[0][1]["content"])
    assert ("action_receipt_summary" in payload) == expected
    assert index.calls == int(expected)
    assert all(row["id"] != "case_id" for row in payload["sources"])
    if expected:
        assert payload["action_receipt_summary"] == index.facts
        assert "不能证实客户意愿" in histories[0][0]["content"]


def test_receipt_summary_dropped_when_no_budget(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    original = gate_module.history_size
    monkeypatch.setattr(gate_module, "history_size", lambda history: original(history) + (
        1000000 if "action_receipt_summary" in history[1]["content"] else 0))
    asyncio.run(gate._check_one("客户明确要求处理", stage="handoff", force=True))
    assert index.calls == 1
    assert "action_receipt_summary" not in json.loads(histories[0][1]["content"])


def test_original_over_budget_never_reads_receipts(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    monkeypatch.setattr(gate_module, "history_size", lambda history: 1000000)
    asyncio.run(gate._check_one("客户明确要求处理", stage="handoff", force=True))
    assert index.calls == 0 and histories == []
    assert gate.session.record["last_run"]["draft_reviews"][-1]["status"] == "context_budget"


def test_receipt_facts_cannot_be_cited_as_source(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    monkeypatch.setattr(gate_module, "claim_targets", lambda draft: [])
    original_limit = gate_module.session_limit
    monkeypatch.setattr(gate_module, "session_limit", lambda name: 0 if name == "MODEL_RECOVERY_LIMIT" else original_limit(name))

    async def model(history, **kwargs):
        histories.append(history)
        return {"role": "assistant", "content": json.dumps({"status": "revise", "issues": [{
            "field": "draft", "reason": "问题", "source_id": "case_id", "quote": "case_id"}]})}

    gate.manager._call_model = model
    feedback = asyncio.run(gate._check_one("客户明确要求处理", stage="handoff", force=True))
    assert "审查结果无效" in feedback
    assert "case_id" not in feedback


def test_receipt_review_cache_tracks_actual_facts(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    from agent_core.core import flow_review_items
    monkeypatch.setattr(flow_review_items, "review_units", lambda *a, **kw: [{
        "item_id": "s", "kind": "shared_claim", "draft": "客户明确要求处理",
        "sources": [{"id": "s1", "kind": "read_file", "text": "原始证据"}]}])
    async def run():
        for _ in range(2):
            assert await gate.check("客户明确要求处理", stage="handoff") is None
        index.facts = [{"count": 3}]
        index.revision += 1
        assert await gate.check("客户明确要求处理", stage="handoff") is None
    asyncio.run(run())
    assert len(histories) == 2
    assert json.loads(histories[-1][1]["content"])["action_receipt_summary"] == [{"count": 3}]


def test_receipt_index_preserves_revision_focus_when_unchanged(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    from agent_core.core import flow_review_revision
    targets = [{"id": "C1", "text": "客户明确要求处理", "field": "draft", "start": 0, "end": 8}]
    focused = []
    monkeypatch.setattr(gate_module, "shared_targets", lambda draft: targets)
    monkeypatch.setattr(gate_module, "prepare_claim_request", lambda draft, sources, **kw: (
        gate_module.review_messages(draft, sources), []))
    monkeypatch.setattr(flow_review_revision, "revision_targets", lambda *args: focused.append(args) or targets)
    monkeypatch.setattr(gate_module, "parse_review_reply", lambda answer: {
        "status": "revise", "issues": [{"target_id": "C1"}]})
    monkeypatch.setattr(gate_module, "claim_assessment", lambda *args: ("需修改", None))
    async def run():
        await gate._check_one("客户明确要求处理", stage="handoff", force=True,
                              unit_kind="shared_claim", revision_key="s")
        await gate._check_one("客户要求处理", stage="handoff", force=True, recheck=True,
                              unit_kind="shared_claim", revision_key="s")
        index.revision += 1
        await gate._check_one("客户要求处理", stage="handoff", force=True, recheck=True,
                              unit_kind="shared_claim", revision_key="s")
    asyncio.run(run())
    assert len(focused) == 1
