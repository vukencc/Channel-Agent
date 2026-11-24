"""使用受控模型回调验证独立事项的滚动复核与共享说明屏障。"""
import asyncio
import json
from collections import Counter

import pytest

from agent_test.tests.test_flow_review_parallel import setup_gate
from agent_test.tests.test_flow_whole_claim_batches import rejection, reply


def draft(include_shared=True, count=4, revised=()):
    data = {"items": [{"id": f"A{i}", "text": f"客户明确{'表示' if i in revised else '要求'}核对第{i}项。"}
                      for i in range(1, count + 1)]}
    if include_shared:
        data["notes"] = "政策要求先取得授权。"
    return json.dumps(data, ensure_ascii=False)


def install(gate, state, action):
    async def model(history, **kwargs):
        assert kwargs["disable_tools"]
        payload = json.loads(history[1]["content"])
        reviewed = json.loads(payload["draft"])
        key = reviewed["items"][0]["id"] if "items" in reviewed else "shared"
        state.setdefault("item_calls", Counter())[key] += 1
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            return await action(key, payload, state["item_calls"][key])
        finally:
            state["active"] -= 1
    gate.manager._call_model = model


def test_independent_items_roll_before_shared_review(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def run():
        first_may_finish = asyncio.Event()
        third_started = asyncio.Event()
        completed = set()

        async def action(key, payload, attempt):
            if key == "A1":
                await first_may_finish.wait()
            if key == "A3":
                third_started.set()
            if key == "shared":
                assert completed == {"A1", "A2", "A3", "A4"}
                assert state["active"] == 1
            assert payload["sources"] == [{"id": "s1", "kind": "read_file", "text": "原始证据"}]
            completed.add(key)
            return reply()

        install(gate, state, action)
        task = asyncio.create_task(gate.check(draft(), stage="handoff"))
        try:
            await asyncio.wait_for(third_started.wait(), 1)
        finally:
            first_may_finish.set()
            feedback = await task
        assert feedback is None

    asyncio.run(run())
    assert state["peak"] == 2 and state["reserved"] == 5
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert [unit["kind"] for unit in event["units"]] == ["item"] * 4 + ["shared_claim"]
    assert event["attempts"] == ["passed"] * 5


def test_inflight_rejection_drains_and_tracks_independent_pending_items(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def run():
        both_started = asyncio.Event()

        async def action(key, payload, attempt):
            if state["active"] == 2:
                both_started.set()
            await both_started.wait()
            return rejection(payload)

        install(gate, state, action)
        assert await asyncio.wait_for(gate.check(draft(), stage="handoff"), 1)

    asyncio.run(run())
    assert gate.pending_items == {"A1", "A2"}
    assert list(state["item_calls"]) == ["A1", "A2"]
    assert state["active"] == 0
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert [unit["status"] for unit in event["units"]] == ["revise", "revise"]


def test_parallel_items_and_shared_notes_use_one_recovery(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        if (key == "A1" and attempt == 1) or key == "shared":
            raise TimeoutError
        return reply()

    install(gate, state, action)
    assert asyncio.run(gate.check(draft(count=2), stage="handoff"))
    run = gate.session.record["last_run"]
    event, = run["draft_reviews"]
    assert [unit["attempts"] for unit in event["units"]] == [
        ["TimeoutError", "passed"], ["passed"], ["TimeoutError"]
    ]
    assert run["recovery_attempts"] == 1 and state["reserved"] == 4


def test_successful_independent_item_is_reused_and_rejected_item_preserves_navigation(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)
    navigated = []

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        if key == "A1":
            if attempt == 1:
                return rejection(payload)
            navigated.append(payload["revision_context"]["source_locations"][0]["quote"])
        return reply()

    install(gate, state, action)

    async def run():
        assert await gate.check(draft(count=3), stage="handoff")
        assert gate.pending_items == {"A1"}
        assert await gate.check(draft(count=3, revised=(1,)), stage="handoff") is None

    asyncio.run(run())
    assert state["item_calls"] == {"A1": 2, "A2": 1, "A3": 1, "shared": 1}
    assert navigated == ["原始证据"]
    event = gate.session.record["last_run"]["draft_reviews"][-1]
    assert [unit["status"] for unit in event["units"]] == ["passed", "reused", "passed", "passed"]
    assert state["reserved"] == 5


def test_binding_change_after_dispatch_bars_shared_and_clears_approvals(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        gate.session.record["permission_policy"] = "readonly"
        return reply()

    install(gate, state, action)
    feedback = asyncio.run(gate.check(draft(), stage="handoff"))
    assert "上下文已改变" in feedback
    assert list(state["item_calls"]) == ["A1", "A2"]
    assert not gate.passed_items and not gate.handoff_feedback_valid
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["status"] == "context_changed" and state["active"] == 0


def test_parallel_cancel_drains_and_records_out_of_order_pass(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def run():
        third_started = asyncio.Event()

        async def action(key, payload, attempt):
            if key == "A2":
                return reply()
            if key == "A3":
                third_started.set()
            await asyncio.Event().wait()

        install(gate, state, action)
        task = asyncio.create_task(gate.check(draft(), stage="handoff"))
        try:
            await asyncio.wait_for(third_started.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert state["active"] == 0
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["status"] == "cancelled"
    assert [unit["status"] for unit in event["units"]] == ["cancelled", "passed", "cancelled"]
    assert event["attempts"] == ["cancelled", "passed", "cancelled"]


def test_serial_configuration_runs_independent_items_in_order(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=1)

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        assert state["active"] == 1
        return reply()

    install(gate, state, action)
    assert asyncio.run(gate.check(draft(count=3), stage="handoff")) is None
    assert list(state["item_calls"]) == ["A1", "A2", "A3", "shared"]
    assert state["peak"] == 1 and state["reserved"] == 4


def long_item_draft(*, revised=False):
    return json.dumps({
        "items": [{"id": f"A{i}", "text": "".join(
            f"客户明确{'表示' if revised and i == 1 and j == 5 else '要求'}核对第{i}项的第{j}个问题。"
            for j in range(1, count + 1))}
            for i, count in [(1, 14), (2, 13)]],
        "notes": "".join(f"政策要求核对第{i}份授权。" for i in range(1, 7)),
    }, ensure_ascii=False)


@pytest.mark.parametrize("concurrency", [1, 2])
def test_many_targets_per_item_keep_full_context_and_slot_bound(monkeypatch, concurrency):
    from agent_core.core.flow_review_claims import claim_targets, focal_targets, shared_targets
    from agent_core.core.flow_review_items import review_units

    gate, index, _, state = setup_gate(monkeypatch, concurrency=concurrency)
    gate.session.flow_review_task = "核对原始反馈与办理依据。"
    sources = [{"id": "s1", "kind": "read_file", "text": "原始证据"}]
    original = long_item_draft()
    units = review_units(original, sources)
    received = {"A1": [], "A2": [], "shared": []}
    by_key = {"A1": units[0], "A2": units[1], "shared": units[2]}
    expected = {key: (shared_targets(unit["draft"]) if key == "shared"
                     else focal_targets(claim_targets(unit["draft"]))) for key, unit in by_key.items()}
    assert [len(expected[key]) for key in received] == [14, 13, 6]

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        assert payload["draft"] == by_key[key]["draft"]
        assert payload["sources"] == by_key[key]["sources"]
        assert payload["workflow_task"] == gate.session.flow_review_task
        if key == "shared":
            assert all(received[item] for item in ("A1", "A2"))
            assert payload["action_receipt_summary"] == index.facts
        else:
            assert "action_receipt_summary" not in payload
        received[key].append(payload["targets"])
        return reply()

    install(gate, state, action)
    assert asyncio.run(gate.check(original, stage="handoff")) is None
    assert state["peak"] == concurrency and state["active"] == 0
    assert state["reserved"] == 10
    assert all(target["location"] == "/context" for batch in received["shared"] for target in batch)
    for key, batches in received.items():
        assert all(1 <= len(batch) <= 4 for batch in batches)
        assert [target["id"] for batch in batches for target in batch] == [target["id"] for target in expected[key]]
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert [unit["batch_count"] for unit in event["units"]] == [4, 4, 2]
    assert [unit["target_count"] for unit in event["units"]] == [14, 13, 6]
    assert all(unit["status"] == "passed" for unit in event["units"])


def test_nested_batches_drain_then_recheck_rejected_and_unfinished_items(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)
    navigated = []

    async def run():
        peer_started = asyncio.Event()
        peer_may_finish = asyncio.Event()

        async def reject_second_batch(key, payload, attempt):
            if key == "A2":
                peer_started.set()
                await peer_may_finish.wait()
                return reply()
            if key == "A1" and attempt == 1:
                await peer_started.wait()
                return reply()
            assert key == "A1" and payload["targets"][0]["id"] == "C5"
            peer_may_finish.set()
            return rejection(payload)

        install(gate, state, reject_second_batch)
        assert await asyncio.wait_for(gate.check(long_item_draft(), stage="handoff"), 1)
        assert state["item_calls"] == {"A1": 2, "A2": 1}
        assert state["active"] == 0 and not gate.passed_items
        assert gate.pending_items == {"A1", "A2"}
        event = gate.session.record["last_run"]["draft_reviews"][-1]
        assert [unit["status"] for unit in event["units"]] == ["revise", "review_stopped"]
        assert [batch["status"] for batch in event["units"][1]["batches"]] == ["passed", "review_stopped"]

        async def pass_revision(key, payload, attempt):
            await asyncio.sleep(0)
            if key == "A1" and payload["targets"][0]["id"] == "C5":
                navigation = payload["revision_context"]["source_locations"]
                navigated.extend(row["quote"] for row in navigation)
            return reply()

        install(gate, state, pass_revision)
        assert await gate.check(long_item_draft(revised=True), stage="handoff") is None

    asyncio.run(run())
    assert state["item_calls"] == {"A1": 6, "A2": 5, "shared": 2}
    assert navigated == ["原始证据"]
    assert state["peak"] == 2 and state["active"] == 0


def test_nested_item_batches_share_one_recovery(monkeypatch):
    from agent_core import config

    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 3)
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def action(key, payload, attempt):
        if (key, attempt) == ("A1", 1):
            raise TimeoutError
        if (key, attempt) == ("A2", 2):
            raise TimeoutError
        await asyncio.sleep(0)
        return reply()

    install(gate, state, action)
    assert asyncio.run(gate.check(long_item_draft(), stage="handoff"))
    run = gate.session.record["last_run"]
    event, = run["draft_reviews"]
    assert run["recovery_attempts"] == 1
    assert not gate.passed_items and "shared" not in state["item_calls"]
    assert state["active"] == 0 and state["peak"] == 2
    assert event["units"][0]["batches"][0]["attempts"] == ["TimeoutError", "passed"]
    assert event["units"][1]["batches"][1]["attempts"] == ["TimeoutError"]


def test_cancelling_nested_item_batches_drains_inflight_without_partial_approval(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def run():
        blocked = set()
        both_waiting = asyncio.Event()

        async def action(key, payload, attempt):
            if attempt == 1:
                return reply()
            blocked.add(key)
            if len(blocked) == 2:
                both_waiting.set()
            await asyncio.Event().wait()

        install(gate, state, action)
        task = asyncio.create_task(gate.check(long_item_draft(), stage="handoff"))
        try:
            await asyncio.wait_for(both_waiting.wait(), 1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert state["item_calls"] == {"A1": 2, "A2": 2}
    assert state["active"] == 0 and state["peak"] == 2 and not gate.passed_items
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert event["status"] == "cancelled"
    assert [unit["attempts"] for unit in event["units"]] == [["passed", "cancelled"], ["passed", "cancelled"]]


def test_queued_nested_batch_stop_preserves_peer_verified_rejection(monkeypatch):
    from agent_core.core.model_settings import check_model_dispatch

    gate, _, _, state = setup_gate(monkeypatch, concurrency=2)

    async def run():
        peer_queued = asyncio.Event()
        rejection_returned = asyncio.Event()

        async def action(key, payload, attempt):
            if attempt == 1:
                await asyncio.sleep(0)
                return reply()
            if key == "A1":
                await peer_queued.wait()
                rejection_returned.set()
                return rejection(payload)
            peer_queued.set()
            await rejection_returned.wait()
            check_model_dispatch()
            raise AssertionError("已停止的请求不能进入供应商")

        install(gate, state, action)
        assert await asyncio.wait_for(gate.check(long_item_draft(), stage="handoff"), 1)
        assert gate.handoff_feedback_valid and not gate.blocked
        assert not gate.passed_items

    asyncio.run(run())
    assert state["active"] == 0 and state["item_calls"] == {"A1": 2, "A2": 2}
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert [unit["status"] for unit in event["units"]] == ["revise", "ModelDispatchStopped"]


def test_nested_item_batches_cannot_exceed_original_round_budget(monkeypatch):
    gate, _, _, state = setup_gate(monkeypatch, concurrency=2, rounds=3)

    async def action(key, payload, attempt):
        await asyncio.sleep(0)
        return reply()

    install(gate, state, action)
    assert asyncio.run(gate.check(long_item_draft(), stage="handoff"))
    assert state["reserved"] == 3 and sum(state["item_calls"].values()) == 3
    assert "shared" not in state["item_calls"] and not gate.passed_items
    assert state["active"] == 0 and state["peak"] == 2
    event, = gate.session.record["last_run"]["draft_reviews"]
    assert "round_budget" in [unit["status"] for unit in event["units"]]
