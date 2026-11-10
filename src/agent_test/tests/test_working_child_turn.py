"""Working 的委派详情绑定到实际执行的子会话轮次。"""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_core import config
from agent_core.core.agent_tasks import AgentTasks
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture(autouse=True)
def controlled_model_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY_BACKGROUND", False)
    monkeypatch.setattr(config, "MAX_CONCURRENT_AGENTS", 2)


def _delegate_reply() -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "delegate", "type": "function", "function": {
            "name": "create_session", "arguments": json.dumps({
                "task": "核对委派工单", "title": "工单核对", "tool_names": [],
            }, ensure_ascii=False),
        },
    }]}


def _start_flow(manager: SessionManager, flows: FlowService, workspace: Path) -> tuple[str, str]:
    projects = ProjectService(manager, flows)
    project = projects.create("工单项目", str(workspace), permission_policy="full_access")
    flow_id = project["flow_id"]
    value = flows.get(flow_id)
    value["nodes"] = [{"id": "first", "title": "分派", "prompt": "分派核对任务"}]
    flows.save(value)
    return flow_id, flows.start(flow_id)["run"]["id"]


def test_working_preserves_completed_delegation_during_later_child_turn(
    tmp_path: Path, sandbox_env: Path,
) -> None:
    async def run() -> None:
        parent_waiting, release_parent = asyncio.Event(), asyncio.Event()
        followup_started, release_followup = asyncio.Event(), asyncio.Event()

        async def model(history: list[dict], *, session_id: str, emit: Callable[[str, dict], None]) -> dict:
            if manager.sessions[session_id].record.get("delegated_from"):
                latest_user_message = next(message for message in reversed(history) if message["role"] == "user")
                followup = latest_user_message["content"] == "单独补充问题"
                emit("metrics", {"model": "补充问题模型" if followup else "委派模型"})
                if followup:
                    followup_started.set()
                    await release_followup.wait()
                return {"role": "assistant", "content": "子会话答复"}
            if manager.agent_tasks is None or not manager.agent_tasks.records:
                return _delegate_reply()
            parent_waiting.set()
            await release_parent.wait()
            return {"role": "assistant", "content": "节点完成"}

        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        try:
            _, run_id = _start_flow(manager, flows, sandbox_env)

            await asyncio.wait_for(parent_waiting.wait(), 10)
            task_id, = manager.agent_tasks.records
            await asyncio.wait_for(asyncio.shield(manager.agent_tasks.tasks[task_id]), 10)
            child = manager.sessions[manager.agent_tasks.records[task_id]["child_id"]]
            delegation_turn = child.record["last_run"]["turn_id"]
            node = flows.working_runs()[0]["nodes"][0]
            delegated, = node["subagents"]
            assert (delegated["id"], delegated["status"], delegated["model"], delegated["phase"]) == (
                child.id, "completed", "委派模型", None,
            )

            manager.submit(child, "单独补充问题")
            await asyncio.wait_for(followup_started.wait(), 10)
            assert child.record["flow_run_id"] == run_id
            assert child.record["last_run"]["turn_id"] != delegation_turn
            assert child.record["last_run"]["model_calls"][-1]["model"] == "补充问题模型"
            assert child.phase

            active_run, = flows.working_runs()
            assert active_run["run_id"] == run_id and active_run["status"] == "running"
            node, = active_run["nodes"]
            assert node["subagent_count"] == 1 and node["active_subagent_count"] == 0
            assert node["subagents"] == [delegated]

            release_followup.set()
            await asyncio.wait_for(asyncio.shield(child.task), 10)
            assert flows.working_runs()[0]["nodes"][0]["subagents"] == [delegated]
        finally:
            release_followup.set()
            release_parent.set()
            try:
                await flows.shutdown()
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())


def test_working_shows_delegation_stopping_until_child_drains(
    tmp_path: Path, sandbox_env: Path,
) -> None:
    async def run() -> None:
        parent_waiting, release_parent = asyncio.Event(), asyncio.Event()
        child_started, child_draining, release_child = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def model(history: list[dict], *, session_id: str, emit: Callable[[str, dict], None]) -> dict:
            if manager.sessions[session_id].record.get("delegated_from"):
                emit("metrics", {"model": "委派模型"})
                child_started.set()
                try:
                    await release_child.wait()
                except asyncio.CancelledError:
                    child_draining.set()
                    await release_child.wait()
                    raise
            elif manager.agent_tasks is None or not manager.agent_tasks.records:
                return _delegate_reply()
            else:
                parent_waiting.set()
                await release_parent.wait()
            return {"role": "assistant", "content": "已完成"}

        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        try:
            flow_id, _ = _start_flow(manager, flows, sandbox_env)
            await asyncio.wait_for(asyncio.gather(parent_waiting.wait(), child_started.wait()), 10)
            task_id, = manager.agent_tasks.records
            record = manager.agent_tasks.records[task_id]
            child = manager.sessions[record["child_id"]]
            task = manager.agent_tasks.tasks[task_id]
            node, = flows.working_runs()[0]["nodes"]
            assert node["active_subagent_count"] == 1
            assert node["subagents"][0]["status"] == "running"

            manager.agent_tasks.cancel(record["owner"], task_id)
            await asyncio.wait_for(child_draining.wait(), 10)
            assert child.busy and not task.done()
            assert child.record["status"] == "stopping"
            node, = flows.working_runs()[0]["nodes"]
            delegated, = node["subagents"]
            assert (delegated["id"], delegated["status"], delegated["model"], delegated["phase"]) == (
                child.id, "stopping", "委派模型", child.phase,
            )
            assert delegated["phase"] and node["active_subagent_count"] == 1

            release_child.set()
            await asyncio.wait_for(asyncio.shield(task), 10)
            node, = flows.working_runs()[0]["nodes"]
            delegated, = node["subagents"]
            assert (delegated["status"], delegated["model"], delegated["phase"]) == (
                "cancelled", "委派模型", None,
            )
            assert node["active_subagent_count"] == 0
            release_parent.set()
            await asyncio.wait_for(asyncio.shield(flows.tasks[flow_id]), 10)
            assert flows.working_runs() == []
        finally:
            release_child.set()
            release_parent.set()
            try:
                await flows.shutdown()
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("model_fails", "expected"),
    [
        (False, ("completed", "委派工单答复", "")),
        (True, ("error", "", "RuntimeError: 委派失败")),
    ],
    ids=["completed", "error"],
)
def test_delegation_settles_original_turn_if_followup_submits_at_completion(
    tmp_path: Path, sandbox_env: Path, model_fails: bool, expected: tuple[str, str, str],
) -> None:
    async def run() -> None:
        delegation_started, release_delegation = asyncio.Event(), asyncio.Event()
        followup_started, release_followup = asyncio.Event(), asyncio.Event()

        async def model(history: list[dict], *, session_id: str, emit: Callable[[str, dict], None]) -> dict:
            latest_user_message = next(message for message in reversed(history) if message["role"] == "user")
            if latest_user_message["content"] == "单独补充问题":
                followup_started.set()
                await release_followup.wait()
                return {"role": "assistant", "content": "补充问题答复"}
            delegation_started.set()
            await release_delegation.wait()
            if model_fails:
                raise RuntimeError("委派失败")
            return {"role": "assistant", "content": "委派工单答复"}

        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        queue = manager.agent_tasks = AgentTasks(manager, require_budget_config=False)
        try:
            parent = manager.create(workspace_path=str(sandbox_env))
            task_id = await queue.start(parent, "核对委派工单", [])
            await asyncio.wait_for(delegation_started.wait(), 10)
            child = manager.sessions[queue.status(parent.id, task_id)["child_id"]]
            delegation_turn = child.task

            # 此回调先于委派队列从 shield 恢复，令会话状态在队列结算前进入下一轮。
            delegation_turn.add_done_callback(lambda _: manager.submit(child, "单独补充问题"))
            release_delegation.set()
            await asyncio.wait_for(followup_started.wait(), 10)
            assert child.task is not delegation_turn and child.busy
            await asyncio.wait_for(asyncio.shield(queue.tasks[task_id]), 10)
            record = queue.status(parent.id, task_id)
            assert (record["status"], record["result"], record["error"]) == expected
        finally:
            release_delegation.set()
            release_followup.set()
            try:
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())
