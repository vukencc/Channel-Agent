import asyncio
import json

import pytest

from agent_core.core.flow_review_items import review_units, tool_review_sources


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def source(value, kind="user_input", scope="user_report"):
    return {"id": "U1", "kind": kind, "scope": scope, "text": dump(value)}


def test_four_groups_preserve_record_context_and_project_sources():
    rows = [{"id": f"C{i}", "text": f"客户表示问题{i}", "reason": "普通说明"} for i in range(7)]
    draft = {"source_path": "feedback.json", "a": rows[:2], "b": rows[2:4], "c": rows[4:5], "d": rows[5:]}
    draft["d"][1]["prior_help_ineffective"] = "客户明确表示此前处理无效"
    original = [{"id": row["id"], "text": row["text"]} for row in rows]
    units = review_units(dump(draft), [source(original)])
    assert units is not None and len(units) == 1
    assert all(json.loads(unit["draft"])["source_path"] == "feedback.json" for unit in units)
    assert json.loads(units[-1]["draft"])["d"][0] == rows[-1]
    assert units[-1]["sources"][0]["id"] == "U1"
    assert "问题6" in units[-1]["sources"][0]["text"]
    assert "问题0" not in units[-1]["sources"][0]["text"]


def test_copy_only_same_id_and_field_and_trusted_source():
    draft = [{"id": "a", "text": "客户表示希望退款"}, {"id": "b", "text": "客户表示希望换货"}]
    assert review_units(dump(draft), [source(draft)]) == []
    wrong = [{"id": "b", "text": draft[0]["text"]}, {"id": "a", "text": draft[1]["text"]}]
    assert len(review_units(dump(draft), [source(wrong)])) == 2
    assert len(review_units(dump(draft), [source(draft, "read_file", "observed_span")])) == 2
    assert len(review_units(dump(draft), [source(draft)], required_ids=frozenset({"a"}))) == 1
    assert review_units(dump(draft), [source(draft)], required_ids=frozenset({"missing"})) is None


def test_fallback_for_ambiguous_structure_and_cross_references():
    assert review_units('[{"id":"a","id":"b","text":"客户表示好"}]', []) is None
    assert review_units(dump([{"id": "a"}, {"id": "a"}]), []) is None
    assert review_units(dump({"summary": "客户明确表示退款", "items": [{"id": "a"}]}), []) is None
    assert review_units(dump([{"id": "a", "text": "见事项 b"}, {"id": "b", "text": "普通"}]), []) is None
    assert review_units("```json\n[]\n``` trailing", []) is None


def test_fenced_routing_and_unsplittable_source():
    draft = [{"id": "a", "category": "normal"}, {"id": "b", "reason": "规则要求先审批"}]
    row = {"id": "S1", "kind": "rag_search", "scope": "observed_span", "text": "完整条款"}
    units = review_units("```json\n" + dump(draft) + "\n```", [row])
    assert units and len(units) == 1 and units[0]["item_id"] == "b"
    assert units[0]["sources"] == [row]


def test_rules_and_unmatched_text_are_never_skipped():
    draft = [{"id": "a", "text": "规则要求先审批"}, {"id": "b", "text": "新增描述"}]
    units = review_units(dump(draft), [source(draft)])
    assert [unit["item_id"] for unit in units] == ["a"]
    assert len(review_units(dump(draft), [source([draft[0]])])) == 1


def test_implicit_cross_reference_and_source_metadata_preserved():
    assert review_units(dump([{"id": "a", "reason": "其他事项同上"}, {"id": "b"}]), []) is None
    original = {"source_path": "original.json", "items": [{"id": "a", "text": "原文"}, {"id": "b", "text": "原文2"}]}
    row = source(original)
    assert review_units(dump([{"id": "a", "reason": "客户表示退款"}, {"id": "b"}]), [row]) is None


def test_real_shape_projects_one_and_preserves_exact_source_fragment():
    originals = [{"id": f"C{i}", "text": f"客户表示情况{i}"} for i in range(7)]
    rows = [{**row, "classification_reason": "普通分类说明", "prior_help_ineffective": False} for row in originals]
    rows[-1]["classification_reason"] = "客户明确要求立即退款"
    draft = {"source_path": "feedback.json", "a": rows[:2], "b": rows[2:4], "c": rows[4:5], "d": rows[5:]}
    data = {"ok": True, "path": "feedback.json", "total": 7, "count": 7, "skipped": 0, "items": originals}
    for text in (json.dumps(data, ensure_ascii=False, separators=(",", ":")), json.dumps(data, ensure_ascii=False, indent=2)):
        row = {"id": "T1", "kind": "custom_result", "scope": "tool_report", "text": text}
        units = review_units(dump(draft), [row])
        assert len(units) == 1 and units[0]["item_id"] == "C6"
        assert json.loads(units[0]["draft"])["d"][0] == rows[-1]
        fragment = units[0]["sources"][0]["text"]
        assert fragment in text and json.loads(fragment) == originals[-1]


def test_cross_field_trigger_and_untrusted_or_wrong_id_copy():
    draft = [{"id": "a", "text": "客户表示退款", "reason": "要求复核"}, {"id": "b", "text": "普通"}]
    assert [u["item_id"] for u in review_units(dump(draft), [source(draft)])] == ["a"]
    draft[0]["reason"] = "普通分类"
    wrong = [{"id": "b", "text": draft[0]["text"]}, {"id": "a", "text": "其他"}]
    assert [u["item_id"] for u in review_units(dump(draft), [source(wrong)])] == ["a"]
    assert [u["item_id"] for u in review_units(dump(draft), [source(draft, "read_file", "observed_span")])] == ["a"]


def test_limits_and_single_item_fallback():
    assert review_units(dump([{"id": "one", "reason": "客户表示退款"}]), []) is None
    assert review_units(dump([{"id": str(i)} for i in range(33)]), []) is None
    assert review_units(dump({"items": [{"id": "a"}, {"id": "b"}], "unknown": {"flag": True}}), []) is None


def test_conflicting_or_duplicate_source_records_fall_back():
    draft = [{"id": "a", "text": "客户表示退款"}, {"id": "b", "text": "普通"}]
    first = source(draft)
    second = {**source([{"id": "a", "text": "客户表示换货"}]), "id": "U2"}
    assert review_units(dump(draft), [first, second]) is None
    duplicate = source([draft[0], draft[0]])
    assert review_units(dump(draft), [duplicate]) is None
    duplicate_key = {**first, "text": '[{"id":"a","id":"b"}]'}
    assert review_units(dump(draft), [duplicate_key]) is None


def test_shared_notes_and_list_metadata_preserve_full_context():
    records = [{"id": "P01", "reason": "客户表示退款", "checks": ["核对记录", "核对条件"], "ok": True},
               {"id": "P02", "reason": "政策要求先审批", "checks": ["核对处理"]}]
    notes = "P01和P02均为模拟流程。P02客户明确要求优先处理。其他状态均待核对。"
    draft = {"entries": records, "memo": notes, "submitted": ["P01", "P02"]}
    units = review_units(dump(draft), [])
    assert [unit["item_id"] for unit in units] == ["P01", "P02"]
    for unit, record in zip(units, records):
        projected = json.loads(unit["draft"])
        assert projected == {"entries": [record], "memo": notes, "submitted": ["P01", "P02"]}


def test_global_sensitive_sentences_must_have_unique_unambiguous_owner():
    base = {"entries": [{"id": "P01"}, {"id": "P02"}], "submitted": ["P01", "P02"]}
    assert review_units(dump({**base, "memo": "客户明确要求处理。"}), []) is None
    assert review_units(dump({**base, "memo": "P01和P02客户明确要求处理。"}), []) is None
    assert review_units(dump({**base, "memo": "P01客户明确要求处理。上述要求需跟进。"}), []) is None
    assert review_units(dump({**base, "memo": "P02客户明确要求处理。"}), []) == [{
        "item_id": "P02", "draft": dump({"entries": [{"id": "P02"}], "submitted": ["P01", "P02"], "memo": "P02客户明确要求处理。"}), "sources": []}]
    assert review_units(dump({**base, "submitted": ["P02", "P01"]}), []) is None


def test_record_references_ascii_boundaries_and_indexed_sources():
    assert review_units(dump([{"id": "P01", "checks": ["见P02"]}, {"id": "P02"}]), []) is None
    draft = [{"id": "P01", "reason": "客户表示退款"}, {"id": "P02", "reason": "客户表示换货"}]
    input_row = {"id": "U1", "kind": "user_input", "scope": "user_report", "text": dump([{"id": "P01", "text": "原始输入"}])}
    policy = {"id": "S1", "kind": "rag_search", "scope": "observed_span", "text": "政策完整条款"}
    units = review_units(dump(draft), [input_row, policy])
    assert [s["id"] for s in units[0]["sources"]] == ["U1", "S1"]
    assert [s["id"] for s in units[1]["sources"]] == ["S1"]


def test_flexible_record_source_wrapper_and_unknown_global_context():
    draft = [{"id": "P01", "reason": "客户表示退款"}, {"id": "P02"}]
    value = {"records": [{"id": "P01", "text": "原话"}, {"id": "P02", "text": "另一个"}], "count": 2, "path": "input.json"}
    row = source(value)
    unit = review_units(dump(draft), [row])[0]
    assert json.loads(unit["sources"][0]["text"]) == value["records"][0]
    value["qualification"] = "须经主管批准"
    row = source(value)
    assert review_units(dump(draft), [row]) is None


def test_shared_claim_context_sources_and_recheck():
    rows = [{"id": "A1"}, {"id": "A2"}]
    text = "普通背景。我仅记录客户未提供的材料，不代表已经核实。政策要求先取得授权。尾注。"
    draft = {"entries": rows, "context": text}
    evidence = [source(rows), {"id": "S1", "kind": "rag_search", "scope": "observed_span", "text": "完整政策来源"}]
    units = review_units(dump(draft), evidence)
    assert len(units) == 1
    unit = units[0]
    assert unit["kind"] == "shared_claim"
    assert json.loads(unit["draft"]) == {"field": "context", "claims": ["我仅记录客户未提供的材料，不代表已经核实。", "政策要求先取得授权。"], "context": text, "item_ids": ["A1", "A2"]}
    assert unit["sources"] == evidence
    revised = {**draft, "context": "普通背景。我记录了材料。"}
    again = review_units(dump(revised), evidence, required_ids=frozenset({unit["item_id"]}))
    assert len(again) == 1 and again[0]["item_id"] == unit["item_id"]
    assert json.loads(again[0]["draft"])["claims"] == ["普通背景。我记录了材料。"]
    collision = {"entries": [{"id": unit["item_id"]}, {"id": "A2"}], "context": text}
    assert review_units(dump(collision), evidence) is None


def test_shared_claim_ambiguity_and_context_changes():
    base = {"entries": [{"id": "A1"}, {"id": "A2"}]}
    assert review_units(dump({**base, "memo": "政策要求两者先取得授权。"}), []) is None
    assert review_units(dump({**base, "memo": "A1和A2政策要求先取得授权。"}), []) is None
    first = review_units(dump({**base, "memo": "政策要求先取得授权。"}), [source(base["entries"])])[0]
    changed = review_units(dump({**base, "memo": "政策要求先取得授权。背景变化。"}), [source(base["entries"])])[0]
    assert first["item_id"] == changed["item_id"] and first["draft"] != changed["draft"]
    negative = review_units(dump({**base, "memo": "政策不要求先取得授权。"}), [source(base["entries"])])[0]
    assert json.loads(negative["draft"])["claims"] == ["政策不要求先取得授权。"]
    other_source = {**source(base["entries"]), "text": dump([{"id": "A1", "text": "变化"}, {"id": "A2"}])}
    changed_source = review_units(dump({**base, "memo": "政策要求先取得授权。"}), [other_source])[0]
    assert changed_source["sources"] != first["sources"]
    other_field = review_units(dump({**base, "memo2": "政策要求先取得授权。"}), [other_source])[0]
    assert other_field["item_id"] != first["item_id"]
    assert review_units(dump({**base, "memo": "政策要求先取得授权。这是必要条件。"}), []) is None
    assert review_units(dump(base), [], required_ids=frozenset({first["item_id"]})) is None


def test_tool_source_selector_is_bounded_by_observed_unique_ids():
    rows = [{"feedback_id": "F01", "text": "第一条"}, {"feedback_id": "F02", "text": "第二条"}]
    report = {"id": "T1", "kind": "custom_result", "scope": "tool_report", "text": dump({"items": rows, "count": 2})}
    rag = {"id": "R1", "kind": "rag_search", "scope": "observed_span", "text": dump(rows)}
    unknown = {"id": "X1", "kind": "read_file", "scope": "observed_span", "text": "不可解析的完整来源"}
    original = [report, rag, unknown]
    selected = tool_review_sources("F01客户明确要求复核。", original)
    assert json.loads(selected[0]["text"]) == rows[0]
    assert selected[1:] == original[1:]
    assert tool_review_sources("F01与F02客户明确要求复核。", original) == original
    assert tool_review_sources("F01与其他事项比较。", original) == original
    assert tool_review_sources("客户明确要求复核。", original) == original
    duplicate = {**report, "text": dump([rows[0], rows[0]])}
    assert tool_review_sources("F01客户明确要求复核。", [duplicate]) == [duplicate]
    conflicting = {**report, "text": dump([{**rows[0], "item_ref": "F02"}, rows[1]])}
    assert tool_review_sources("F01客户明确要求复核。", [conflicting]) == [conflicting]
    conflicting_source = {**report, "id": "T2", "text": dump([{**rows[0], "text": "不同来源记录"}, rows[1]])}
    assert tool_review_sources("F01客户明确要求复核。", [report, conflicting_source]) == [report, conflicting_source]


def test_tool_read_file_projection_requires_structured_unique_matching_id():
    rows = [{"id": "CF007", "text": "原始记录"}, {"id": "CF008", "text": "另一记录"}]
    raw = '{"items": [\n  {"id": "CF007", "text": "原始记录"},\n  {"id": "CF008", "text": "另一记录"}\n]}'
    read = {"id": "S1", "kind": "read_file", "scope": "observed_span", "path": "feedback.json", "text": raw}
    policy = {"id": "S2", "kind": "rag_search", "scope": "observed_span", "text": "完整政策"}
    user = {"id": "U1", "kind": "user_input", "scope": "user_report", "text": "完整用户输入"}
    candidate = dump({"item_ref": "CF007", "reason": "客户要求复核"})
    result = tool_review_sources(candidate, [read, policy, user])
    assert json.loads(result[0]["text"]) == rows[0]
    assert result[0]["text"] in raw
    assert result[0]["text"] == '{"id": "CF007", "text": "原始记录"}'
    fabricated = '{"items": [' + result[0]["text"] + ']}'
    assert fabricated not in raw
    assert fabricated not in result[0]["text"]
    assert {k: v for k, v in result[0].items() if k != "text"} == {k: v for k, v in read.items() if k != "text"}
    assert result[1:] == [policy, user]
    nested = {**read, "text": '{"data": {"records": ' + raw[raw.index("["):raw.rindex("]") + 1] + '}}'}
    nested_result = tool_review_sources(candidate, [nested, user])
    assert nested_result[0] == nested
    assert nested_result[1] == user
    for draft in ("CF007客户要求复核", dump({"reason": "CF007客户要求复核"}),
                  dump({"item_ref": "CF007", "id": "CF008"}), dump({"item_ref": "missing"})):
        assert tool_review_sources(draft, [read, policy, user]) == [read, policy, user]


def test_tool_read_file_projection_rejects_ambiguous_sources():
    draft = dump({"item_ref": "CF007", "reason": "客户要求复核"})
    def read(text):
        return {"id": "S1", "kind": "read_file", "scope": "observed_span", "text": text}
    for data in (
        {"items": [{"id": "CF007", "text": "参见CF008"}, {"id": "CF008", "text": "其他"}]},
        {"items": [{"id": "CF007"}, {"id": "CF007"}]},
        {"items": [{"id": "CF007", "feedback_id": "CF008"}, {"id": "CF008"}]},
        {"unknown": [{"id": "CF007"}, {"id": "CF008"}]},
        {"items": [{"id": "CF007"}, {"id": "CF008"}], "policy": "完整政策"},
    ):
        sources = [read(dump(data))]
        assert tool_review_sources(draft, sources) == sources
    first = read(dump([{"id": "CF007", "text": "甲"}, {"id": "CF008"}]))
    conflict = {**first, "id": "S2", "text": dump([{"id": "CF007", "text": "乙"}])}
    assert tool_review_sources(draft, [first, conflict]) == [first, conflict]
    for other in (
        {"id": "T1", "kind": "custom_result", "scope": "tool_report", "text": dump([{"id": "CF007", "text": "乙"}])},
        {"id": "U1", "kind": "user_input", "scope": "user_report", "text": "另一个用户的 CF007"},
        {"id": "R1", "kind": "rag_search", "scope": "observed_span", "text": "政策适用于 CF007"},
        {"id": "S3", "kind": "read_file", "scope": "observed_span", "text": "未知格式 CF007"},
    ):
        assert tool_review_sources(draft, [first, other]) == [first, other]


def test_shared_claim_intra_sentence_reference_and_item_focus():
    row = [{"id": "F01", "reason": "客户明确要求复核"}, {"id": "F02"}]
    memo = "F01客户明确要求复核。我只记录客户未提供的资料，不将其作为已核实事实。"
    units = review_units(dump({"items": row, "memo": memo}), [source(row)])
    assert units is not None and len(units) == 2
    item, shared = units
    assert json.loads(item["draft"])["memo"] == "F01客户明确要求复核。"
    assert json.loads(shared["draft"])["claims"] == ["我只记录客户未提供的资料，不将其作为已核实事实。"]
    assert json.loads(shared["draft"])["context"] == memo
    assert review_units(dump({"items": row, "memo": "客户未提供资料。我将其记录。"}), []) is None


def test_unrelated_context_pronoun_does_not_block_independent_shared_claim():
    draft = {"items": [{"id": "A1"}, {"id": "A2"}],
             "memo": "登记内容按其分类保存。另有登记步骤。政策要求先取得授权。"}
    units = review_units(dump(draft), [])
    assert units is not None and len(units) == 1
    assert json.loads(units[0]["draft"])["context"] == draft["memo"]


def test_shared_claim_general_determiners_are_not_anaphora():
    draft = {"items": [{"id": "A1"}, {"id": "A2"}],
             "memo": "其他资料的政策要求先取得授权，但其他条件仍需核对。"}
    units = review_units(dump(draft), [])
    assert units is not None and len(units) == 1
    assert json.loads(units[0]["draft"])["claims"] == [draft["memo"]]


@pytest.mark.parametrize("field,text", [
    ("客户", "明确希望退款。"),
    ("权限政策", "必须由管理员操作。"),
    ("明确请求", "由用户本人办理退款。"),
    ("必须办理", "按退款政策执行。"),
])
def test_shared_field_name_and_value_require_full_review(monkeypatch, field, text):
    from agent_core.core.flow_draft_review import needs_review
    from agent_test.tests.test_flow_draft_receipt_review import make_gate
    from agent_test.tests.test_flow_whole_claim_batches import reply

    draft = {"items": [{"id": "A1", "text": "蓝色标记。"}, {"id": "A2", "text": "绿色标记。"}], field: text}
    encoded = dump(draft)
    assert needs_review(encoded) and not needs_review(field) and not needs_review(text)
    gate, _, histories = make_gate(monkeypatch)

    async def model(history, **kwargs):
        assert kwargs["disable_tools"]
        histories.append(history)
        target, = json.loads(history[1]["content"])["targets"]
        return reply("revise", issues=[{"target_id": target["id"], "draft_quote": text,
                                       "source_id": "s1", "quote": "原始证据", "reason": "归属缺少依据"}])

    gate.manager._call_model = model
    assert asyncio.run(gate.check(encoded, stage="handoff"))
    history, = histories
    payload = json.loads(history[1]["content"])
    assert json.loads(payload["draft"]) == draft
    assert payload["sources"] == [{"id": "s1", "kind": "read_file", "text": "原始证据"}]
    assert [target["field"] for target in payload["targets"]] == ["/" + field]
    assert review_units(encoded, payload["sources"]) is None


@pytest.mark.parametrize("with_reviewed_item", [False, True])
@pytest.mark.parametrize("metadata", [
    {"subject": "customer", "statement": "explicitly requested a refund"},
    {"condition": "a tenant-owner policy", "requirement": "must approve reimbursement"},
    {"记载": "明确要求核对。", "主体": "用户"},
])
def test_unassigned_sibling_metadata_preserves_entire_handoff(
    monkeypatch, metadata, with_reviewed_item,
):
    from agent_core.core import flow_draft_gate
    from agent_core.core.flow_draft_review import needs_review
    from agent_test.tests.test_flow_draft_receipt_review import make_gate
    from agent_test.tests.test_flow_whole_claim_batches import reply

    items = [{"id": "A1", "text": "蓝色标记"}, {"id": "A2", "text": "绿色标记"}]
    originals = [{**item} for item in items]
    if with_reviewed_item:
        items[0]["text"] = "客户明确要求核对记录。"
    value = {"items": items, **metadata}
    evidence = [source(originals, "read_file", "observed_span")]
    gate, _, histories = make_gate(monkeypatch)
    monkeypatch.setattr(flow_draft_gate, "review_sources", lambda *a, **k: evidence)

    async def model(history, **kwargs):
        assert kwargs["disable_tools"]
        histories.append(history)
        return reply("need_source", field="statement", needed="独立原始记录")

    gate.manager._call_model = model
    # 反序标点用例也由已有的争议事项触发运行时的整稿筛选。
    if not needs_review(dump(value)):
        assert review_units(dump(value), evidence) is None
        return
    assert asyncio.run(gate.check(dump(value), stage="handoff"))
    assert histories
    for history in histories:
        payload = json.loads(history[1]["content"])
        assert json.loads(payload["draft"]) == value
        assert payload["sources"] == evidence
    assert review_units(dump(value), evidence) is None


def test_empty_unverified_projection_requires_full_review():
    records = [{"id": "A1", "text": "customer"}, {"id": "A2", "text": "explicitly asked for a refund"}]
    assert review_units(dump(records), [source(records, "read_file", "observed_span")]) is None


def test_neutral_metadata_and_proven_verbatim_copy_keep_empty_projection():
    neutral = {"items": [{"id": "A1"}, {"id": "A2"}], "subject": "blue", "statement": "archive"}
    assert review_units(dump(neutral), []) == []
    records = [{"id": "A1", "text": "客户表示希望退款。"}, {"id": "A2", "text": "绿色标记。"}]
    assert review_units(dump({**neutral, "items": records}), [source(records)]) == []


def test_other_shared_unit_cannot_cover_an_unassigned_clause():
    records = [{"id": "A1"}, {"id": "A2"}]
    context = {"items": records, "note": "我只记录客户未提供的资料。"}
    original = [source(records)]
    units = review_units(dump(context), original)
    assert units and [unit["kind"] for unit in units] == ["shared_claim"]
    assert review_units(dump({**context, "statement": "explicitly requested a refund"}), original) is None
