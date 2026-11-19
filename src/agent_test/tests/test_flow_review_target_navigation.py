"""整稿导航必须绑定实际字段和原始来源，预算不足时仍保留完整输入。"""
import json

import pytest

from agent_core.core.flow_review_claims import claim_targets, prepare_claim_request


def target(key, path, field, needle):
    start = field.index(needle)
    return {"id": key, "field": path, "text": needle, "start": start, "end": start + len(needle)}


def references(draft, targets, ids):
    from agent_core.core.flow_review_navigation import target_record_references

    return target_record_references(draft, targets, set(ids))


def test_escaped_pointers_bind_the_current_field_and_cross_references():
    text = "客户明确提到 LOT-20，并附前一次记录。"
    memo = "另见 LOT-2。并列 LOT-20。"
    doc = {"A/B~": [{"record_id": "LOT-2", "more": {"~note/": text}},
                    {"id": "LOT-20", "notes": "无关"}], "memo": memo}
    ts = [target("C3", "/A~1B~0/0/more/~0note~1", text, text),
          target("C6", "/memo", memo, "并列 LOT-20。")]
    owners, refs = references(json.dumps(doc, ensure_ascii=False), ts, ["LOT-2", "LOT-20"])
    assert owners == {"C3": "LOT-2"}
    assert refs == {"C3": ("LOT-2", "LOT-20"), "C6": ("LOT-20",)}


@pytest.mark.parametrize("mutate", [
    {"start": True}, {"end": 9999}, {"start": -1}, {"text": "不同原句"},
    {"field": "/x/00/note"}, {"field": "/x/-1/note"}, {"field": "/x/0/~2note"},
    {"field": "/x/0/absent"}, {"field": "x/0/note"}
])
def test_stale_target_never_acquires_navigation_even_if_it_spells_an_id(mutate):
    text = "关联 LOT-20。"
    draft = json.dumps({"x": [{"id": "LOT-2", "note": text}]}, ensure_ascii=False)
    row = target("C1", "/x/0/note", text, text)
    assert references(draft, [{**row, **mutate}], ["LOT-2", "LOT-20"]) == ({}, {})


@pytest.mark.parametrize("text", ["另见 LOT-200。", "LOT-20-extra", "A-LOT-20", "LOT-20.3",
                                   "工单一致，或参考上面几项。"])
def test_shared_text_never_inherits_from_a_nearby_record_or_partial_identifier(text):
    doc = {"records": [{"id": "LOT-20"}, {"id": "LOT-2"}], "memo": text}
    ts = [target("C1", "/memo", text, text)]
    assert references(json.dumps(doc, ensure_ascii=False), ts, ["LOT-2", "LOT-20"]) == ({}, {})


@pytest.mark.parametrize("draft", [
    '{"x":[{"id":"U7","n":"结论"},{"id":"U7","n":"重复"}]}',
    '{"x":[{"id":"U7","ticket_id":"U8","n":"结论"}]}',
    '{"x":[{"id":"U7","record_id":7,"n":"结论"}]}',
    '{"x":[{"id":"U7","n":"错误","n":"结论"}]}',
    '{"x":[{"id":"UNOBSERVED","n":"结论"}]}'
])
def test_duplicate_ambiguous_or_unobserved_record_never_assigns_owner(draft):
    assert references(draft, [target("C1", "/x/0/n", "结论", "结论")], ["U7", "U8"]) == ({}, {})


@pytest.mark.parametrize("measure", ["history_size", "request_tokens"])
def test_wire_pairs_each_item_with_substantive_source_spans_and_keeps_full_input(monkeypatch, measure):
    from agent_core.core import flow_review_claims

    records = [{"item_id": "A-11", "note": "客户明确提出需要核对收件日期。"},
               {"item_id": "A-12", "note": "客户明确提出需要核对退款记录。"}]
    draft = json.dumps({"rows": records}, ensure_ascii=False)
    targets = claim_targets(draft)
    sources = [{"id": "s1", "kind": "read_file", "scope": "observed_span",
                "path": "queue.json", "text": json.dumps(records, ensure_ascii=False)},
               {"id": "u1", "kind": "user_input", "scope": "user_report", "text": "客户另补交了记录。"}]
    def prepare():
        history, passages = prepare_claim_request(draft, sources, targets=targets, whole_batch=True,
                                                  char_limit=100000, token_limit=100000)
        payload = json.loads(history[1]["content"])
        assert payload["sources"] == sources and payload["draft"] == draft
        assert [r["excerpt"] for r in payload["targets"]] == [t["text"] for t in targets]
        assert all(set(row) == {"id", "source_id", "text", "start", "end"} for row in passages)
        return payload, passages

    payload, passages = prepare()
    assert [t["record_refs"] for t in payload["targets"]] == [["A-11"], ["A-12"]]
    assert {(p["record_ref"], p["text"]) for p in payload["evidence_passages"] if "record_ref" in p} >= {
        (r["item_id"], r["note"]) for r in records}
    assert "record_ref" not in next(p for p in payload["evidence_passages"] if p["source_id"] == "u1")
    original = getattr(flow_review_claims, measure)
    monkeypatch.setattr(flow_review_claims, measure, lambda history: 1_000_000
                        if '"record_refs":' in history[1]["content"] else original(history))
    limited, still_issued = prepare()
    assert all("record_refs" not in t for t in limited["targets"])
    assert all("record_ref" not in p for p in limited["evidence_passages"])
    assert still_issued == passages


def test_container_identity_does_not_label_shared_fields_across_nested_records():
    doc = {"id": "A81", "note": "共享说明。", "items": [{"id": "B56", "note": "独立说明。"}]}
    rows = [target("C1", "/note", "共享说明。", "共享说明。"),
            target("C2", "/items/0/note", "独立说明。", "独立说明。")]
    assert references(json.dumps(doc, ensure_ascii=False), rows, ["A81", "B56"]) == (
        {"C2": "B56"}, {"C2": ("B56",)})


def test_anonymous_object_in_a_record_list_has_no_inherited_item_label():
    doc = {"id": "A81", "rows": [{"note": "未经标记的事项。"}]}
    row = target("C1", "/rows/0/note", "未经标记的事项。", "未经标记的事项。")
    assert references(json.dumps(doc, ensure_ascii=False), [row], ["A81", "B56"]) == ({}, {})


@pytest.mark.parametrize("limit", ["depth", "nodes", "ids"])
def test_large_or_deep_inputs_omit_optional_labels_but_keep_source_and_draft(limit):
    doc = {"rows": [{"id": "A81", "note": "客户明确要求核对记录。"}]}
    records = [{"id": "A81", "detail": "甲事项的原始记录。"}, {"id": "B56", "detail": "乙事项。"}]
    if limit == "depth":
        value = None
        for _ in range(10):
            value = [value]
        doc["metadata"] = value
    elif limit == "nodes":
        doc["metadata"] = [None] * 2050
    else:
        records.extend({"id": "D" + str(i), "detail": "另一个事项。"} for i in range(257))
    draft = json.dumps(doc, ensure_ascii=False)
    source = {"id": "s1", "kind": "read_file", "scope": "observed_span",
              "text": json.dumps(records, ensure_ascii=False)}
    row = target("C1", "/rows/0/note", doc["rows"][0]["note"], doc["rows"][0]["note"])
    history, _ = prepare_claim_request(draft, [source], targets=[row], whole_batch=True,
                                       char_limit=200000, token_limit=100000)
    payload = json.loads(history[1]["content"])
    assert payload["sources"] == [source] and payload["draft"] == draft
    assert payload["targets"][0]["excerpt"] == row["text"]
    assert "record_refs" not in payload["targets"][0]
    assert all("record_ref" not in part for part in payload.get("evidence_passages", []))
