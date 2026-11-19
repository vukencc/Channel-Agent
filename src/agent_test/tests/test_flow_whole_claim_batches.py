"""交接含跨事项指代时，用受控模型回调检查进入独立审查器的完整范围。"""
import asyncio
import json

import pytest

from agent_core.core import flow_draft_gate as gate_module

from agent_core.core.flow_review_claims import claim_targets, focal_targets
from agent_core.core.flow_review_items import review_units
from agent_test.tests.test_flow_draft_receipt_review import make_gate
from agent_test.tests.test_flow_draft_review_runtime import assistant, call, runtime_store


def reply(status="pass", **values):
    return {"role": "assistant", "content": json.dumps({"status": status, **values}, ensure_ascii=False)}


def rejection(payload, *, invalid=False):
    target = payload["targets"][0]
    return reply("revise", issues=[{
        "target_id": target["id"], "draft_quote": target["excerpt"],
        "source_id": "missing" if invalid else "s1", "quote": "原始证据",
        "reason": "原文没有记录该明确要求",
    }])


def draft_with_references():
    return json.dumps({
        "items": [
            {"id": "A1", "observations": [f"客户明确提出需要查证第{i}个条目。" for i in range(1, 6)]},
            {"id": "A2", "observations": [f"客户表示希望核对第{i}项，并参考上述记录。" for i in range(6, 10)]},
        ],
        "memo": "两者的记录需要结合阅读。",
    }, ensure_ascii=False)


def test_ambiguous_record_handoff_preserves_all_evidence_and_checks_every_target(monkeypatch):
    gate, index, histories = make_gate(monkeypatch)
    draft = draft_with_references()
    targets = focal_targets(claim_targets(draft))
    assert review_units(draft, []) is None
    assert len(targets) >= 9

    feedback = asyncio.run(gate.check(draft, stage="handoff"))
    assert feedback is None
    payloads = [json.loads(history[1]["content"]) for history in histories]
    assert len(payloads) >= 3
    assert all(1 <= len(payload["targets"]) <= 4 for payload in payloads)
    assert all(json.loads(payload["draft"]) == json.loads(draft) for payload in payloads)
    assert all(payload["sources"] == [{"id": "s1", "kind": "read_file", "text": "原始证据"}] for payload in payloads)
    assert all(payload["action_receipt_summary"] == index.facts for payload in payloads)
    expected = [(target["id"], target["field"], target["start"], target["end"]) for target in targets]
    actual = [(row["id"], row["field"], row["start"], row["end"]) for p in payloads for row in p["targets"]]
    assert actual == expected


@pytest.mark.parametrize("failure", ["revise", "need_source", "invalid", "timeout", "cancel", "rounds"])
def test_later_batch_failure_never_approves_partial_handoff(monkeypatch, failure):
    """受控离线响应；第一批通过不代表完整草稿获批。"""
    gate, _, histories = make_gate(monkeypatch)
    rounds = iter([True, False])
    if failure == "rounds":
        gate.reserve_round = lambda: next(rounds, False)

    async def model(history, **kwargs):
        histories.append(history)
        payload = json.loads(history[1]["content"])
        if len(histories) == 1:
            return reply()
        if failure == "timeout":
            raise TimeoutError("受控离线超时")
        if failure == "cancel":
            gate.session.cancelled.set()
            return reply()
        if failure == "need_source":
            return reply("need_source", field="items", needed="该事项原始记录")
        return rejection(payload, invalid=failure == "invalid")

    gate.manager._call_model = model
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    else:
        assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    event = gate.session.record["last_run"]["draft_reviews"][-1]
    assert event["status"] != "passed"
    assert event["batches"][0]["status"] == "passed"
    assert len(event["batches"]) == 2
    assert gate._whole_pending
    assert not gate.passed_items
    assert len(histories) == (1 if failure == "rounds" else 3 if failure in {"invalid", "timeout"} else 2)
    if failure == "need_source":
        assert gate.needs_sources


@pytest.mark.parametrize("failure", ["timeout", "invalid"])
def test_all_whole_batches_share_one_recovery_opportunity(monkeypatch, failure):
    gate, _, histories = make_gate(monkeypatch)

    async def model(history, **kwargs):
        histories.append(history)
        if len(histories) in {1, 3}:
            if failure == "timeout":
                raise TimeoutError("受控离线超时")
            return rejection(json.loads(history[1]["content"]), invalid=True)
        return reply()

    gate.manager._call_model = model
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert len(histories) == 3
    payloads = [json.loads(h[1]["content"]) for h in histories]
    assert payloads[0] == payloads[1]
    assert payloads[2]["targets"] != payloads[0]["targets"]
    run = gate.session.record["last_run"]
    assert run["recovery_attempts"] == 1
    assert run["draft_reviews"][-1]["status"] != "passed"


def test_successful_batches_are_serial_and_release_only_after_last_batch(monkeypatch):
    gate, _, histories = make_gate(monkeypatch)
    active = 0

    async def model(history, **kwargs):
        nonlocal active
        active += 1
        assert active == 1
        assert kwargs["disable_tools"] is True
        histories.append(history)
        assert gate._whole_pending
        await asyncio.sleep(0)
        active -= 1
        return reply()

    gate.manager._call_model = model
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff")) is None
    assert len(histories) >= 3
    assert not gate._whole_pending
    event = gate.session.record["last_run"]["draft_reviews"][-1]
    assert event["status"] == "passed"
    assert all(batch["status"] == "passed" for batch in event["batches"])


@pytest.mark.parametrize("change", ["draft", "sources", "receipts", "unchanged"])
def test_whole_rejection_rechecks_all_targets_after_resubmission(monkeypatch, change):
    gate, index, histories = make_gate(monkeypatch)
    draft = draft_with_references()
    deny = True

    async def model(history, **kwargs):
        histories.append(history)
        payload = json.loads(history[1]["content"])
        if deny and len(histories) == 2:
            return rejection(payload)
        return reply()

    gate.manager._call_model = model
    assert asyncio.run(gate.check(draft, stage="handoff"))
    assert len(histories) == 2
    deny = False
    if change == "draft":
        draft = draft.replace("查证第1个", "核实第1个")
    elif change == "sources":
        monkeypatch.setattr(gate_module, "review_sources", lambda *a, **kw: [
            {"id": "s1", "kind": "read_file", "text": "新增的完整来源"}])
    elif change == "receipts":
        index.facts = [{"count": 3, "simulated": True}]
        index.revision += 1
    assert asyncio.run(gate.check(draft, stage="handoff")) is None
    payloads = [json.loads(h[1]["content"]) for h in histories[2:]]
    expected = claim_targets(draft)
    assert [row for payload in payloads for row in payload["targets"]] == [
        {**{key: target[key] for key in ("id", "field", "start", "end")}, "excerpt": target["text"][:64]}
        for target in expected]
    assert all(json.loads(p["draft"]) == json.loads(draft) for p in payloads)
    assert all(p["action_receipt_summary"] == index.facts for p in payloads)
    if change == "sources":
        assert all(p["sources"][0]["text"] == "新增的完整来源" for p in payloads)


@pytest.mark.parametrize("draft", ["客户明确提出需要核对。", "客户明确提出需要核对版本1.2。"])
def test_small_or_unlocatable_drafts_keep_original_whole_review(monkeypatch, draft):
    gate, _, histories = make_gate(monkeypatch)
    assert asyncio.run(gate.check(draft, stage="handoff")) is None
    assert len(histories) == 1
    assert json.loads(histories[0][1]["content"])["draft"] == draft


def test_whole_batch_keeps_existing_context_budget_fail_closed(monkeypatch):
    gate, _, histories = make_gate(monkeypatch)
    monkeypatch.setattr(gate_module, "history_size", lambda history: 1000000)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert histories == []
    assert gate.session.record["last_run"]["draft_reviews"][-1]["status"] == "context_budget"


def test_item_review_still_projects_only_matching_record(monkeypatch):
    gate, _, histories = make_gate(monkeypatch)
    draft = json.dumps([{"id": "A1", "reason": "客户明确要求立即处理"},
                        {"id": "A2", "reason": "普通记录"}], ensure_ascii=False)
    assert review_units(draft, [{"id": "s1", "kind": "read_file", "text": "原始证据"}]) is not None
    assert asyncio.run(gate.check(draft, stage="handoff")) is None
    assert len(histories) == 1
    assert json.loads(json.loads(histories[0][1]["content"])["draft"]) == [
        {"id": "A1", "reason": "客户明确要求立即处理"}]


@pytest.mark.parametrize("permission", ["readonly", "full_access"])
def test_actual_flow_blocks_downstream_after_later_batch_rejection(runtime_store, permission):
    """真实 Flow 调度与临时文件读取，模型仅为明确标记的离线受控回调。"""
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager

    store, project = runtime_store
    reviews, normal = [], []

    async def run():
        async def model(history, **kwargs):
            if len(history) == 2:
                try:
                    payload = json.loads(history[1]["content"])
                except (TypeError, ValueError):
                    payload = None
                if isinstance(payload, dict) and {"draft", "sources"} <= payload.keys():
                    reviews.append(payload)
                    if len(reviews) == 1:
                        return reply()
                    return reply("need_source", field="items", needed="各条原始记录")
            normal.append(history)
            if len(normal) == 1:
                return assistant(calls=[call("read-original", "read_file", path="source.txt")])
            return assistant(draft_with_references())

        manager = SessionManager(store, model=model)
        service = FlowService(manager)
        try:
            flow = service.create(workspace_path=str(project), permission_policy=permission)
            flow["nodes"] = [
                {"id": "a", "title": "复核上游", "prompt": "读取原文后交接", "tool_names": ["read_file"]},
                {"id": "b", "title": "下游", "prompt": "接收交接", "tool_names": ["read_file"]},
            ]
            flow["edges"] = [{"source": "a", "target": "b"}]
            flow = service.save(flow)
            service.start(flow["id"])
            await service.tasks[flow["id"]]
            result = service.get(flow["id"])["run"]
            sessions = list(manager.sessions.values())
            assert len(sessions) == 1
            assert sessions[0].record["permission_policy"] == permission
            assert result["nodes"]["a"]["status"] != "completed"
            assert result["nodes"]["b"]["status"] == "blocked"
            assert len(reviews) >= 2
            assert all(len(p["targets"]) <= 4 for p in reviews)
        finally:
            await service.shutdown()
            await manager.shutdown()

    asyncio.run(run())


def test_whole_batch_places_complete_targets_and_evidence_before_full_context(monkeypatch):
    from agent_core.core.flow_review_claims import prepare_claim_request

    draft = json.dumps({"items": ["客户明确要求" + ("核对原始记录" * 20) + str(i) + "。" for i in range(5)]}, ensure_ascii=False)
    targets = claim_targets(draft)[:4]
    sources = [{"id": "s1", "kind": "read_file", "scope": "observed_span", "path": "source.txt", "text": "客户明确要求核对原始记录。"}]
    history, passages = prepare_claim_request(draft, sources, targets=targets, node_task="职责", char_limit=100000, token_limit=100000, whole_batch=True)
    payload = json.loads(history[1]["content"])
    assert list(payload)[0] == "targets"
    assert [row["excerpt"] for row in payload["targets"]] == [row["text"] for row in targets]
    assert "claim_texts" not in payload
    assert payload["draft"] == draft and payload["sources"] == sources
    assert passages and list(payload)[1] == "evidence_passages"
    assert payload["evidence_passages"] == [{k: p[k] for k in ("id", "source_id", "text")} for p in passages]


def test_long_batches_preserve_28_ids_and_resolve_exact_indexed_citation():
    from agent_core.core.flow_review_claims import claim_assessment, prepare_claim_request

    draft = json.dumps(["客户明确要求" + "核对原始记录" * 15 + str(i) + "。" for i in range(28)], ensure_ascii=False)
    targets = claim_targets(draft)
    sources = [{"id": "s1", "kind": "read_file", "scope": "observed_span", "text": "客户明确要求核对原始记录。"}]
    assert len(targets) == 28
    shown = []
    for offset in range(0, 28, 4):
        batch = targets[offset:offset + 4]
        history, passages = prepare_claim_request(draft, sources, targets=batch, char_limit=100000,
                                                  token_limit=100000, whole_batch=True)
        payload = json.loads(history[1]["content"])
        shown.extend(row["id"] for row in payload["targets"])
        assert payload["draft"] == draft and payload["sources"] == sources
        verdict = {"status": "revise", "issues": [{"target_id": batch[0]["id"],
                   "evidence_id": passages[0]["id"], "reason": "具体差异"}]}
        feedback, invalid = claim_assessment(verdict, sources, batch, passages)
        assert invalid is None and batch[0]["text"] in feedback and passages[0]["text"] in feedback
    assert shown == [row["id"] for row in targets]


def test_full_focus_over_budget_falls_back_to_complete_legacy_request():
    from agent_core.core.flow_review_claims import prepare_claim_request

    draft = json.dumps(["客户明确要求" + "核对资料" * 100 + str(i) + "。" for i in range(5)], ensure_ascii=False)
    targets = claim_targets(draft)[:4]
    sources = [{"id": "s1", "kind": "read_file", "text": "原始证据"}]
    history, passages = prepare_claim_request(draft, sources, targets=targets, char_limit=0,
                                              token_limit=100000, whole_batch=True)
    payload = json.loads(history[1]["content"])
    assert payload["draft"] == draft and payload["sources"] == sources
    assert [row["id"] for row in payload["targets"]] == [row["id"] for row in targets]
    assert all(len(row["excerpt"]) == 64 for row in payload["targets"])
    assert passages == []


@pytest.mark.parametrize("reject_all_receipts", [False, True])
def test_whole_receipt_budget_omits_optional_index_or_blocks(monkeypatch, reject_all_receipts):
    from agent_core.core import flow_review_claims

    gate, index, histories = make_gate(monkeypatch)
    source = {"id": "s1", "kind": "read_file", "scope": "observed_span",
              "text": "客户明确要求核对原始记录。"}
    monkeypatch.setattr(gate_module, "review_sources", lambda *a, **kw: [source])
    original_size = flow_review_claims.history_size
    rejected_index = []

    def bounded_size(history):
        payload = json.loads(history[1]["content"])
        if "action_receipt_summary" in payload and (reject_all_receipts or "evidence_passages" in payload):
            rejected_index.append("evidence_passages" in payload)
            return 1000000
        return original_size(history)

    monkeypatch.setattr(flow_review_claims, "history_size", bounded_size)
    monkeypatch.setattr(gate_module, "history_size", bounded_size)
    draft = json.dumps(["客户明确要求核对原始记录" + str(i) + "。" for i in range(5)], ensure_ascii=False)
    feedback = asyncio.run(gate.check(draft, stage="handoff"))
    if reject_all_receipts:
        assert feedback and histories == []
        assert gate.session.record["last_run"]["draft_reviews"][-1]["status"] == "context_budget"
    else:
        assert feedback is None and histories
        for history in histories:
            payload = json.loads(history[1]["content"])
            assert json.loads(payload["draft"]) == json.loads(draft) and payload["sources"] == [source]
            assert payload["action_receipt_summary"] == index.facts
            assert "evidence_passages" not in payload
            assert "不能证实客户意愿" in history[0]["content"]
        assert any(rejected_index)

def test_each_batch_requests_all_supported_independent_findings():
    from agent_core.core.flow_review_claims import prepare_claim_request

    draft = draft_with_references()
    targets = claim_targets(draft)[2:6]
    sources = [{"id": "S1", "kind": "read_file", "text": "原始材料支持分别核对事项。"}]
    history, _ = prepare_claim_request(draft, sources, targets=targets, node_task="复核",
                                       char_limit=32000, token_limit=16000, whole_batch=True)
    payload = json.loads(history[1]["content"])
    assert [row["id"] for row in payload["targets"]] == [t["id"] for t in targets]
    assert payload["draft"] == draft and payload["sources"] == sources
    assert "逐项核对本批" in history[0]["content"]
    assert "各自的目标和引文" in history[0]["content"]
    assert "首个" not in history[0]["content"]
