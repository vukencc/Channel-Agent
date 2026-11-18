import json

from agent_core.core.flow_review_items import review_units


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
