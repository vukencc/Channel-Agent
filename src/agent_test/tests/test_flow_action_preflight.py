import pytest

from agent_core.core.flow_action_preflight import FlowActionPreflight


def definition(name="send", properties=None):
    properties = properties or {"message": {"type": "string"}}
    return {"name": name, "description": "业务工具", "code": "def main(arguments): return arguments", "network": False,
            "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}


def test_draft_approval_is_exact_and_single_use():
    tool = definition()
    gate = FlowActionPreflight([tool])
    preview = gate.schemas()[0]["function"]
    assert preview["parameters"] == tool["parameters"]
    assert gate.original_for_preview(preview["name"]) == "send"
    binding = {"turn": "1", "source_digest": "abc", "policy": "reviewer"}
    draft = gate.draft(preview["name"], '{"message":"hello"}', binding)
    assert not gate.consume("send", '{"message":"hello"}', binding, tool)
    assert gate.approve(draft, binding)
    assert len(gate.schemas()) == 2
    assert not gate.consume("send", '{"message":"changed"}', binding, tool)
    assert not gate.consume("send", '{"message":"hello"}', {**binding, "turn": "2"}, tool)
    assert not gate.consume("send", '{"message":"hello"}', binding, {**tool, "code": "changed"})
    assert gate.consume("send", '{ "message" : "hello" }', binding, tool)
    assert not gate.consume("send", '{"message":"hello"}', binding, tool)
    assert len(gate.schemas()) == 1


def test_strict_json_schema_and_bounded_pending():
    tool = definition(properties={"ids": {"type": "array", "items": {"type": "string"}}})
    gate = FlowActionPreflight([tool], max_pending=1)
    preview = gate.schemas()[0]["function"]["name"]
    for raw in ['{"ids":[],"ids":[]}', '{"ids":NaN}', '[]', '{"ids":[1]}', '{"ids":["' + 'x' * 65536 + '"]}']:
        with pytest.raises(ValueError):
            gate.draft(preview, raw, {})
    draft = gate.draft(preview, '{"ids":["a","b"]}', {})
    with pytest.raises(ValueError):
        gate.draft(preview, '{"ids":[]}', {})
    assert gate.approve(draft, {})
    assert not gate.consume("send", '{"ids":["b","a"]}', {}, tool)
    assert gate.discard(draft)
    assert not gate.approve(draft, {})


def test_selection_and_colliding_names():
    read = definition("read", {"id": {"type": "string"}, "limit": {"type": "integer"}})
    write = definition("x" * 64)
    gate = FlowActionPreflight([read, write], occupied_names={"draft_" + "x" * 58})
    names = [item["function"]["name"] for item in gate.schemas()]
    assert names[0] == "read"
    assert len(names[1]) <= 64 and names[1] != write["name"] and names[1] not in {"draft_" + "x" * 58, "read"}
    assert FlowActionPreflight([definition("ambiguous", {"id": {"type": "object"}})]).schemas()[0]["function"]["name"] != "ambiguous"


def test_number_spelling_and_object_key_order():
    tool = definition(properties={"amount": {"type": "number"}, "text": {"type": "string"}})
    gate = FlowActionPreflight([tool])
    preview = gate.preview_for_original("send")
    draft = gate.draft(preview, '{"amount":1e0,"text":"x"}', {"turn": 1})
    assert gate.approve(draft, {"turn": 1})
    assert not gate.consume("send", '{"amount":1.0,"text":"x"}', {"turn": 1}, tool)
    assert gate.consume("send", '{"text":"x","amount":1e0}', {"turn": 1}, tool)
    with pytest.raises(ValueError):
        gate.draft(preview, '{"text":"x","amount":1e0}', {"turn": 1})


def test_selector_shape_and_preview_description():
    read = definition("read", {"path": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}})
    assert FlowActionPreflight([read]).schemas()[0]["function"]["name"] == "read"
    write = definition("write", {**read["parameters"]["properties"], "message": {"type": "string"}})
    preview = FlowActionPreflight([write]).schemas()[0]["function"]
    assert preview["name"] != "write"
    assert write["description"] in preview["description"]
    read["parameters"]["properties"]["ids"]["items"] = {"type": "object"}
    assert FlowActionPreflight([read]).schemas()[0]["function"]["name"] != "read"


def test_consumed_records_do_not_exhaust_capacity_or_expose_validation_values():
    tool = definition()
    gate = FlowActionPreflight([tool], max_pending=1)
    preview = gate.preview_for_original("send")
    for value in ("one", "two", "three"):
        raw = '{"message":"' + value + '"}'
        receipt = gate.draft(preview, raw, {})
        assert gate.approve(receipt, {})
        assert gate.consume("send", raw, {}, tool)
    with pytest.raises(ValueError) as error:
        gate.draft(preview, '{"message":123456789}', {})
    assert "123456789" not in str(error.value)
    assert error.value.__cause__ is None


def test_only_receipts_returned_before_this_model_request_can_be_consumed():
    tool = definition()
    preflight = FlowActionPreflight([tool], max_pending=1)
    preview = preflight.preview_for_original("send")
    issued_before = preflight.approved_ids()
    for text in ("first", "second", "third"):
        raw = '{"message":"' + text + '"}'
        identifier = preflight.draft(preview, raw, {})
        assert preflight.approve(identifier, {})
        assert preflight.take_approval("send", raw, {}, tool, issued=issued_before) is None
        assert preflight.take_approval("send", raw, {}, tool, issued=preflight.approved_ids()) == identifier
    selector = definition("fetch", {"path": {"type": "string"},
        "ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}})
    assert FlowActionPreflight([selector]).preview_for_original("fetch") is None


def test_project_dynamic_code_requires_preview_even_with_selector_arguments():
    selector = definition("operate", {"id": {"type": "string"}})
    catalog = FlowActionPreflight([selector], stage_selectors=True)
    shown = [row["function"]["name"] for row in catalog.schemas()]
    assert shown == [catalog.preview_for_original("operate")] and "operate" not in shown
