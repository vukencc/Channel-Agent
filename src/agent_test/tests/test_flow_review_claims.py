import json
from agent_core.core.flow_review_claims import (INVALID_REVIEW, claim_feedback, claim_messages,
                                               claim_targets, shared_targets)

def test_targets_keep_conditions_and_decode_json():
    draft = json.dumps({"a/b": "客户表示不退款，除非收到凭证；否则等待。\nCustomer said no refund unless approved."}, ensure_ascii=True)
    targets = claim_targets(draft)
    assert [t["field"] for t in targets] == ["/a~1b", "/a~1b"]
    assert targets[0]["text"] == "客户表示不退款，除非收到凭证；否则等待。"
    assert targets[1]["text"] == "Customer said no refund unless approved."
    assert targets == claim_targets("```json\n" + draft + "\n```")

def test_general_text_and_ambiguous_cross_sentence_fallback():
    assert claim_targets("客户表示没有提供凭证。后续等待。 ")[0]["text"] == "客户表示没有提供凭证。"
    assert [t["text"] for t in claim_targets("客户表示没有提供凭证。上述情况需要政策审批。")] == ["客户表示没有提供凭证。", "上述情况需要政策审批。"]
    assert claim_targets('{"客户":"未说明", "处理":"等待"}')[0]["text"] == "未说明"
    assert claim_targets('{"x":"客户表示有要求", "x":"客户表示无要求"}') == []
    assert [t["text"] for t in claim_targets('客户表示已联系。客户。没有提供凭证。')] == ['客户表示已联系。', '客户。', '没有提供凭证。']
    assert claim_targets('政策不得要求先审批，除非适用特殊条件。')[0]["text"] == '政策不得要求先审批，除非适用特殊条件。'

def test_protocol_and_checked_feedback():
    draft = "客户表示不需要审批。"
    targets = claim_targets(draft)
    sources = [{"id": "S1", "text": "客户表示需要审批。", "secret": "omit"}]
    messages = claim_messages(draft, sources, targets=targets, node_task="分类", per_item=True)
    payload = json.loads(messages[1]["content"])
    assert payload["targets"] == [{"id": "C1", "field": "/", "start": 0, "end": len(draft), "excerpt": draft}]
    assert "secret" not in payload["sources"][0]
    assert "逐字" in messages[0]["content"] and "岗位" in messages[0]["content"]
    assert "忠实转述" in messages[0]["content"] and "冒充客户或政策" in messages[0]["content"]
    assert "主体、范围、精度或必要步骤" in messages[0]["content"]
    assert "当前事项的完整记录和共享备注" in messages[0]["content"]
    assert "用户证词不等于岗位对影响的分级" in messages[0]["content"]
    issue = {"target_id": targets[0]["id"], "draft_quote": "不需要审批", "source_id": "S1", "quote": "需要审批", "reason": "否定范围相反"}
    assert "否定范围相反" in claim_feedback({"status": "revise", "issues": [issue]}, sources, targets)
    for bad in ({**issue, "draft_quote": "无审批"}, {**issue, "quote": "无需审批"}, {**issue, "target_id": "invented"}):
        feedback = claim_feedback({"status": "revise", "issues": [bad]}, sources, targets)
        assert feedback and "无效" in feedback and "请修改草稿" not in feedback
    assert claim_feedback({"status": "pass"}, sources, targets) is None
    assert claim_feedback({"status": "pass"}, sources * 2, targets) is not None
    assert "需要来源" in claim_feedback({"status": "need_source", "field": "/a", "needed": "政策"}, sources, targets)


def test_shared_claims_are_original_only_even_when_forced():
    unit = {"field": "备/注", "claims": ["普通建议。"], "context": "客户表示同意。普通建议。", "item_ids": ["A"]}
    targets = shared_targets(json.dumps(unit, ensure_ascii=False))
    assert len(targets) == 1 and targets[0]["field"] == "/备~1注"
    assert targets[0]["text"] == "普通建议。" and targets[0]["context"] == unit["context"]
    unit["claims"] = ["虚构句。"]
    assert shared_targets(json.dumps(unit, ensure_ascii=False)) == []
    unit["claims"] = ["普通建议。"] * 2
    assert shared_targets(json.dumps(unit, ensure_ascii=False)) == []


def test_long_draft_retains_existing_field_hints_and_invalid_marker():
    draft = json.dumps({"f": "客户表示不需要审批。" + "普通内容。" * 300}, ensure_ascii=False)
    targets = claim_targets(draft)
    sources = [{"id": "S1", "text": "客户表示需要审批。"}]
    payload = json.loads(claim_messages(draft, sources, targets=targets)[1]["content"])
    assert payload["review_field_hints"] == ["/f"]
    assert claim_feedback({"status": "revise", "issues": []}, sources, targets) == INVALID_REVIEW


def test_long_context_keeps_complete_target_and_neighbours():
    draft = json.dumps({"note": "普通说明。" * 200 + "客户表示不需要审批，除非适用例外。" + "后续确认。" * 200}, ensure_ascii=False)
    targets = claim_targets(draft)
    target = next(t for t in targets if "客户表示" in t["text"])
    assert target["text"] == "客户表示不需要审批，除非适用例外。"
    assert target["text"] in target["context"] and len(target["context"]) <= 800


def test_sensitive_non_text_and_empty_containers_fall_back():
    for value in (True, 1, None, {}, []):
        draft = json.dumps({"客户": {"要求人工": value}, "备注": "政策要求先申请授权。"}, ensure_ascii=False)
        assert claim_targets(draft) == []
    assert claim_targets(json.dumps({"时间": 12, "备注": "政策要求先申请授权。"}, ensure_ascii=False))


def test_compact_wire_positions_and_repeated_long_sentences():
    value = "".join(f"客户表示第{i}项" + "具体内容" * 70 + "。" for i in range(4))
    draft = json.dumps({"notes": value}, ensure_ascii=False)
    sources = [{"id": "S1", "text": "来源全文。" * 80}]
    targets = claim_targets(draft)
    messages = claim_messages(draft, sources, targets=targets)
    payload = json.loads(messages[1]["content"])
    assert payload["draft"] == draft and payload["sources"] == sources
    assert len(targets) == 4
    for wire, target in zip(payload["targets"], targets):
        assert set(wire) == {"id", "field", "start", "end", "excerpt"}
        assert value[wire["start"]:wire["end"]] == target["text"]
        assert wire["excerpt"] == target["text"][:64]
    verdict = {"status": "revise", "issues": [{"target_id": targets[0]["id"],
               "draft_quote": targets[0]["text"][-10:], "source_id": "S1",
               "quote": "来源全文", "reason": "范围不同"}]}
    assert "范围不同" in claim_feedback(verdict, sources, targets)
    assert len(messages[1]["content"]) < len(draft) + len(sources[0]["text"]) + 1000
    unit = {"field": "notes", "claims": ["普通建议。"], "context": "前文。普通建议。", "item_ids": ["A"]}
    shared_draft = json.dumps(unit, ensure_ascii=False)
    shared = shared_targets(shared_draft)
    wire = json.loads(claim_messages(shared_draft, sources, targets=shared, shared=True)[1]["content"])["targets"][0]
    assert wire["location"] == "/context" and unit["context"][wire["start"]:wire["end"]] == shared[0]["text"]
