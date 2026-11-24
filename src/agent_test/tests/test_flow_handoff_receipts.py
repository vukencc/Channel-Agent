import json

from agent_core.core.flow_handoff_receipts import FlowHandoffReceipts


CONTRACT = {"fields": {"submitted_ticket_ids": {"type": "array"}, "notes": {"type": "string"}}}


def observe(receipts, call_id, payload, *, tool_name="submit_to_ops", arguments="{}", **changes):
    call = {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": arguments}}
    metrics = {"tool_call_id": call_id, "name": tool_name, "action_stage": "commit", "source": "executed", "outcome": "completed", "execution_progress": True}
    metrics.update(changes)
    result = "[退出码] 0\n[stdout]\n" + (payload if isinstance(payload, str) else json.dumps(payload))
    receipts.observe(call, result, metrics)


def test_simulated_receipts_do_not_turn_feedback_ids_into_ticket_ids():
    receipts = FlowHandoffReceipts(CONTRACT)
    for i in range(2):
        observe(receipts, str(i), {"ok": True, "simulated": True, "delivered_externally": False, "ticket": {"feedback_id": f"F{i}"}})
    assert receipts.expectations() == {"submitted_ticket_ids": []}
    guide = receipts.guide(include_values=True)
    assert "F0" in guide and "F1" in guide and "2" in guide and "[]" in guide


def test_issued_ids_are_ordered_and_conflicts_are_not_inferred():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket": {"ticket_id": "T1", "feedback_id": "F1"}})
    observe(receipts, "2", {"ok": True, "ticket": {"id": "T2"}})
    assert receipts.expectations() == {"submitted_ticket_ids": ["T1", "T2"]}
    observe(receipts, "3", {"ok": True, "ticket": {"ticket_id": "T3", "id": "other"}})
    assert receipts.expectations() == {}


def test_uncertain_commit_invalidates_complete_mapping():
    for payload, changes in [({"ok": False, "ticket": {}}, {}), ("{\"ok\":true,\"ticket\":{}", {}), ({"ok": True, "ticket": {}}, {"outcome": "error"}), ({"ok": True, "ticket": {}}, {"tool_call_id": "other"})]:
        receipts = FlowHandoffReceipts(CONTRACT)
        observe(receipts, "1", {"ok": True, "ticket": {"feedback_id": "F"}})
        observe(receipts, "2", payload, **changes)
        assert receipts.expectations() == {}


def test_preview_and_read_do_not_establish_receipts():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket": {}}, action_stage="preview")
    observe(receipts, "2", {"ok": True, "ticket": {}}, action_stage=None)
    assert receipts.expectations() == {}


def test_duplicates_and_unrelated_custom_contract_are_not_enforced():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket": {"id": "T1"}})
    observe(receipts, "2", {"ok": True, "ticket": {"id": "T1"}})
    assert receipts.expectations() == {}
    receipts = FlowHandoffReceipts({"fields": {"feedback_ids": {"type": "array"}}})
    observe(receipts, "1", {"ok": True, "feedback": {"id": "F1"}})
    assert receipts.expectations() == {}


def test_invalid_and_truncated_receipts_cannot_prove_empty_ids():
    for payload in ["{\"ok\":true,\"ok\":true,\"ticket\":{}}", "{\"ok\":true,\"ticket\":{}}\n[输出已截断]", {"ok": True, "ticket": {"ticket_id": None}}, {"ok": True, "other": {}}]:
        receipts = FlowHandoffReceipts(CONTRACT)
        observe(receipts, "1", payload)
        assert receipts.expectations() == {}


def test_duplicate_call_and_capacity_fail_closed():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket": {"feedback_id": "F"}})
    observe(receipts, "1", {"ok": True, "ticket": {"feedback_id": "F"}})
    assert receipts.expectations() == {}
    receipts = FlowHandoffReceipts(CONTRACT)
    for i in range(129):
        observe(receipts, str(i), {"ok": True, "ticket": {"feedback_id": "F"}})
    assert receipts.expectations() == {}


def test_independent_entities_and_not_executed_rejection():
    contract = {"fields": {"submitted_ticket_ids": {"type": "array"}, "created_case_ids": {"type": "array"}}}
    receipts = FlowHandoffReceipts(contract)
    observe(receipts, "1", {"ok": True, "ticket": {"id": "T1"}})
    observe(receipts, "2", {"ok": True, "case": {"case_id": "C1"}}, tool_name="create_case")
    observe(receipts, "3", {}, source="not_executed", outcome="rejected", execution_progress=False)
    assert receipts.expectations() == {"submitted_ticket_ids": ["T1"], "created_case_ids": ["C1"]}


def test_action_and_entity_must_match():
    for name in ("read_ticket", "create_ticket", "unknown_ticket"):
        receipts = FlowHandoffReceipts(CONTRACT)
        observe(receipts, "1", {"ok": True, "ticket": {"feedback_id": "F"}}, tool_name=name)
        assert receipts.expectations() == {}


def test_root_ids_and_insufficient_evidence():
    for payload in ({"ok": True, "ticket": {}}, {"ok": True, "id": "T", "ticket": {"feedback_id": "F"}}, {"ok": True, "ticket_no": "T", "ticket": {"feedback_id": "F"}}, {"ok": True, "ticketId": "T", "ticket": {"feedback_id": "F"}}, {"ok": True, "ticket": {"number": "T", "feedback_id": "F"}}, {"ok": True, "ticket_id": "T1", "ticket": {"id": "T2"}}):
        receipts = FlowHandoffReceipts(CONTRACT)
        observe(receipts, "1", payload)
        assert receipts.expectations() == {}
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket_id": "T1", "ticket": {"id": "T1"}})
    assert receipts.expectations() == {"submitted_ticket_ids": ["T1"]}


def test_values_excluded_by_default_and_echoed_id_is_not_issued():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "1", {"ok": True, "ticket": {"id": "T1", "feedback_id": "F1"}}, arguments='{"ticket_id":"T1"}')
    assert receipts.expectations() == {"submitted_ticket_ids": []}
    assert "T1" not in receipts.guide() and "F1" not in receipts.guide()
    assert "F1" in receipts.guide(include_values=True)


def test_oversized_full_reference_has_no_constraints():
    receipts = FlowHandoffReceipts(CONTRACT)
    for i in range(100):
        observe(receipts, str(i), {"ok": True, "ticket": {"feedback_id": str(i) + "x" * 100}})
    assert receipts.guide(include_values=True) == ""
    assert receipts.expectations() == {}


def test_model_display_bounds_canonical_expansion(monkeypatch):
    """有效中文按原文预算展示，越过最大可能转义长度的旧数据不再次解析。"""
    from agent_core.core import flow_handoff

    body = "资" * 16000
    contract = {"fields": {"notes": {"type": "string"}}}
    readable = json.dumps({"notes": body}, ensure_ascii=False, separators=(",", ":"))
    canonical, error = flow_handoff.validate_handoff(readable, contract)
    assert error is None and len(canonical) > flow_handoff.MAX_HANDOFF_CHARS
    assert flow_handoff.handoff_for_model(canonical, contract) == readable
    oversized = "x" * (12 * flow_handoff.MAX_HANDOFF_CHARS + 1)
    def forbid_decode(*args, **kwargs):
        raise AssertionError("超出有效交接可能转义长度时应在 JSON 解析前返回")
    monkeypatch.setattr(flow_handoff.json, "loads", forbid_decode)
    assert flow_handoff.handoff_for_model(oversized, contract) is oversized


def test_resumed_receipt_reader_exemption_requires_trusted_successful_pair():
    receipts = FlowHandoffReceipts(CONTRACT)
    observe(receipts, "issued", {"ok": True, "ticket": {"id": "T1"}})
    state = receipts.checkpoint_state()
    for changes, preserved in [
        ({"receipt_reader": True}, True),
        ({"receipt_reader": True, "source": "not_executed", "outcome": "rejected",
          "execution_progress": False}, True),
        ({"receipt_reader": True, "outcome": "error", "execution_progress": False}, False),
        ({"receipt_reader": True, "source": "result_unknown"}, False),
        ({"receipt_reader": True, "tool_call_id": "different"}, False),
        ({}, False),
    ]:
        resumed = FlowHandoffReceipts(CONTRACT)
        resumed.restore_checkpoint(state)
        observe(resumed, "read", {"items": [], "receipt_reader": True}, tool_name="read_feedback", **changes)
        assert resumed.lost_checkpoint_evidence() is not preserved
        assert resumed.expectations() == ({"submitted_ticket_ids": ["T1"]} if preserved else {})


def test_receipt_reader_backend_is_limited_to_linux_and_windows_sandbox():
    from agent_core.core.flow_receipt_readers import receipt_reader_backend

    assert receipt_reader_backend("linux", "sandbox")
    assert receipt_reader_backend("win32", "sandbox")
    for platform in ("linux", "win32", "darwin"):
        assert not receipt_reader_backend(platform, "local")
    assert not receipt_reader_backend("darwin", "sandbox")
