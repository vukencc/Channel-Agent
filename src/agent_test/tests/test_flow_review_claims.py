import json

import pytest

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


def test_focus_separates_referenced_subjects_from_actual_claims():
    from agent_core.core.flow_review_claims import focal_targets
    draft = json.dumps({"summary": "客户反馈页面有延迟。", "assessment":
                        "本次给用户提供了排查方向。客户没有说明延迟从何时开始；客户要求先查服务日志。",
                        "reference": "具体授权条件见附件。"}, ensure_ascii=False)
    targets = claim_targets(draft)
    focal = focal_targets(targets)
    assert len(focal) == 1 and "客户没有说明" in focal[0]["text"] and "客户要求" in focal[0]["text"]
    assert any("客户反馈" in t["text"] for t in targets)
    # 保留所有原始字段，审查员可依据上下文指出未覆盖的实质疑义。
    payload = json.loads(claim_messages(draft, [{"id": "S", "text": "用户原文"}], targets=focal)[1]["content"])
    assert payload["draft"] == draft


def test_cross_field_or_conditional_uncertainty_remains_in_scope():
    from agent_core.core.flow_review_claims import focal_targets
    for record in ({"who": "客户", "action": "明确希望转接", "other": "政策要求核验地址。"},
                   {"policy": "资料核验。", "other": "客户要求延后处理。"},
                   {"remark": "除非客户另行确认。", "other": "政策要求提交申请。"},
                   {"summary": "客户从未付款。", "other": "政策要求提交申请。"}):
        raw = claim_targets(json.dumps(record, ensure_ascii=False))
        assert len(raw) > 1 and focal_targets(raw) == raw


def test_long_focal_sentence_reaches_reviewer_without_offset_reconstruction():
    claim = "客户表示" + "描述出现的现象、" * 14 + "并要求查明原因。"
    draft = json.dumps({"observed": claim, "next": "准备排查。"}, ensure_ascii=False)
    targets = claim_targets(draft)
    payload = json.loads(claim_messages(draft, [{"id": "S", "text": "反馈原文"}], targets=targets)[1]["content"])
    assert payload["claim_texts"] == {targets[0]["id"]: claim}
    assert payload["targets"][0]["excerpt"] == claim[:64]


def test_referenced_verdict_resolves_both_sides_and_preserves_roles():
    from agent_core.core.flow_review_claims import action_review_context, claim_assessment, prepare_claim_request
    draft = '{"reason":"客户要求停止服务。"}'
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": "用户表示页面缓慢。"}]
    targets = claim_targets(draft)
    action = action_review_context({"name": "record_case", "description": "记录初步判断",
              "code": "private_code", "parameters": {"type": "object", "properties": {
                  "reason": {"type":"string","description":"节点给出的评估","default":"private_default"}}}})
    history, passages = prepare_claim_request(draft, sources, targets=targets, node_task="排查问题",
                                            action_context=action, char_limit=24000, token_limit=16000)
    payload = json.loads(history[1]["content"])
    assert "private_" not in history[1]["content"] and payload["action_context"]["name"] == "record_case"
    assert payload["sources"] == sources and payload["draft"] == draft
    assert payload["evidence_passages"][0]["id"] == "E1" and len(passages) == 1
    verdict = {"status": "revise", "issues": [{"target_id": "C1", "evidence_id": "E1", "reason": "原文未提出停服。"}]}
    feedback, invalid = claim_assessment(verdict, sources, targets, passages)
    assert invalid is None and "客户要求停止服务。" in feedback and "用户表示页面缓慢。" in feedback
    verdict["issues"][0]["evidence_id"] = "E9999"
    feedback, invalid = claim_assessment(verdict, sources, targets, passages)
    assert feedback == INVALID_REVIEW and invalid == "unverified_reference"
    assert claim_assessment({"status": "pass"}, sources, targets, passages) == (None, None)


def test_budget_fallback_preserves_original_sources_without_offering_unissued_ids():
    from agent_core.core.context import history_size
    from agent_core.core.flow_review_claims import prepare_claim_request
    draft = "客户明确要求" + "我们核对记录，" * 20 + "然后继续。"
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": "用户说明页面缓慢。"}]
    targets = claim_targets(draft)
    original = claim_messages(draft, sources, targets=targets)
    payload = json.loads(original[1]["content"])
    del payload["claim_texts"]
    original[1]["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    limit = history_size(original)
    history, passages = prepare_claim_request(draft, sources, targets=targets, char_limit=limit, token_limit=16000)
    assert history_size(history) <= limit and passages == []
    assert json.loads(history[1]["content"]) == payload


def test_wire_catalog_bounds_preserve_sources_and_literal_fallback():
    from agent_core.core.flow_review_claims import claim_assessment, prepare_claim_request
    rows = [{"id": f"ticket-{i}", "message": f"客户报告页面显示第{i}条提示。"} for i in range(25)]
    sources = [{"id": "S1", "kind": "read_file", "scope": "observed_span", "text": json.dumps(rows, ensure_ascii=False)}]
    draft = json.dumps({"ticket_id": "ticket-14", "message": "客户报告页面显示第3条提示。"}, ensure_ascii=False)
    targets = claim_targets(draft)
    history, passages = prepare_claim_request(draft, sources, targets=targets, node_task="核查本条反馈",
                                             char_limit=32000, token_limit=16000)
    payload = json.loads(history[1]["content"])
    assert 0 < len(passages) <= 12 and sum(len(p["text"]) for p in passages) <= 1200
    assert payload["draft"] == draft and payload["sources"] == sources and payload["workflow_task"] == "核查本条反馈"
    assert rows[14]["message"] in [p["text"] for p in passages]
    assert [p["id"] for p in passages] == [p["id"] for p in payload["evidence_passages"]]
    assert rows[3]["message"] not in [p["text"] for p in passages]
    target = next(t for t in targets if "客户报告" in t["text"])
    literal = {"status": "revise", "issues": [{"target_id": target["id"], "draft_quote": target["text"],
               "source_id": "S1", "quote": rows[3]["message"], "reason": "草稿引用了另一个事项。"}]}
    feedback, invalid = claim_assessment(literal, sources, targets, passages)
    assert invalid is None and target["text"] in feedback


def test_multiple_issues_can_mix_issued_ids_and_exact_quotes():
    from agent_core.core.flow_review_claims import claim_assessment
    from agent_core.core.flow_review_passages import review_passages
    sources = [{"id": "S1", "kind": "user_input", "scope": "user_report", "text": "用户报告页面卡顿。"},
               {"id": "S2", "kind": "rag_search", "scope": "observed_span", "text": "提交申请后等待审核。"}]
    targets = [{"id": "C1", "text": "用户要求转人工。"}, {"id": "C2", "text": "必须由所有者申请。"}]
    offered = review_passages(sources)[:1]
    indexed = {"target_id": "C1", "evidence_id": offered[0]["id"], "reason": "客户只报告卡顿。"}
    literal = {"target_id": "C2", "draft_quote": "必须由所有者申请。", "source_id": "S2",
               "quote": "提交申请后等待审核。", "reason": "条款未限定申请人。"}
    for issues in ([indexed, literal], [literal, indexed]):
        feedback, invalid = claim_assessment({"status": "revise", "issues": issues}, sources, targets, offered)
        assert invalid is None and feedback.index(issues[0]["reason"]) < feedback.index(issues[1]["reason"])
    for bad, expected in (({**indexed, "evidence_id": "E2"}, "unverified_reference"),
                          ({**literal, "quote": "申请自动批准。"}, "source_quote_not_found"),
                          ({**indexed, "quote": "混用字段"}, "unverified_reference")):
        other = literal if "evidence_id" in bad else indexed
        assert claim_assessment({"status": "revise", "issues": [other, bad]}, sources, targets, offered) == (
            INVALID_REVIEW, expected)


def test_verified_citation_locations_are_collected_only_after_a_complete_revise():
    from agent_core.core.flow_review_claims import claim_assessment
    from agent_core.core.flow_review_passages import review_passages

    text = "资料甲。核对原始条款。资料丙。核对原始条款。"
    src = [{"id": "s", "kind": "read_file", "scope": "observed_span", "text": text}]
    ts = [{"id": "C4", "field": "/note", "text": "用户要求直接处理。"}]
    parts = review_passages(src)
    part = parts[-1]
    issue = {"target_id": "C4", "evidence_id": part["id"], "reason": "要求范围有差异"}
    records = {}
    verdict = {"status": "revise", "issues": [issue]}
    message, invalid = claim_assessment(verdict, src, ts, [part], cited_spans=records)
    assert message and invalid is None
    assert records == {"C4": [{"source_id": "s", "start": part["start"], "end": part["end"],
                               "quote": part["text"]}]}
    records.clear()
    broken = {"status": "revise", "issues": [issue, {**issue, "evidence_id": "not-issued"}]}
    assert claim_assessment(broken, src, ts, [part], cited_spans=records)[1] == "unverified_reference"
    assert records == {}
    literal = {"target_id": "C4", "source_id": "s", "draft_quote": "要求直接处理",
               "quote": part["text"], "reason": "应核对原件"}
    verdict = {"status": "revise", "issues": [literal]}
    assert claim_assessment(verdict, src, ts, [part], cited_spans=records)[1] is None
    assert records == {}
    literal["quote"] = "资料丙。"
    assert claim_assessment(verdict, src, ts, [], cited_spans=records)[1] is None
    start = text.index("资料丙。")
    assert records == {"C4": [{"source_id": "s", "start": start, "end": start + 4, "quote": "资料丙。"}]}


@pytest.mark.parametrize("kind,expected", [("read_file", True), ("rag_search", False)])
def test_claim_reviewer_reads_the_provenance_of_the_workspace_memory_entry(kind, expected):
    from agent_core.core.flow_draft_review import MEMORY_GUIDANCE
    from agent_core.core.flow_review_claims import claim_targets, prepare_claim_request

    draft = "客户明确要求重新核实。"
    source = {"id": "S", "kind": kind, "scope": "observed_span", "text": "客户原来报告过问题。",
              "path": ".channelagent/memory.json", "workspace_memory_path": True}
    for whole in (False, True):
        history, _ = prepare_claim_request(draft, [source], targets=claim_targets(draft),
                                           whole_batch=whole, char_limit=100_000, token_limit=32_000)
        payload = json.loads(history[1]["content"])
        assert (MEMORY_GUIDANCE in history[0]["content"]) is expected
        assert (payload["sources"][0].get("workspace_memory_path") is True) is expected
        assert payload["sources"][0]["text"] == source["text"]


@pytest.mark.parametrize("meter", ["history_size", "request_tokens"])
def test_tight_recheck_budgets_drop_edits_then_navigation_without_losing_the_previous_payload(monkeypatch, meter):
    from agent_core.core import flow_review_claims as claims

    draft = "客户明确要求重新核实。"
    src = [{"id": "s", "kind": "read_file", "scope": "observed_span", "text": "客户曾报告过故障。"}]
    target = claims.claim_targets(draft)
    base, current_e = claims.prepare_claim_request(draft, src, targets=target, char_limit=100000, token_limit=32000)
    navigation = {"source_locations": [{"field": "/", "source_id": "s", "start": 0, "end": len(src[0]["text"]),
                                        "quote": src[0]["text"]}],
                  "edits": [{"field": "/", "start": 0, "end": 2}]}
    limit = 32000
    actual = getattr(claims, meter)
    def size(history):
        detail = json.loads(history[1]["content"]).get("revision_context") or {}
        return limit + 1 if detail.get("edits") else actual(history)
    monkeypatch.setattr(claims, meter, size)
    packed = claims.with_revision_navigation(base, target, navigation, char_limit=limit, token_limit=limit)
    summary = json.loads(packed[1]["content"])
    assert summary["revision_context"] == {"source_locations": navigation["source_locations"]}
    assert summary["draft"] == draft and summary["sources"] == src
    assert [e["id"] for e in summary.get("evidence_passages", [])] == [e["id"] for e in current_e]
    monkeypatch.setattr(claims, meter, lambda hist: limit + 1 if "revision_context" in json.loads(hist[1]["content"]) else actual(hist))
    assert claims.with_revision_navigation(base, target, navigation, char_limit=limit, token_limit=limit) == base

def test_verified_feedback_locates_independent_errors_in_repeated_draft_text():
    draft = json.dumps({"one/a": "客户明确要求。", "two~b": "客户明确要求。"}, ensure_ascii=False)
    sources = [{"id": "S1", "text": "样本有定性描述。仅按影响分级。"}]
    targets = claim_targets(draft)
    issues = [{"target_id": row["id"], "draft_quote": row["text"], "source_id": "S1",
               "quote": quote, "reason": reason} for row, quote, reason in zip(
                   targets, ("样本有定性描述。", "仅按影响分级。"), ("精度不同", "附加条件"), strict=True)]
    feedback = claim_feedback({"status": "revise", "issues": issues}, sources, targets)
    for target, issue in zip(targets, issues, strict=True):
        for value in (target["field"], issue["draft_quote"], issue["quote"], issue["reason"]):
            assert value in feedback
    bad_issues = [issues[0], {**issues[1], "quote": "其他材料"}]
    assert claim_feedback({"status": "revise", "issues": bad_issues}, sources, targets) == INVALID_REVIEW

def test_action_and_handoff_review_distinguish_precision_from_rule_conditions():
    draft = "客户表示未提供资料，规则要求先提供样本。"
    sources = [{"id": "S", "text": "原始材料"}]
    for batched in (False, True):
        history = claim_messages(draft, sources, targets=claim_targets(draft), whole_batch=batched)
        assert "定性描述" in history[0]["content"] and "精确数值" in history[0]["content"]
        assert "调查建议" in history[0]["content"] and "判定条件" in history[0]["content"]
