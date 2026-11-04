"""用离线模型回调验证项目 Flow 的节点会话与交接。"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import ModelConnectionError
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture(autouse=True)
def offline_model_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "RAG_ASSESS", False)


@asynccontextmanager
async def runtime(root: Path, model):
    store = SessionStore(root / "state", workspace_root=root / "private")
    manager = SessionManager(store, model=model)
    service = FlowService(manager)
    projects = ProjectService(manager, service)
    try:
        yield service, projects
    finally:
        try:
            await service.shutdown()
            await manager.shutdown()
        finally:
            store.close()


def make_flow(service: FlowService, flow: dict) -> dict:
    flow["nodes"] = [
        {"id": "first", "title": "分析", "prompt": "提炼输入", "reuse_session": True},
        {"id": "next", "title": "执行", "prompt": "根据交接执行", "reuse_session": True},
        {"id": "last", "title": "验收", "prompt": "验收本轮"},
    ]
    flow["edges"] = [{"source": "first", "target": "next"}, {"source": "next", "target": "last"}]
    return service.save(flow)


async def run_turn(service: FlowService, flow_id: str, input_text: str) -> dict:
    service.start(flow_id, input_text=input_text)
    await asyncio.wait_for(asyncio.shield(service.tasks[flow_id]), 5)
    run = service.get(flow_id)["run"]
    assert run["status"] == "completed", run["error"]
    return run


def test_reuse_survives_restart_with_current_direct_handoff(tmp_path: Path) -> None:
    seen = []

    async def model(history, *, session_id, emit):
        seen.append((session_id, [message["content"] for message in history]))
        return {"role": "assistant", "content": f"节点回复-{len(seen)}-结束"}

    async def run() -> None:
        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            template = make_flow(service, service.create())
            project = projects.create("项目一", str(project_path), template["id"])
            first = await run_turn(service, project["flow_id"], "本轮输入甲")
        async with runtime(tmp_path, model) as (service, projects):
            second = await run_turn(service, project["flow_id"], "本轮输入乙")
            other = projects.create("项目二", str(project_path), template["id"])
            third = await run_turn(service, other["flow_id"], "本轮输入丙")

        for node in ("first", "next"):
            assert first["nodes"][node]["session_id"] == second["nodes"][node]["session_id"]
        assert len({node["session_id"] for node in second["nodes"].values()}) == 3
        assert first["nodes"]["last"]["session_id"] != second["nodes"]["last"]["session_id"]
        assert {node["session_id"] for node in second["nodes"].values()}.isdisjoint(
            node["session_id"] for node in third["nodes"].values()
        )
        assert "本轮输入甲" in seen[0][1][-1]
        assert "节点回复-1-结束" in seen[3][1][:-1]
        assert "节点回复-2-结束" in seen[4][1][:-1]
        assert "本轮输入乙" in seen[3][1][-1]
        assert "本轮输入甲" not in seen[3][1][-1]
        assert "节点回复-4-结束" in seen[4][1][-1]
        assert "节点回复-1-结束" not in seen[4][1][-1]
        assert "节点回复-5-结束" in seen[5][1][-1]
        assert "节点回复-4-结束" not in seen[5][1][-1]
        assert "节点回复-2-结束" not in seen[5][1][-1]
        assert "本轮输入乙" not in "\n".join(seen[6][1])

    asyncio.run(run())


def test_busy_flow_runs_two_queued_inputs_in_order(tmp_path: Path) -> None:
    async def run() -> None:
        first_started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def model(history, *, session_id, emit):
            current_input = history[-1]["content"]
            calls.append((session_id, current_input))
            if len(calls) == 1:
                first_started.set()
                await release.wait()
            return {"role": "assistant", "content": f"排队回复-{len(calls)}-结束"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(project_path))
            flow_id = make_flow(service, service.get(project["flow_id"]))["id"]
            try:
                service.start(flow_id, input_text="忙时输入甲")
                await asyncio.wait_for(first_started.wait(), 5)
                for index, input_text in enumerate(("忙时输入乙", "忙时输入丙"), start=1):
                    snapshot = service.start(flow_id, input_text=input_text)
                    assert len(snapshot["pending_runs"]) == index
                    assert snapshot["run"]["status"] == "running"
                assert len(calls) == 1
            finally:
                release.set()
            async with asyncio.timeout(5):
                while (snapshot := service.get(flow_id))["pending_runs"] or snapshot["run"]["status"] in {
                    "queued", "running", "cancelling"
                }:
                    await asyncio.sleep(0.01)
            assert snapshot["run"]["status"] == "completed", snapshot["run"]["error"]
            assert not service.manager.project_reservations

        assert len(calls) == 9
        for offset, label in zip((0, 3, 6), ("甲", "乙", "丙")):
            assert f"忙时输入{label}" in calls[offset][1]
            assert f"排队回复-{offset + 1}-结束" in calls[offset + 1][1]
            assert f"排队回复-{offset + 2}-结束" in calls[offset + 2][1]
        assert calls[0][0] == calls[3][0] == calls[6][0]
        assert calls[1][0] == calls[4][0] == calls[7][0]

    asyncio.run(run())


@pytest.mark.parametrize("stop", ["failure", "checkpoint", "cancel"])
def test_stopped_reused_turn_does_not_forward_previous_reply(tmp_path: Path, stop: str) -> None:
    async def run() -> None:
        started = asyncio.Event()
        calls = []

        async def model(history, *, session_id, emit):
            calls.append(session_id)
            if len(calls) == 4:
                started.set()
                if stop == "failure":
                    raise RuntimeError("受控模型故障")
                if stop == "checkpoint":
                    raise ModelConnectionError("受控连接故障")
                await asyncio.Event().wait()
            return {"role": "assistant", "content": "上一轮的成功回复"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(project_path))
            flow_id = make_flow(service, service.get(project["flow_id"]))["id"]
            first = await run_turn(service, flow_id, "成功轮输入")
            assert "上一轮的成功回复" in first["nodes"]["first"]["output_handoff"]
            service.start(flow_id, input_text="中止轮输入")
            if stop == "cancel":
                await asyncio.wait_for(started.wait(), 5)
                service.cancel(flow_id)
            await asyncio.wait_for(asyncio.shield(service.tasks[flow_id]), 5)
            stopped = service.get(flow_id)["run"]
            assert stopped["status"] == ("cancelled" if stop == "cancel" else "failed")
            assert stopped["nodes"]["first"]["session_id"] == first["nodes"]["first"]["session_id"]
            assert stopped["nodes"]["first"]["output_handoff"] == ""
            assert stopped["nodes"]["next"]["status"] == "blocked"
            assert stopped["nodes"]["last"]["status"] == "blocked"
            assert not service.manager.project_reservations
            assert len(calls) == 4

    asyncio.run(run())


def test_legacy_flow_keeps_busy_rejection_and_fresh_sessions(tmp_path: Path) -> None:
    async def run() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def model(history, *, session_id, emit):
            started.set()
            await release.wait()
            return {"role": "assistant", "content": "受控回复"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(project_path))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "old", "title": "旧节点", "prompt": "执行旧任务"}]
            flow_id = service.save(flow)["id"]
            try:
                service.start(flow_id)
                await asyncio.wait_for(started.wait(), 5)
                with pytest.raises(ValueError, match="Flow 已运行"):
                    service.start(flow_id)
            finally:
                release.set()
            await asyncio.wait_for(asyncio.shield(service.tasks[flow_id]), 5)
            first = service.get(flow_id)["run"]
            service.start(flow_id)
            await asyncio.wait_for(asyncio.shield(service.tasks[flow_id]), 5)
            second = service.get(flow_id)["run"]
            assert first["status"] == second["status"] == "completed"
            assert first["nodes"]["old"]["session_id"] != second["nodes"]["old"]["session_id"]

    asyncio.run(run())


def test_reused_session_applies_current_project_permissions(tmp_path: Path, sandbox_env: Path) -> None:
    async def run() -> None:
        async def model(history, *, session_id, emit):
            if history[-1]["role"] == "tool" or "权限测试乙" not in history[-1]["content"]:
                return {"role": "assistant", "content": "受控回复"}
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "write-after-permission-change",
                    "type": "function",
                    "function": {
                        "name": "create_file",
                        "arguments": json.dumps({"path": "forbidden.txt", "content": "不应写入"}),
                    },
                }],
            }

        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(sandbox_env), permission_policy="full_access")
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "node", "title": "节点", "prompt": "验证权限", "reuse_session": True}]
            flow_id = service.save(flow)["id"]
            first = await run_turn(service, flow_id, "权限测试甲")
            projects.update(project["id"], permission_policy="readonly")
            second = await run_turn(service, flow_id, "权限测试乙")
            session_id = second["nodes"]["node"]["session_id"]
            assert session_id == first["nodes"]["node"]["session_id"]
            record = service.manager.sessions[session_id].record
            assert record["permission_policy"] == "readonly"
            tool_results = [
                message["content"]
                for message in record["messages"]
                if message["role"] == "tool" and message["tool_call_id"] == "write-after-permission-change"
            ]
            assert len(tool_results) == 1
            assert "readonly，只允许读取" in tool_results[0], tool_results[0]
            assert not (sandbox_env / "forbidden.txt").exists()

    asyncio.run(run())


def test_cancelled_queue_pauses_across_restart_and_stays_out_of_export(tmp_path: Path) -> None:
    async def run() -> None:
        started = asyncio.Event()
        seen = []

        async def model(history, *, session_id, emit):
            seen.append((session_id, history[-1]["content"]))
            if len(seen) == 1:
                started.set()
                await asyncio.Event().wait()
            return {"role": "assistant", "content": "受控回复"}

        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(project_path))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "node", "title": "节点", "prompt": "执行任务", "reuse_session": True}]
            flow_id = service.save(flow)["id"]
            service.start(flow_id, input_text="本轮取消输入")
            await asyncio.wait_for(started.wait(), 5)
            service.start(flow_id, input_text="排队保密输入")
            service.cancel(flow_id)
            await asyncio.wait_for(asyncio.shield(service.tasks[flow_id]), 5)
            stopped = service.get(flow_id)
            assert stopped["run"]["status"] == "cancelled"
            assert len(stopped["pending_runs"]) == 1
            assert len(seen) == 1
            exported = service.export_template(flow_id)
            assert exported["nodes"][0]["reuse_session"] is True
            assert {"project_id", "run", "session_bindings", "pending_runs"}.isdisjoint(exported)
            serialized = json.dumps(exported, ensure_ascii=False)
            assert "排队保密输入" not in serialized
            assert seen[0][0] not in serialized
        async with runtime(tmp_path, model) as (service, _):
            assert flow_id not in service.tasks
            assert len(service.get(flow_id)["pending_runs"]) == 1
            await run_turn(service, flow_id, "")
            assert service.get(flow_id)["pending_runs"] == []
        assert len(seen) == 2
        assert "排队保密输入" in seen[1][1]
        assert "本轮取消输入" not in seen[1][1]

    asyncio.run(run())


def test_reuse_drops_session_moved_away_and_back(tmp_path: Path) -> None:
    seen = []

    async def model(history, *, session_id, emit):
        seen.append(history)
        return {"role": "assistant", "content": "受控回复"}

    async def run() -> None:
        original, other = tmp_path / "original", tmp_path / "other"
        original.mkdir()
        other.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(original))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "node", "title": "节点", "prompt": "执行任务", "reuse_session": True}]
            flow_id = service.save(flow)["id"]
            first = await run_turn(service, flow_id, "原目录首次输入")
            old_session_id = first["nodes"]["node"]["session_id"]
            manager = service.manager
            session = manager.sessions[old_session_id]
            manager.set_workspace_path(session, str(other))
            manager.submit(session, "另一个目录的隔离输入")
            await asyncio.wait_for(asyncio.shield(session.task), 5)
            manager.set_workspace_path(session, str(original))
            second = await run_turn(service, flow_id, "原目录再次输入")
            assert second["nodes"]["node"]["session_id"] != old_session_id
            assert len(seen) == 3
            assert "另一个目录的隔离输入" in seen[1][-1]["content"]
            assert all("另一个目录的隔离输入" not in message["content"] for message in seen[2])

    asyncio.run(run())


def test_queue_only_request_rejects_idle_flow(tmp_path: Path) -> None:
    seen = []

    async def model(history, *, session_id, emit):
        seen.append(session_id)
        return {"role": "assistant", "content": "受控回复"}

    async def run() -> None:
        project_path = tmp_path / "project"
        project_path.mkdir()
        async with runtime(tmp_path, model) as (service, projects):
            project = projects.create("项目", str(project_path))
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "node", "title": "节点", "prompt": "执行任务", "reuse_session": True}]
            flow_id = service.save(flow)["id"]
            await run_turn(service, flow_id, "已经完成的输入")
            old_task = service.tasks[flow_id]
            old_snapshot = service.get(flow_id)
            with pytest.raises(ValueError):
                service.start(flow_id, input_text="迟到的排队输入", queue_only=True)
            assert service.tasks[flow_id] is old_task and old_task.done()
            assert service.get(flow_id) == old_snapshot
            assert not service.manager.project_reservations
            assert len(seen) == 1

    asyncio.run(run())


def test_previous_flow_child_cannot_submit_after_readonly_turn(
    tmp_path: Path, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "ENABLE_AGENT_TASKS", True)
    monkeypatch.setattr(config, "ENABLE_SESSION_BUDGETS", True)
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)

    async def run() -> None:
        def tool_call(name: str, identifier: str, arguments: dict) -> dict:
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": identifier, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }]}

        async def model(history, *, session_id, emit):
            last = history[-1]
            if last.get("tool_call_id") == "create-child":
                task_id = json.loads(last["content"])["task_id"]
                return tool_call("wait_agent_task", "wait-child", {"task_id": task_id})
            if last["role"] == "user" and "父节点委派输入" in last["content"]:
                return tool_call("create_session", "create-child", {"task": "子 Agent 独立任务", "tool_names": []})
            return {"role": "assistant", "content": "受控回复"}

        async with runtime(tmp_path, model) as (service, projects):
            manager = service.manager
            project = projects.create("项目", str(sandbox_env), permission_policy="full_access")
            flow = service.get(project["flow_id"])
            flow["nodes"] = [{"id": "node", "title": "节点", "prompt": "执行任务", "reuse_session": True}]
            flow_id = service.save(flow)["id"]
            first = await run_turn(service, flow_id, "父节点委派输入")
            assert len(manager.agent_tasks.records) == 1
            task = next(iter(manager.agent_tasks.records.values()))
            assert task["status"] == "completed"
            child = manager.sessions[task["child_id"]]
            assert child.record["permission_policy"] == "full_access"
            parent_id = first["nodes"]["node"]["session_id"]
            assert child.record["delegated_from"] == parent_id
            projects.update(project["id"], permission_policy="readonly")
            second = await run_turn(service, flow_id, "父节点只读输入")
            assert second["nodes"]["node"]["session_id"] == parent_id
            assert manager.sessions[parent_id].record["permission_policy"] == "readonly"
            assert not manager.project_reservations

            old_task = child.task
            old_messages = len(child.record["messages"])
            with pytest.raises(ValueError, match="旧轮次"):
                manager.submit(child, "旧子 Agent 再次执行")
            assert child.task is old_task and old_task.done()
            assert len(child.record["messages"]) == old_messages

    asyncio.run(run())
