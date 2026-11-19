from agent_core.core.flow_review_passages import review_passages, resolve_passage_issues, relevant_passages
from agent_core.core.flow_review_claims import claim_feedback, INVALID_REVIEW


def test_exact_passages_and_resolution():
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": '{"rule":"若未审批，不得操作。", "note":"客户说：\\\"暂不处理\\\"。"}'}]
    targets = [{"id": "C1", "text": "可以直接操作。"}]
    passages = review_passages(sources)
    assert any(p["text"] == "若未审批，不得操作。" for p in passages)
    assert all(sources[0]["text"][p["start"]:p["end"]] == p["text"] for p in passages)
    p = next(p for p in passages if p["text"] == "若未审批，不得操作。")
    verdict = {"status": "revise", "issues": [{"target_id": "C1", "evidence_id": p["id"], "reason": "缺少条件"}]}
    assert resolve_passage_issues(verdict, sources, targets, passages) == {"status": "revise", "issues": [{"target_id": "C1", "draft_quote": targets[0]["text"], "source_id": "S1", "quote": p["text"], "reason": "缺少条件"}]}
    assert resolve_passage_issues(verdict, [{**sources[0], "text": "已变更"}], targets, passages) is None
    assert resolve_passage_issues({**verdict, "extra": 1}, sources, targets, passages) is None
    assert resolve_passage_issues({"status": "pass"}, sources, targets, passages) is None


def test_bounds_and_invalid_inputs():
    assert review_passages([{"id": "S", "text": "a"}, {"id": "S", "text": "b"}]) == []
    assert review_passages([{"id": "S", "text": "x" * 161}]) == []
    sources = [{"id": "S", "kind": "rag_search", "scope": "observed_span", "text": "".join(f"第{i}条规则。" for i in range(200))}]
    passages = review_passages(sources)
    assert len(passages) <= 64 and sum(len(p["text"]) for p in passages) <= 8192
    assert all(len(p["text"]) <= 160 for p in passages)


def test_forgery_cross_source_and_multiple_issues():
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": "若获审批，则可操作。"},
               {"id": "S2", "kind": "rag_search", "scope": "observed_span", "text": "不得转交。"}]
    targets = [{"id": "C1", "text": "无需审批。"}, {"id": "C2", "text": "可以转交。"}]
    passages = review_passages(sources)
    verdict = {"status": "revise", "issues": [
        {"target_id": "C1", "evidence_id": "E1", "reason": "缺少审批"},
        {"target_id": "C2", "evidence_id": "E2", "reason": "禁止转交"}]}
    converted = resolve_passage_issues(verdict, sources, targets, passages)
    assert converted and claim_feedback(converted, sources, targets) != INVALID_REVIEW
    forged = [dict(p) for p in passages]
    forged[0]["source_id"] = "S2"
    assert resolve_passage_issues(verdict, sources, targets, forged) is None
    forged = [dict(p) for p in passages]
    forged[0]["text"] = "审批"
    assert resolve_passage_issues(verdict, sources, targets, forged) is None
    assert resolve_passage_issues(verdict, sources, targets, passages + passages[:1]) is None
    assert resolve_passage_issues(verdict, sources, targets + targets[:1], passages) is None
    verdict["issues"][0]["evidence_id"] = "E99"
    assert resolve_passage_issues(verdict, sources, targets, passages) is None


def test_ndjson_exact_offsets_selection_and_resolution():
    text = '  {"id":"A", "message":"首条中文原句。"}\r\n\r\n {"id":"B", "message":"第二条需要审批。", "escaped":"不可\\u4ee5"}\r\n'
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "records.NDJSON", "text": text}
    targets = [{"id": "C", "text": "第二条无需审批。"}]
    passages = review_passages([source])
    assert [p["text"] for p in passages] == ["A", "首条中文原句。", "B", "第二条需要审批。"]
    assert all(text[p["start"]:p["end"]] == p["text"] for p in passages)
    selected = relevant_passages([source], targets, '{"id":"B"}')
    assert any(p["text"] == "第二条需要审批。" for p in selected)
    assert not any(p["text"] == "首条中文原句。" for p in selected)
    passage = next(p for p in selected if p["text"] == "第二条需要审批。")
    verdict = {"status": "revise", "issues": [{"target_id": "C", "evidence_id": passage["id"], "reason": "条件不符"}]}
    assert resolve_passage_issues(verdict, [source], targets, selected)
    verdict["issues"][0]["evidence_id"] = "E999"
    assert resolve_passage_issues(verdict, [source], targets, selected) is None


def test_ndjson_rejects_invalid_and_untrusted_sources():
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "x.jsonl", "text": '{"a":"甲。"}\n{"a":"乙。"}\n'}
    invalid = ['{"a":"甲。"}\n{"a":"乙。"}\n{"a":', '{"a":"甲。"}\n{"a":"乙。","a":"丙。"}', '{"a":"甲。"}\n{"a":NaN}', '{"a":"甲。"}{"a":"乙。"}\n', '{"a":"甲。"}\n普通文本', '{"a":"甲。"}\n{"a":"甲。"}', '{"a":"甲。"}\n[1,2]']
    for text in invalid:
        assert review_passages([{**source, "text": text}]) == []
    for change in ({"path": "x.txt"}, {"path": ""}, {"kind": "rag_search"}, {"kind": "custom_result", "scope": "tool_report"}, {"scope": "unverified"}):
        assert review_passages([{**source, **change}]) == []
    assert review_passages([{k: v for k, v in source.items() if k != "path"}]) == []


def test_ndjson_limits_and_ordinary_json():
    import json
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "x.jsonl", "text": "\n".join(json.dumps({"message": f"原句{i}。"}, ensure_ascii=False) for i in range(100))}
    passages = review_passages([source])
    assert len(passages) == 64
    assert sum(len(p["text"]) for p in passages) <= 8192
    assert len({(p["start"], p["end"]) for p in passages}) == len(passages)
    assert [p["text"] for p in review_passages([{**source, "text": "[{\"message\":\"普通原句。\"}]"}])] == ["普通原句。"]


def test_ndjson_mixed_sources_and_records_receive_fair_index():
    import json
    sources = [
        {"id": "S1", "kind": "read_file", "scope": "observed_span", "text": "".join(f"说明{i}。" for i in range(12))},
        {"id": "S2", "kind": "read_file", "scope": "observed_span", "text": "".join(f"反馈{i}。" for i in range(44))},
        {"id": "S3", "kind": "read_file", "scope": "observed_span", "path": "records.jsonl", "text": "\n".join(json.dumps({"id": identifier, "message": "".join(f"{identifier}记录{i}。" for i in range(count))}, ensure_ascii=False) for identifier, count in (("A", 15), ("B", 17)))},
        {"id": "S4", "kind": "user_input", "scope": "user_report", "text": "".join(f"直接用户{i}。" for i in range(4))},
    ]
    passages = review_passages(sources)
    assert len(passages) == 64
    assert {p["source_id"] for p in passages} == {"S1", "S2", "S3", "S4"}
    assert any(p["text"].startswith("A记录") for p in passages)
    assert any(p["text"].startswith("B记录") for p in passages)
    assert all(next(s for s in sources if s["id"] == p["source_id"])["text"][p["start"]:p["end"]] == p["text"] for p in passages)
    selected = relevant_passages(sources, [{"id": "C", "text": "B记录"}], '{"id":"B"}')
    assert any(p["text"].startswith("B记录") for p in selected)
    assert not any(p["text"].startswith("A记录") for p in selected)
    verdict = {"status": "revise", "issues": [{"target_id": "C", "evidence_id": next(p["id"] for p in passages if p["text"].startswith("A记录")), "reason": "错误归属"}]}
    assert resolve_passage_issues(verdict, sources, [{"id": "C", "text": "B记录"}], selected) is None


def test_ndjson_rejects_overflow_and_keeps_unknown_record_identity():
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "path": "x.jsonl", "text": '{"id":"A","message":"甲原文。"}\n{"message":"未知原文。"}'}
    selected = relevant_passages([source], [{"id": "C", "text": "未知原文。"}], '{"id":"B"}')
    assert any(p["text"] == "未知原文。" for p in selected)
    assert review_passages([{**source, "text": '{"a":"甲。"}\n{"a":1e999}'}]) == []

    import json
    many = [{"id": f"T{i}", "message": f"记录{i}原文。"} for i in range(70)]
    source["text"] = "\n".join(json.dumps(row, ensure_ascii=False) for row in many)
    selected = relevant_passages([source], [{"id": "C", "text": "记录1原文。"}], '{"id":"T2"}')
    assert all(p["text"] != "记录1原文。" for p in selected)


def test_relevant_passages_item_ref_and_stale_subset():
    sources = [{"id": "S1", "kind": "rag_search", "scope": "observed_span", "item_ref": "T1", "text": "用户的原始表述。"},
               {"id": "S2", "kind": "rag_search", "scope": "observed_span", "text": "审批后才能操作。"}]
    targets = [{"id": "C1", "text": "审批前就能操作。"}]
    selected = relevant_passages(sources, targets, '{"ticket_id":"T1"}')
    assert {p["source_id"] for p in selected} == {"S1", "S2"}
    verdict = {"status": "revise", "issues": [{"target_id": "C1", "evidence_id": selected[0]["id"], "reason": "不符"}]}
    stale = [{**sources[0], "text": "用户的其他表述。"}, sources[1]]
    assert resolve_passage_issues(verdict, stale, targets, selected) is None
    forged = [dict(p) for p in selected]
    forged[0]["source_id"] = "S2"
    assert resolve_passage_issues(verdict, sources, targets, forged) is None
    assert relevant_passages(sources, targets, '{"id":"T1","item_id":"T2"}') == [selected[1]]


def test_json_escapes_unknown_sources_and_limits():
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "text": '{"a":"必须先审批；否则不得操作。","b":"客户说\\\"不处理\\\"。"}'}
    passages = review_passages([source])
    assert [p["text"] for p in passages] == ["必须先审批；否则不得操作。"]
    assert review_passages([{**source, "kind": "unrecognized"}]) == []
    assert review_passages([{**source, "scope": "unverified"}]) == []
    assert review_passages([{**source, "text": '{"x":"甲", "x":"乙"}'}]) == []
    assert review_passages([{**source, "text": "x" * 1_000_001}]) == []
    assert review_passages([{**source, "text": "x" * 161 + "。"}]) == []


def test_relevant_subset_preserves_matching_record_and_issued_ids():
    import json
    rows = [{"id": f"T{i}", "message": f"用户反馈第{i}条原文。"} for i in range(20)]
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": json.dumps(rows, ensure_ascii=False)},
               {"id": "S2", "kind": "rag_search", "scope": "observed_span", "text": "政策要求审批后操作。"}]
    targets = [{"id": "C1", "text": "用户反馈第17条并要求审批后操作。"}]
    passages = relevant_passages(sources, targets, '{"id":"T17","summary":"用户反馈"}')
    assert len(passages) <= 12 and sum(len(p["text"]) for p in passages) <= 1200
    assert any(p["text"] == rows[17]["message"] for p in passages)
    assert any(p["source_id"] == "S2" for p in passages)
    assert all(p in review_passages(sources) for p in passages)
    verdict = {"status": "revise", "issues": [{"target_id": "C1", "evidence_id": passages[0]["id"], "reason": "差异"}]}
    assert resolve_passage_issues(verdict, sources, targets, passages)
    verdict["issues"][0]["evidence_id"] = next(p["id"] for p in review_passages(sources) if p not in passages)
    assert resolve_passage_issues(verdict, sources, targets, passages) is None
    assert resolve_passage_issues(verdict, sources, targets, passages + passages[:1]) is None
