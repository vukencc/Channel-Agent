from agent_core.core.flow_review_passages import review_passages, resolve_passage_issues
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


def test_json_escapes_unknown_sources_and_limits():
    source = {"id": "S", "kind": "read_file", "scope": "observed_span", "text": '{"a":"必须先审批；否则不得操作。","b":"客户说\\\"不处理\\\"。"}'}
    passages = review_passages([source])
    assert [p["text"] for p in passages] == ["必须先审批；否则不得操作。"]
    assert review_passages([{**source, "kind": "unrecognized"}]) == []
    assert review_passages([{**source, "scope": "unverified"}]) == []
    assert review_passages([{**source, "text": '{"x":"甲", "x":"乙"}'}]) == []
    assert review_passages([{**source, "text": "x" * 1_000_001}]) == []
    assert review_passages([{**source, "text": "x" * 161 + "。"}]) == []
