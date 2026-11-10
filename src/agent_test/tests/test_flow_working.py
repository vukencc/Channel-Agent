"""Working 仅展示本轮 Flow 节点及其实际委派记录。"""

import asyncio
import json

from agent_core import config
from agent_core.core.agent_tasks import AgentTasks
from agent_core.core.flows import FlowService
from agent_core.core.headless import redact_result
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_working_reports_live_children_and_resets_reused_node(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY_BACKGROUND", False)
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", False)
    secret = "working-secret-example"
    prompt = "分析客服问题\n" + "事" * 148 + secret

    async def run():
        child_started, release_child = asyncio.Event(), asyncio.Event()
        next_started, release_next = asyncio.Event(), asyncio.Event()
        reused_started, release_reused = asyncio.Event(), asyncio.Event()
        other_started, release_other = asyncio.Event(), asyncio.Event()
        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        delegated_parents = set()
        waiting_parents = set()

        def tool(name, call_id, arguments):
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }]}

        async def model(history, *, session_id, emit):
            record = manager.sessions[session_id].record
            if record.get("delegated_from"):
                emit("metrics", {"model": "子任务实际模型"})
                child_started.set()
                await release_child.wait()
            elif record["title"] == "另一项目节点":
                other_started.set()
                await release_other.wait()
            elif record["flow_node_id"] == "next":
                next_started.set()
                await release_next.wait()
            elif any("第 2 轮新信息" in (message.get("content") or "") for message in history
                     if message["role"] == "user"):
                reused_started.set()
                await release_reused.wait()
            elif session_id not in delegated_parents:
                delegated_parents.add(session_id)
                emit("metrics", {"model": "已选备用模型"})
                return tool("create_session", "delegate-1", {
                    "task": "检查已有工单记录", "title": "核对资料", "tool_names": [],
                })
            elif session_id not in waiting_parents:
                waiting_parents.add(session_id)
                task_id = next(item["id"] for item in manager.agent_tasks.records.values()
                               if item["owner"] == session_id)
                return tool("wait_agent_task", "wait-1", {"task_id": task_id})
            return {"role": "assistant", "content": "助手回复不会出现在 Working"}

        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)

        def make_project(title, directory, nodes, edges=()):
            path = tmp_path / directory
            path.mkdir()
            project = projects.create(title, str(path), permission_policy="full_access")
            value = flows.get(project["flow_id"])
            value.update(nodes=nodes, edges=list(edges), handoff="此交接不会出现在 Working")
            return flows.save(value)["id"]

        try:
            flow_id = make_project("工单项目", "tickets", [
                {"id": "next", "title": "处理", "prompt": "处理已分类问题"},
                {"id": "first", "title": "分类", "prompt": prompt, "reuse_session": True},
            ], [{"source": "first", "target": "next"}])
            other_id = make_project("独立项目", "other", [
                {"id": "first", "title": "另一项目节点", "prompt": "处理独立项目"},
            ])
            queued = flows.start(flow_id)["run"]
            assert flows.working_runs()[0]["status"] == "queued"
            flows.start(other_id)
            await asyncio.wait_for(asyncio.gather(child_started.wait(), other_started.wait()), 10)

            rows = flows.working_runs(redact=lambda value: redact_result(value, extra_secrets=[secret]))
            assert {row["flow_id"] for row in rows} == {flow_id, other_id}
            row = next(row for row in rows if row["flow_id"] == flow_id)
            assert row["title"] == "工单项目" and row["run_id"] == queued["id"]
            assert [node["id"] for node in row["nodes"]] == ["first", "next"]
            current, pending = row["nodes"]
            assert current["id"] == row["current_node"]
            assert current["model"] == "已选备用模型" and current["model_calls"] == 1
            assert current["started_at"] and pending["started_at"] is None
            assert current["subagent_count"] == current["active_subagent_count"] == 1
            child = current["subagents"][0]
            assert (child["title"], child["model"], child["task_summary"]) == (
                "核对资料", "子任务实际模型", "检查已有工单记录")
            assert next(row for row in rows if row["flow_id"] == other_id)["nodes"][0]["subagents"] == []
            serialized = json.dumps(rows, ensure_ascii=False)
            assert all(text not in serialized for text in (
                secret, secret[:7], "此交接不会出现在 Working", "助手回复不会出现在 Working", str(tmp_path)))

            flows.start(flow_id, input_text="第 2 轮新信息", queue_only=True)
            release_child.set()
            await asyncio.wait_for(next_started.wait(), 10)
            done = next(row for row in flows.working_runs() if row["flow_id"] == flow_id)["nodes"][0]
            assert done["status"] == "completed" and done["active_subagent_count"] == 0
            assert done["subagents"][0]["status"] == "completed"
            release_next.set()
            await asyncio.wait_for(reused_started.wait(), 10)
            second = next(row for row in flows.working_runs() if row["flow_id"] == flow_id)
            assert second["run_id"] != queued["id"]
            assert second["nodes"][0]["session_id"] == current["session_id"]
            assert second["nodes"][0]["subagents"] == []
            assert second["nodes"][0]["model_calls"] == 0
            release_reused.set()
            release_other.set()
            await asyncio.wait_for(asyncio.gather(flows.tasks[flow_id], flows.tasks[other_id]), 10)
            assert flows.working_runs() == []
        finally:
            for event in (release_child, release_next, release_reused, release_other):
                event.set()
            try:
                await flows.shutdown()
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())


def test_default_delegated_title_redacts_before_truncating(tmp_path, monkeypatch):
    secret = "working-secret-example"
    monkeypatch.setattr(config, "API_KEY", secret)
    monkeypatch.setattr(config, "ENABLE_AGENT_TASKS", False)

    async def run():
        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store)
        manager.agent_tasks = AgentTasks(manager, require_budget_config=False)
        try:
            parent = manager.create()
            identifier = await manager.agent_tasks.start(
                parent, "检查已有工单记录并核对编号：" + secret, [], delay_seconds=120)
            child = manager.sessions[manager.agent_tasks.records[identifier]["child_id"]]
            assert child.record["title"] == "子任务 · 检查已有工单记录并核对编号：[已隐藏配置密钥]"
        finally:
            try:
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())


def test_working_reports_permission_wait_of_node_and_child(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", None)
    monkeypatch.setattr(config, "RAG_ASSESS", False)

    async def run():
        root_calls = child_calls = 0
        finish_root = asyncio.Event()

        async def model(history, *, session_id, emit):
            nonlocal root_calls, child_calls
            session = manager.sessions[session_id]
            if session.record.get("delegated_from"):
                child_calls += 1
                if child_calls == 1:
                    name, args = "delete_file", {"path": "留存.txt", "reason": "验证拒绝操作"}
                else:
                    return {"role": "assistant", "content": "已收到拒绝，文件保持原样"}
            else:
                root_calls += 1
                if root_calls == 1:
                    name, args = "create_session", {"task": "验证删除请求", "tool_names": ["delete_file"]}
                else:
                    await finish_root.wait()
                    return {"role": "assistant", "content": "已核对审批结果"}
            return {"role": "assistant", "content": "", "tool_calls": [{
                "id": name, "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }]}

        async def confirmable(*, delegated):
            async with asyncio.timeout(8):
                while True:
                    for session in manager.sessions.values():
                        if bool(session.record.get("delegated_from")) == delegated and session.confirmation:
                            return session
                    await asyncio.sleep(.01)

        store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")
        manager = SessionManager(store, model=model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        workspace = tmp_path / "project"
        workspace.mkdir()
        retained = workspace / "留存.txt"
        retained.write_text("保留", encoding="utf-8")
        try:
            project = projects.create("审批示例", str(workspace), permission_policy="smart")
            value = flows.get(project["flow_id"])
            value["nodes"] = [{"id": "first", "title": "委派", "prompt": "委派后验证审批结果"}]
            flow_id = flows.save(value)["id"]
            flows.start(flow_id)
            parent = await confirmable(delegated=False)
            parent_status = flows.working_runs()[0]["nodes"][0]["status"]
            assert manager.decide(parent, True)
            child = await confirmable(delegated=True)
            node = flows.working_runs()[0]["nodes"][0]
            assert node["active_subagent_count"] == 1
            child_status = node["subagents"][0]["status"]
            assert manager.decide(child, False)
            await asyncio.wait_for(asyncio.shield(child.task), 8)
            finish_root.set()
            await asyncio.wait_for(asyncio.shield(flows.tasks[flow_id]), 8)
            assert retained.read_text(encoding="utf-8") == "保留"
            assert (parent_status, child_status) == ("confirming", "confirming")
        finally:
            finish_root.set()
            try:
                await flows.shutdown()
                await manager.shutdown()
            finally:
                store.close()

    asyncio.run(run())
