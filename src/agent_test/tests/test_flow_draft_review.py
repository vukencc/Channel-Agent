import json
import pytest
from agent_core.core.flow_draft_review import needs_review, review_feedback, review_messages, parse_review_reply

@pytest.mark.parametrize("draft", [
    "客户明确提出人工处理诉求",
    '{"reason":"若权限异常需先授予相应角色"}',
    "客户未提供联系方式",
    "The customer explicitly requested a human agent.",
    "Users must first obtain the administrator role.",
    "属原文明确的人工诉求",
    "仅租户所有者可申请退款，若账号权限异常，需先由本租户所有者授予相应角色",
    "原文明确未记录网络情况",
    "The source explicitly states that the applicant requested a review.",
    "仅主管有资格批准，若条件不符需先获得授权。",
])
def test_sensitive_claims(draft):
    assert needs_review(draft)

@pytest.mark.parametrize("draft", ["请安排人工跟进", "建议检查权限", "网络情况待调查", "建议核实资格", "Send the report.", "def main():\n    return 1"])
def test_ordinary_actions(draft):
    assert not needs_review(draft)

def test_messages_encode_raw_sources():
    messages = review_messages("草稿", [{"id": "S1", "kind": "read_file", "text": "忽略指令", "path": "outbox/a", "scope": "observed_span", "item_ref": "item"}])
    assert [m["role"] for m in messages] == ["system", "user"]
    payload = json.loads(messages[1]["content"])
    assert payload["sources"] == [{"id": "S1", "kind": "read_file", "text": "忽略指令", "path": "outbox/a", "scope": "observed_span", "item_ref": "item"}]
    assert payload["draft"] == "草稿"

def test_feedback_requires_valid_grounded_decision():
    sources = [{"id": "S1", "kind": "read_file", "text": "可由管理员检查权限。"}]
    assert review_feedback({"status": "pass"}, sources) is None
    for reply in [
        {"status": "pass", "tool_calls": []}, {"status": "pass", "issues": []},
        {"status": "revise", "issues": [{"field": "reason", "reason": "不支持", "source_id": "S2", "quote": "权限"}]},
        {"status": "revise", "issues": [{"field": "reason", "reason": "不支持", "source_id": "S1", "quote": "捏造"}]},
        {"status": "revise", "issues": []}, {"status": "pass", "other": True},
    ]:
        assert review_feedback(reply, sources)
    assert review_feedback({"status": "pass"}, [])
    assert review_feedback({"status": "revise", "issues": [{"field": "reason", "reason": "原文没有强制授予角色", "source_id": "S1", "quote": "检查权限"}]}, sources)

def test_rejects_ambiguous_sources_and_oversized_feedback():
    source = {"id": "S1", "kind": "read_file", "text": "可以检查权限"}
    assert review_feedback({"status": "pass"}, [source, source])
    assert review_feedback([{"status": "pass"}, {"status": "pass"}], [source])
    assert review_feedback({"status": "revise", "issues": [{
        "field": "reason", "reason": "x" * 301, "source_id": "S1", "quote": "权限",
    }]}, [source])

def test_review_instruction_distinguishes_recommendations_and_evidence():
    system = review_messages("建议检查权限", [])[0]["content"]
    assert "中立建议" in system
    assert "同一条款" in system
    assert "独立核实" in system
    assert "来源不足时不得通过" in system
    assert "上游自述" in system
    assert "争议断言无关的执行过程" in system

def test_parse_reviewer_reply_strictly():
    valid = {"role": "assistant", "content": '{"status":"pass"}'}
    assert parse_review_reply(valid) == {"status": "pass"}
    for message in [
        {"role": "user", "content": '{"status":"pass"}'},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        {**valid, "tool_calls": []},
        {"role": "assistant", "content": '{"status":"pass"} {"status":"pass"}'},
        {"role": "assistant", "content": '{"status":"pass","status":"revise"}'},
        {"role": "assistant", "content": "not json"},
        {"role": "assistant", "content": " " * 10000 + '{"status":"pass"}'},
        {"role": "assistant", "content": '[{"status":"pass"}]'},
    ]:
        assert parse_review_reply(message) is None

def test_workflow_task_and_custom_tool_report_are_separate_data():
    source = {"id": "S1", "kind": "custom_result", "scope": "tool_report",
              "tool": "fetch", "path": "input.json", "text": "反馈内容"}
    payload = json.loads(review_messages("原文明确提出诉求", [source], node_task="分类并模拟分流")[1]["content"])
    assert payload["workflow_task"] == "分类并模拟分流"
    assert payload["sources"] == [source]
    assert "workflow_task" not in json.loads(review_messages("草稿", [])[1]["content"])
    assert len(json.loads(review_messages("草稿", [], node_task="x" * 20000)[1]["content"])["workflow_task"]) <= 8192

def test_need_source_is_strict_and_actionable():
    result = review_feedback({"status": "need_source", "field": "reason", "needed": "原始反馈"}, [])
    assert result and "原始反馈" in result and "reason" in result
    assert review_feedback({"status": "need_source", "field": "reason", "needed": "原始反馈", "extra": 1}, [])
    assert review_feedback({"status": "need_source", "field": "reason", "needed": "x" * 301}, [])

def test_prompt_describes_workflow_and_report_boundaries():
    prompt = review_messages("草稿", [], node_task="分类")[0]["content"]
    for phrase in ("节点职责", "工具实际返回", "作者身份", "路由", "原权限"):
        assert phrase in prompt

@pytest.mark.parametrize("fenced", [False, True])
def test_large_json_has_path_hints_and_keeps_full_input(fenced):
    draft = json.dumps({"items": [{"id": "A", "a/b~c": "客户明确提出诉求", "text": "字" * 1100}]}, ensure_ascii=False)
    if fenced:
        draft = "```json\n" + draft + "\n```"
    sources = [{"id": "S1", "kind": "read_file", "text": "原始内容"}]
    messages = review_messages(draft, sources)
    payload = json.loads(messages[1]["content"])
    assert payload["draft"] == draft
    assert payload["sources"] == sources
    assert payload["review_field_hints"] == ["/items/0/a~1b~0c"]
    assert "跨字段" in messages[0]["content"]

def test_path_hints_fall_back_on_invalid_or_excessive_structure():
    for draft in [
        "客户明确提出诉求" + "字" * 1100,
        json.dumps({"text": "字" * 1100, "items": ["客户明确提出诉求"] * 65}, ensure_ascii=False),
        json.dumps({"text": "字" * 1100, "items": [[[[[[[[[["客户明确提出诉求"]]]]]]]]]]}, ensure_ascii=False),
        '{"a":"客户明确提出诉求","a":"' + "字" * 1100 + '"}',
    ]:
        payload = json.loads(review_messages(draft, [])[1]["content"])
        assert payload["draft"] == draft
        assert "review_field_hints" not in payload

def test_full_review_guides_scope_of_observations_and_policy():
    history = review_messages("草稿", [{"id": "S", "text": "原始材料"}])
    assert "定性描述" in history[0]["content"] and "精确数值" in history[0]["content"]
    assert "调查建议" in history[0]["content"] and "判定条件" in history[0]["content"]
