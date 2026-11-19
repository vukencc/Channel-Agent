import json

from agent_core.core.flow_review_passages import relevant_passages, review_passages

def test_target_refs_select_each_record_from_each_source():
    rows = [{"id": f"R{i}", "detail": f"记录{i}发生了具体问题。"} for i in range(10)]
    sources = [
        {"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
         "path": "a.json", "text": json.dumps(rows, ensure_ascii=False)}
        for i in range(2)
    ]
    targets = [{"id": "C1", "text": "记录8问题"}, {"id": "C2", "text": "记录9问题"}]
    selected = relevant_passages(
        sources, targets, "{}", target_refs={"C1": ("R8",), "C2": ("R9",)}
    )
    assert {(p["source_id"], p["text"]) for p in selected} >= {
        (sid, f"记录{i}发生了具体问题。") for sid in ("S0", "S1") for i in (8, 9)
    }
    assert all(p in review_passages(sources) for p in selected)

def test_source_ranges_reject_duplicate_ids_and_escaped_ids():
    from agent_core.core.flow_review_passages import source_record_ranges
    source = {
        "id": "S", "kind": "read_file", "scope": "observed_span", "path": "a.json",
        "text": json.dumps([{"id": "R1", "detail": "x"}, {"id": "R1", "detail": "y"}]),
    }
    assert source_record_ranges([source]).get("S", []) == []
    source["text"] = r'[{"id":"R\u0031","detail":"x"}]'
    assert source_record_ranges([source]).get("S", []) == []


def test_late_records_and_unissued_or_changed_passages():
    from agent_core.core.flow_review_passages import resolve_passage_issues
    rows = [{"id": f"R{i}", "detail": f"第{i}条具体事实需要核对。"} for i in range(24)]
    sources = [{"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
                "path": "a.json", "text": json.dumps(rows, ensure_ascii=False)} for i in range(2)]
    issued = review_passages(sources)
    assert len(issued) == 64
    assert all(any(p["source_id"] == sid and p["text"] == "第23条具体事实需要核对。" for p in issued)
               for sid in ("S0", "S1"))
    target = [{"id": "C1", "text": "第23条事实"}]
    chosen = relevant_passages(sources, target, "{}", target_refs={"C1": ("R23",)})
    verdict = {"status": "revise", "issues": [
        {"target_id": "C1", "evidence_id": chosen[0]["id"], "reason": "有差异"}
    ]}
    assert resolve_passage_issues(verdict, sources, target, chosen)
    forged = [{**chosen[0], "text": "伪造"}]
    assert resolve_passage_issues(verdict, sources, target, forged) is None

def test_ambiguous_and_forged_identifiers_are_not_projected():
    from agent_core.core.flow_review_passages import source_record_ranges
    rows = [{"id": "R1", "detail": "正常"}, {"id": "R1", "item_id": "R2"},
            {"id": "R3", "item_id": None}]
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "a.json",
              "text": json.dumps(rows, ensure_ascii=False)}
    assert source_record_ranges([source])["S"] == []
    source["text"] = r'[{"id":"R\u0034","detail":"伪造 \"id\":\"R4\""}]'
    assert source_record_ranges([source])["S"] == []

def test_multiple_refs_each_source_and_unidentified_rows_remain_issued():
    rows = [{"id": "A", "detail": "甲事项具体事实。"}, {"id": "B", "detail": "乙事项具体事实。"},
            {"detail": "没有编号但有可引用的事实。"}, {"id": "D", "detail": "重复编号一。"},
            {"id": "D", "detail": "重复编号二。"}]
    sources = [{"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
                "path": "a.json", "text": json.dumps(rows, ensure_ascii=False)} for i in range(2)]
    targets = [{"id": "C", "text": "甲事项及乙事项具体事实"}]
    selected = relevant_passages(sources, targets, "{}", target_refs={"C": ("A", "B")})
    assert {(p["source_id"], p["text"]) for p in selected} >= {
        (sid, text) for sid in ("S0", "S1") for text in ("甲事项具体事实。", "乙事项具体事实。")}
    issued = review_passages(sources)
    assert all(any(p["source_id"] == sid and p["text"] == text for p in issued)
               for sid in ("S0", "S1") for text in
               ("没有编号但有可引用的事实。", "重复编号一。", "重复编号二。"))

def test_unbound_target_can_find_other_record_and_projection_limits():
    from agent_core.core.flow_review_passages import source_record_ranges
    sources = [{"id": "S", "kind": "read_file", "scope": "observed_span", "path": "a.json",
                "text": json.dumps([{"id": "A", "detail": "甲事项说明。"},
                                    {"id": "B", "detail": "乙事项故障。"}], ensure_ascii=False)}]
    targets = [{"id": "C1", "text": "甲事项"}, {"id": "C2", "text": "乙事项故障"}]
    selected = relevant_passages(sources, targets, "{}", target_refs={"C1": ("A",)})
    assert any(p["text"] == "乙事项故障。" for p in selected)
    assert source_record_ranges(sources * 129) == {}
    assert source_record_ranges([{**sources[0], "text": " " * 1_000_001}]) == {}


def test_two_refs_are_covered_before_many_unbound_extras():
    rows = [{"id": "A", "detail": "甲事实发生。"}, {"id": "B", "detail": "乙事实发生。"}]
    sources = [{"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
                "path": "a.json", "text": json.dumps(rows, ensure_ascii=False)} for i in range(2)]
    sources.extend({"id": f"U{i}", "kind": "user_input", "scope": "user_report",
                    "text": f"共享事实{i}。"} for i in range(10))
    target = [{"id": "C", "text": "甲事实及乙事实"}]
    selected = relevant_passages(sources, target, "{}", target_refs={"C": ("A", "B")})
    assert {(p["source_id"], p["text"]) for p in selected} >= {
        (sid, text) for sid in ("S0", "S1") for text in ("甲事实发生。", "乙事实发生。")}
    assert len(selected) <= 12 and sum(len(p["text"]) for p in selected) <= 1200

def test_source_record_ranges_rejects_many_distinct_sources_before_parsing():
    from agent_core.core.flow_review_passages import source_record_ranges
    sources = [{"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
                "path": "a.json", "text": '[{"id":"A"}]'} for i in range(129)]
    assert source_record_ranges(sources) == {}

def test_jsonl_without_identifiers_is_still_fairly_issued():
    sources = [{"id": f"S{i}", "kind": "read_file", "scope": "observed_span",
                "path": "a.jsonl", "text": '\n'.join(
                    json.dumps({"detail": f"来源{i}第{n}条事实。"}, ensure_ascii=False)
                    for n in range(20))} for i in range(2)]
    issued = review_passages(sources)
    assert all(any(p["source_id"] == f"S{i}" and p["text"] == f"来源{i}第19条事实。"
                   for p in issued) for i in range(2))

def test_json_string_array_policy_remains_issued_among_record_sources():
    records = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "records.json",
               "text": '[{"id":"A","detail":"记录事实。"}]'}
    policy = {"id": "P", "kind": "read_file", "scope": "observed_span", "path": "policy.json",
              "text": '["审批后可以处理。","禁止未经确认操作。"]'}
    passages = review_passages([records, policy])
    assert any(p["source_id"] == "P" and p["text"] == "审批后可以处理。" for p in passages)
    assert any(p["source_id"] == "P" and p["text"] == "禁止未经确认操作。" for p in passages)
    assert review_passages([{**policy, "text": '{"a":"原文。","a":"伪造。"}'}]) == []
    assert review_passages([{**policy, "path": "a.jsonl",
                             "text": '{"detail":"原文。"}\n{"detail":'}]) == []


def test_fair_record_spans_streams_before_consuming_all_sentences(monkeypatch):
    from agent_core.core import flow_review_passages as module

    consumed = []

    def spans(text, start, end):
        for i in range(10_000):
            consumed.append(i)
            yield i, i + 1, "A" if i == 0 else "事实。"

    monkeypatch.setattr(module, "_record_spans", spans)
    stream = module._fair_record_spans("", [({"id": "A"}, 0, 1)])
    assert next(stream)[2] == "事实。"
    assert len(consumed) == 2
