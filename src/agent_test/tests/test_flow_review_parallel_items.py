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
