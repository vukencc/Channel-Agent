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
