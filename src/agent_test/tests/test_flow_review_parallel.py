"""受控离线 reviewer 回调；真实门禁与目标核验，无真实模型执行。"""
import asyncio
import json
from collections import Counter

import pytest

from agent_core import config
from agent_core.core.flow_review_claims import claim_targets, focal_targets
from agent_test.tests.test_flow_draft_receipt_review import make_gate
from agent_test.tests.test_flow_draft_review_runtime import assistant, call, runtime_store
from agent_test.tests.test_flow_whole_claim_batches import draft_with_references, rejection, reply


def setup_gate(monkeypatch, concurrency=2, rounds=100):
    if concurrency is not None:
        monkeypatch.setattr(config, "FLOW_REVIEW_CONCURRENCY", concurrency, raising=False)
    gate, index, histories = make_gate(monkeypatch)
    state = {"active": 0, "peak": 0, "reserved": 0, "finished": [], "calls": Counter()}

    def reserve():
        if state["reserved"] >= rounds:
            return False
        state["reserved"] += 1
        return True

    gate.reserve_round = reserve
    return gate, index, histories, state


def install_reviewer(gate, histories, state, action):
    async def model(history, **kwargs):
        assert kwargs["disable_tools"] is True
        assert kwargs["session_id"] == gate.session.id
        payload = json.loads(history[1]["content"])
        key = payload["targets"][0]["id"]
        state["calls"][key] += 1
        histories.append(payload)
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            return await action(payload, state["calls"][key])
        finally:
            state["active"] -= 1
            state["finished"].append(key)
    gate.manager._call_model = model


def audit(gate):
    events = gate.session.record["last_run"]["draft_reviews"]
    assert len(events) == 1
    event = events[0]
    assert [b["batch_index"] for b in event["batches"]] == list(range(1, len(event["batches"]) + 1))
    assert len(event["attempts"]) == sum(len(b.get("attempts", [])) for b in event["batches"])
    return event


@pytest.mark.parametrize("concurrency", [None, 1, 2])
def test_whole_review_respects_bound_and_covers_every_target(monkeypatch, concurrency):
    gate, index, histories, state = setup_gate(monkeypatch, concurrency)
    draft = draft_with_references()
    async def action(payload, attempt):
        assert gate._whole_pending
        await asyncio.sleep(0)
        return reply()
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft, stage="handoff")) is None
    assert state["peak"] == (concurrency or 1)
    assert state["active"] == 0
    assert not gate._whole_pending
    targets = focal_targets(claim_targets(draft))
    assert [t["id"] for p in histories for t in p["targets"]] == [t["id"] for t in targets]
    assert all(json.loads(p["draft"]) == json.loads(draft) and p["action_receipt_summary"] == index.facts for p in histories)
    event = audit(gate)
    assert event["status"] == "passed"
    assert state["reserved"] == len(histories) == len(event["attempts"])


@pytest.mark.parametrize("failure", ["revise", "invalid", "timeout", "need_source", "exception"])
def test_first_failed_wave_drains_without_starting_later_batches(monkeypatch, failure):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    async def action(payload, attempt):
        if payload["targets"][0]["id"] != "C1":
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return reply()
        if failure == "timeout":
            raise TimeoutError("受控离线超时")
        if failure == "exception":
            raise RuntimeError("受控离线异常")
        if failure == "need_source":
            return reply("need_source", field="items", needed="原始记录")
        return rejection(payload, invalid=failure == "invalid")
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert state["active"] == 0
    assert len(histories) <= 2
    assert len(state["finished"]) == len(histories)
    assert gate._whole_pending and not gate.passed_items
    event = audit(gate)
    assert event["status"] != "passed"
    assert event["batches"][0]["status"] != "passed"
    assert state["reserved"] == len(histories) == len(event["attempts"])


@pytest.mark.parametrize("failure", ["timeout", "invalid"])
def test_concurrent_failures_share_only_one_recovery(monkeypatch, failure):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    async def action(payload, attempt):
        await asyncio.sleep(0)
        if attempt == 1:
            if failure == "timeout":
                raise TimeoutError("受控离线超时")
            return rejection(payload, invalid=True)
        return reply()
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert state["peak"] == 2 and state["active"] == 0
    assert len(histories) == 3
    assert sorted(state["calls"].values()) == [1, 2]
    assert gate.session.record["last_run"]["recovery_attempts"] == 1
    event = audit(gate)
    assert state["reserved"] == len(event["attempts"]) == 3
    assert event["status"] != "passed"
    assert sum(b["status"] == "passed" for b in event["batches"]) == 1


@pytest.mark.parametrize("rounds", [0, 1])
def test_exhausted_rounds_never_dispatch_extra_request(monkeypatch, rounds):
    gate, _, histories, state = setup_gate(monkeypatch, rounds=rounds)
    async def action(payload, attempt):
        await asyncio.sleep(0)
        return reply()
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert len(histories) == state["reserved"] == rounds
    assert state["active"] == 0 and gate._whole_pending
    assert audit(gate)["status"] != "passed"


def test_parent_cancellation_drains_inflight_and_preserves_batch_audit(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    async def run():
        both_started = asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                both_started.set()
            await asyncio.Event().wait()
        install_reviewer(gate, histories, state, action)
        task = asyncio.create_task(gate.check(draft_with_references(), stage="handoff"))
        try:
            await asyncio.wait_for(both_started.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert state["active"] == 0
        assert len(histories) == len(state["finished"]) == 2
    asyncio.run(run())
    event = audit(gate)
    assert event["status"] == "cancelled"
    assert all(b["status"] == "cancelled" for b in event["batches"])
    assert gate._whole_pending


def test_out_of_order_completion_keeps_feedback_and_audit_with_original_batch(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    async def run():
        second_done = asyncio.Event()
        async def action(payload, attempt):
            if payload["targets"][0]["id"] == "C1":
                await second_done.wait()
                return rejection(payload)
            second_done.set()
            raise TimeoutError("第二批先结束")
        install_reviewer(gate, histories, state, action)
        feedback = await gate.check(draft_with_references(), stage="handoff")
        assert "原文没有记录该明确要求" in feedback
    asyncio.run(run())
    event = audit(gate)
    assert [b["status"] for b in event["batches"]] == ["revise", "TimeoutError"]
    assert [b["attempts"] for b in event["batches"]] == [["revise"], ["TimeoutError"]]
    assert event["status"] == "revise"
    assert gate.session.phase == ""
    assert state["active"] == 0


@pytest.mark.parametrize("permission", ["readonly", "full_access"])
@pytest.mark.parametrize("approve", [False, True])
def test_real_flow_preserves_selected_permission_and_downstream_gate(runtime_store, monkeypatch, permission, approve):
    """真实临时文件读取及 Flow/SessionManager；仅 reviewer 和主模型驱动为离线回调。"""
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager

    monkeypatch.setattr(config, "FLOW_REVIEW_CONCURRENCY", 2)
    store, project = runtime_store
    normal, reviews = [], []
    active = peak = 0

    async def run():
        nonlocal active, peak
        async def model(history, **kwargs):
            nonlocal active, peak
            payload = None
            if len(history) == 2:
                try:
                    payload = json.loads(history[1]["content"])
                except (ValueError, TypeError):
                    pass
            if isinstance(payload, dict) and {"draft", "sources"} <= payload.keys():
                reviews.append(payload)
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0)
                    if not approve and payload["targets"][0]["id"] != "C1":
                        return reply("need_source", field="items", needed="各条原始记录")
                    return reply()
                finally:
                    active -= 1
            normal.append(kwargs["session_id"])
            if len(normal) == 1:
                return assistant(calls=[call("read-original", "read_file", path="source.txt")])
            if len(normal) == 2 or not approve:
                return assistant(draft_with_references())
            return assistant("处理完成")

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
            assert len(sessions) == (2 if approve else 1)
            assert all(s.record["permission_policy"] == permission for s in sessions)
            assert result["nodes"]["b"]["status"] == ("completed" if approve else "blocked")
            assert result["status"] == "completed" if approve else result["status"] != "completed"
            assert peak == 2 and active == 0
            upstream = sessions[0].record
            event = upstream["last_run"]["draft_reviews"][-1]
            assert event["status"] == ("passed" if approve else "revise")
            assert sum(len(e["attempts"]) for e in upstream["last_run"]["draft_reviews"]) == len(reviews)
            assert upstream["last_run"]["model_rounds"] == len(reviews) + normal.count(sessions[0].id)
            receipt = next(m for m in upstream["messages"] if m.get("tool_call_id") == "read-original")
            assert receipt["_provenance"] == "executed"
            assert {e["name"] for e in upstream["last_run"]["tools"]} == {"read_file"}
        finally:
            await service.shutdown()
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["permission", "receipt_revision", "task", "turn"])
def test_inflight_binding_change_cannot_approve_different_context(monkeypatch, change):
    gate, index, histories, state = setup_gate(monkeypatch)
    async def action(payload, attempt):
        await asyncio.sleep(0)
        if payload["targets"][0]["id"] == "C1":
            if change == "permission":
                gate.session.record["permission_policy"] = "readonly"
            elif change == "receipt_revision":
                index.revision += 1
            elif change == "task":
                gate.session.flow_review_task = "不同的岗位任务"
            else:
                gate.session.record["last_run"]["turn_id"] = "different-turn"
        return reply()
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert state["active"] == 0 and gate._whole_pending
    assert len(histories) == 2
    assert audit(gate)["status"] != "passed"


def test_repeated_parent_cancel_waits_for_reviewer_cleanup(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    async def run():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        install_reviewer(gate, histories, state, action)
        task = asyncio.create_task(gate.check(draft_with_references(), stage="handoff"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert state["active"] == 0
        assert len(histories) == len(state["finished"]) == 2
    asyncio.run(run())
    assert audit(gate)["status"] == "cancelled"


def test_prepare_exception_drains_started_sibling_and_preserves_audit(monkeypatch):
    from agent_core.core import flow_draft_gate as gate_module
    gate, _, histories, state = setup_gate(monkeypatch)
    prepare = gate_module.prepare_claim_request
    def failing_prepare(*args, **kwargs):
        if kwargs["targets"][0]["id"] != "C1":
            raise ValueError("受控准备错误")
        return prepare(*args, **kwargs)
    monkeypatch.setattr(gate_module, "prepare_claim_request", failing_prepare)
    async def action(payload, attempt):
        await asyncio.sleep(0)
        return reply()
    install_reviewer(gate, histories, state, action)
    assert asyncio.run(gate.check(draft_with_references(), stage="handoff"))
    assert state["active"] == 0 and gate._whole_pending
    assert len(histories) <= 1
    event = audit(gate)
    assert event["status"] != "passed"
    assert any(b["status"] == "ValueError" for b in event["batches"])


@pytest.mark.parametrize("failure", ["invalid", "timeout"])
def test_rejected_batch_prevents_sibling_recovery(monkeypatch, failure):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    async def run():
        returned, started = asyncio.Event(), asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                started.set()
            if payload["targets"][0]["id"] == "C1":
                await started.wait()
                returned.set()
                return rejection(payload)
            await returned.wait()
            await asyncio.sleep(0)
            if failure == "timeout":
                raise TimeoutError("受控离线超时")
            return rejection(payload, invalid=True)
        install_reviewer(gate, histories, state, action)
        assert await gate.check(draft_with_references(), stage="handoff")
    asyncio.run(run())
    assert len(histories) == state["reserved"] == 2
    assert state["active"] == 0
    assert len(audit(gate)["attempts"]) == 2


def test_replaced_run_keeps_old_audit_and_never_recovers(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    old_run = gate.session.record["last_run"]
    new_run = {"turn_id": "new", "recovery_attempts": 0}
    async def run():
        started = asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                started.set()
            if payload["targets"][0]["id"] == "C1":
                await started.wait()
                gate.session.record["last_run"] = new_run
                return reply()
            await started.wait()
            await asyncio.sleep(0)
            raise TimeoutError("受控离线超时")
        install_reviewer(gate, histories, state, action)
        assert await gate.check(draft_with_references(), stage="handoff")
    asyncio.run(run())
    assert len(histories) == state["reserved"] == 2
    assert len(old_run["draft_reviews"]) == 1
    assert old_run["draft_reviews"][0]["status"] != "passed"
    assert "draft_reviews" not in new_run
    assert new_run["recovery_attempts"] == 0


def test_replaced_run_does_not_receive_prior_recovery_count(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    old_run = gate.session.record["last_run"]
    new_run = {"turn_id": "new", "recovery_attempts": 0}
    async def run():
        started, recovering = asyncio.Event(), asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                started.set()
            await started.wait()
            if payload["targets"][0]["id"] == "C1":
                if attempt == 1:
                    raise TimeoutError("受控离线超时")
                recovering.set()
                await asyncio.sleep(0)
                return reply()
            await recovering.wait()
            gate.session.record["last_run"] = new_run
            return reply()
        install_reviewer(gate, histories, state, action)
        assert await gate.check(draft_with_references(), stage="handoff")
    asyncio.run(run())
    assert len(histories) == state["reserved"] == 3
    assert old_run["recovery_attempts"] == 1
    assert len(old_run["draft_reviews"]) == 1
    assert "draft_reviews" not in new_run and new_run["recovery_attempts"] == 0


def test_late_model_metrics_do_not_reach_replaced_run(monkeypatch):
    gate, _, _, _ = setup_gate(monkeypatch)
    old_run = gate.session.record["last_run"]
    new_run = {"turn_id": "new"}
    metrics = []
    gate.manager.emit = lambda *args: metrics.append(args)
    async def run():
        started = asyncio.Event()
        calls = 0
        async def model(history, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                started.set()
            await started.wait()
            gate.session.record["last_run"] = new_run
            kwargs["emit"]("metrics", {"input_tokens": 1})
            return reply()
        gate.manager._call_model = model
        assert await gate.check(draft_with_references(), stage="handoff")
    asyncio.run(run())
    assert metrics == [] and gate.session.last_model_event_at == 0
    assert old_run["draft_reviews"][0]["status"] != "passed"
    assert "draft_reviews" not in new_run


def test_queued_dispatch_stopped_is_recorded_without_provider_call(monkeypatch):
    from agent_core.core.model_settings import check_model_dispatch
    gate, _, _, state = setup_gate(monkeypatch)
    async def run():
        queued, rejected = asyncio.Event(), asyncio.Event()
        provider_calls = []
        async def model(history, **kwargs):
            payload = json.loads(history[1]["content"])
            target = payload["targets"][0]["id"]
            if target == "C1":
                await queued.wait()
                provider_calls.append(target)
                rejected.set()
                return rejection(payload)
            queued.set()
            await rejected.wait()
            await asyncio.sleep(0)
            check_model_dispatch()
            provider_calls.append(target)
            return reply()
        gate.manager._call_model = model
        assert await gate.check(draft_with_references(), stage="handoff")
        assert provider_calls == ["C1"]
    asyncio.run(run())
    event = audit(gate)
    assert state["reserved"] == 2
    assert [b["status"] for b in event["batches"]] == ["revise", "ModelDispatchStopped"]
    assert event["batches"][1]["attempts"] == ["ModelDispatchStopped"]


@pytest.mark.parametrize("reject_first", [True, False])
@pytest.mark.parametrize("verdict", ["revise", "need_source"])
@pytest.mark.parametrize("later", ["timeout", "invalid", "dispatch"])
def test_valid_feedback_allows_full_review_of_revised_draft(monkeypatch, reject_first, verdict, later):
    from agent_core.core.model_settings import check_model_dispatch
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    original = draft_with_references()
    revised_data = json.loads(original)
    revised_data["memo"] += "修订。"
    revised = json.dumps(revised_data, ensure_ascii=False)
    async def run():
        both, rejected = asyncio.Event(), asyncio.Event()
        async def action(payload, attempt):
            if json.loads(payload["draft"]) == revised_data:
                await asyncio.sleep(0)
                return reply()
            if state["active"] == 2:
                both.set()
            await both.wait()
            is_rejecting = (payload["targets"][0]["id"] == "C1") == reject_first
            if is_rejecting:
                rejected.set()
                if verdict == "need_source":
                    return reply("need_source", field="items", needed="原始记录")
                return rejection(payload)
            await rejected.wait()
            await asyncio.sleep(0)
            if later == "timeout":
                raise TimeoutError("受控离线超时")
            if later == "dispatch":
                check_model_dispatch()
            return rejection(payload, invalid=True)
        install_reviewer(gate, histories, state, action)
        feedback = await gate.check(original, stage="handoff")
        assert "原文没有记录该明确要求" in feedback if verdict == "revise" else "原始记录" in feedback
        assert not gate.blocked
        assert await gate.check(revised, stage="handoff") is None
    asyncio.run(run())
    new_payloads = [p for p in histories if json.loads(p["draft"]) == revised_data]
    assert [t["id"] for p in new_payloads for t in p["targets"]] == [
        t["id"] for t in claim_targets(revised)]
    assert state["reserved"] == len(histories)


def test_failure_before_valid_feedback_still_blocks_future_submission(monkeypatch):
    gate, _, histories, state = setup_gate(monkeypatch)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    async def run():
        both, failed = asyncio.Event(), asyncio.Event()
        async def action(payload, attempt):
            if state["active"] == 2:
                both.set()
            await both.wait()
            if payload["targets"][0]["id"] == "C1":
                failed.set()
                raise TimeoutError("受控离线超时")
            await failed.wait()
            await asyncio.sleep(0)
            return rejection(payload)
        install_reviewer(gate, histories, state, action)
        assert "原文没有记录该明确要求" in await gate.check(draft_with_references(), stage="handoff")
        assert gate.blocked
        assert await gate.check(draft_with_references(), stage="handoff") == gate.blocked
    asyncio.run(run())
    assert state["reserved"] == len(histories) == 2
