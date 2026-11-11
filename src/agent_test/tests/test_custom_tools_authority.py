"""受控离线模型驱动真实工具；验证能力隔离、撤销及排空配对，不是 provider 实测。"""
import asyncio
import copy
import json
import os
import threading

import pytest

from agent_core import config
from agent_core.core.agent_tasks import AgentTasks
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def definition(code=None):
    return {"name": "authority_probe", "description": "真实隔离能力探针", "network": False,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "code": code or "from pathlib import Path\ndef main(arguments):\n    Path('executed').touch()\n    return True\n"}


def call(identifier="authority-call"):
    return {"id": identifier, "type": "function", "function": {
        "name": "authority_probe", "arguments": "{}"}}


@pytest.fixture
def runtime(sandbox_env, tmp_path, monkeypatch):
    for flag in ("RAG_ASSESS", "MEMORY_AUTO_EXTRACT", "ENABLE_AGENT_TASKS",
                 "ENABLE_STRUCTURED_CONTEXT", "AUTO_CONTINUE_TASKS"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "MODEL_TOOL_NAMES", ["read_file", "list_files", "run_command"])
    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    monkeypatch.setattr(config, "COMMAND_EXECUTION_ENVIRONMENT", "sandbox")
    monkeypatch.setattr(config, "COMMAND_NETWORK", "off")
    store = SessionStore(tmp_path / "state", tmp_path / "private")
    yield store, sandbox_env
    store.close()


def root_session(manager, workspace, spec=None):
    session = manager.create("能力回归", "受控离线模型驱动", workspace_path=str(workspace),
                             execution_environment="sandbox")
    session.record.update(flow_id="a" * 32, permission_policy="full_access")
    manager.set_custom_tools(session, [spec or definition()])
    manager.set_tool_names(session, ["authority_probe"])
    return session


@pytest.mark.parametrize("revocation", ["deployment", "parent-tools", "parent-code", "child-code", "missing-parent"])
def test_delegated_capability_revocation_blocks_actual_execution(runtime, monkeypatch, revocation):
    store, workspace = runtime
    async def scenario():
        manager = SessionManager(store)
        try:
            parent = root_session(manager, workspace)
            child = manager.create("子会话", "受控委派", parent=parent)
            child.record.update(delegated_from=parent.id, custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                inherited_custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                tool_names=["authority_probe"], inherited_tool_names=["authority_probe"])
            assert "authority_probe" in manager._effective_session_tools(child)
            if revocation == "deployment":
                monkeypatch.setattr(config, "MODEL_TOOL_NAMES", ["read_file", "list_files"])
            elif revocation == "parent-tools":
                manager.set_tool_names(parent, [])
            elif revocation == "parent-code":
                manager.set_custom_tools(parent, [definition("def main(arguments): return 'replacement'\n")])
            elif revocation == "child-code":
                child.record["custom_tools"][0]["code"] = "def main(arguments): return 'tampered'\n"
            else:
                # 缺少持久化继承证据时，不允许从孤立的定义恢复能力。
                child.record.pop("inherited_custom_tools")
                manager.sessions.pop(parent.id)
            assert "authority_probe" not in (manager._effective_session_tools(child) or [])
            result = await manager._tool(child, call())
            assert "[退出码] 0" not in result, result
            assert not (workspace / "executed").exists()
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


def test_branch_cannot_retain_replaced_parent_implementation(runtime, monkeypatch):
    store, workspace = runtime
    monkeypatch.setattr(config, "ENABLE_SESSION_BRANCHES", True)
    async def scenario():
        manager = SessionManager(store)
        try:
            parent = root_session(manager, workspace)
            branch = await manager.fork_session(parent, through=0)
            assert "authority_probe" in manager._effective_session_tools(branch)
            manager.set_custom_tools(parent, [definition("def main(arguments): return 'new'\n")])
            assert "authority_probe" not in (manager._effective_session_tools(branch) or [])
            result = await manager._tool(branch, call())
            assert "[退出码] 0" not in result, result
            assert not (manager.workspace(branch) / "executed").exists()
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；Windows 桥接另验")
@pytest.mark.parametrize("command_jobs_enabled", [False, True])
@pytest.mark.parametrize("revocation", ["remove", "replace"])
def test_waiting_custom_tool_rechecks_parent_after_workspace_lock(runtime, monkeypatch, command_jobs_enabled, revocation):
    store, workspace = runtime
    monkeypatch.setattr(config, "ENABLE_COMMAND_JOBS", command_jobs_enabled)
    async def scenario():
        manager = SessionManager(store)
        entered, release = threading.Event(), threading.Event()
        holder = None
        pending = None
        try:
            parent = root_session(manager, workspace)
            child = manager.create("等待中的子会话", "真实锁竞争", parent=parent)
            child.record.update(delegated_from=parent.id, custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                inherited_custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                tool_names=["authority_probe"], inherited_tool_names=["authority_probe"])
            guard = manager.command_jobs.guard if command_jobs_enabled else manager.workspaces.guard
            def hold_writer():
                with guard(workspace, threading.Event()):
                    entered.set()
                    assert release.wait(timeout=5), "测试未及时释放 workspace guard"
            holder = threading.Thread(target=hold_writer, daemon=True)
            holder.start()
            assert await asyncio.to_thread(entered.wait, 2)
            pending = asyncio.create_task(manager._tool(child, call()))
            async with asyncio.timeout(2):
                while not any(row["name"] == "authority_probe" and row["state"] == "waiting_workspace"
                              for row in manager._active_tools.values()):
                    if pending.done():
                        pytest.fail(f"工具未等待 workspace guard：{pending.result()}")
                    await asyncio.sleep(.01)
            assert not pending.done()
            replacement = [] if revocation == "remove" else [definition("def main(arguments): return 'new-version'\n")]
            manager.set_custom_tools(parent, replacement)
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert "[退出码] 0" not in result, result
            assert not (workspace / "executed").exists()
            assert not child.tool_workers and not manager._active_tools
        finally:
            release.set()
            if holder is not None:
                await asyncio.to_thread(holder.join, 2)
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；Windows 桥接另验")
@pytest.mark.parametrize("revocation", ["remove", "replace"])
def test_parent_can_revoke_during_actual_smart_confirmation_without_deadlock(runtime, revocation):
    store, workspace = runtime
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        def confirmation_handler(prompt, timeout):
            entered.set()
            return release.wait(timeout=5)
        manager = SessionManager(store, confirmation_handler=confirmation_handler)
        pending = None
        mutation = None
        try:
            parent = root_session(manager, workspace)
            child = manager.create("确认中的子会话", "审批后重新检查权限", parent=parent)
            child.record.update(delegated_from=parent.id, permission_policy="smart",
                                custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                inherited_custom_tools=copy.deepcopy(parent.record["custom_tools"]),
                                tool_names=["authority_probe"], inherited_tool_names=["authority_probe"])
            await manager.flush()
            pending = asyncio.create_task(manager._tool(child, call()))
            assert await asyncio.to_thread(entered.wait, 2), "真实 smart 确认未触发"
            assert not pending.done()
            replacement = [] if revocation == "remove" else [definition("def main(arguments): return 'new-version'\n")]
            # 跨线程带时限更新：持有授权锁等待确认会导致此处失败，且 finally 仍能释放确认。
            mutation = asyncio.create_task(asyncio.to_thread(manager.set_custom_tools, parent, replacement))
            await asyncio.wait_for(asyncio.shield(mutation), 2)
            assert parent.record["custom_tools"] == replacement
            release.set()
            result = await asyncio.wait_for(pending, 2)
            assert "改变" in result and "[退出码] 0" not in result, result
            assert not (workspace / "executed").exists()
            assert not child.tool_workers and not manager._active_tools
        finally:
            release.set()
            if mutation is not None:
                await asyncio.gather(mutation, return_exceptions=True)
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；Windows 桥接另验")
def test_actual_delegation_inherits_only_selected_definition(runtime):
    store, workspace = runtime
    async def controlled_model(history, **kwargs):
        if history[-1]["role"] != "tool":
            return {"role": "assistant", "content": None, "tool_calls": [call()]}
        assert "[退出码] 0" in history[-1]["content"], history[-1]
        return {"role": "assistant", "content": "已执行获准的子工具"}
    async def scenario():
        manager = SessionManager(store, model=controlled_model)
        try:
            parent = root_session(manager, workspace)
            hidden = {**definition("def main(arguments): return 'hidden'\n"), "name": "hidden_probe"}
            manager.set_custom_tools(parent, [definition(), hidden])
            tasks = AgentTasks(manager, require_budget_config=False)
            manager.agent_tasks = tasks
            with pytest.raises(ValueError, match="子集"):
                await tasks.start(parent, "不得提升权限", ["hidden_probe"])
            identifier = await tasks.start(parent, "执行已选择的动态工具", ["authority_probe"])
            await asyncio.wait_for(tasks.tasks[identifier], 8)
            record = tasks.records[identifier]
            assert record["status"] == "completed", record
            child = manager.sessions[record["child_id"]]
            assert [item["name"] for item in child.record["custom_tools"]] == ["authority_probe"]
            assert child.record["custom_tools"] == child.record["inherited_custom_tools"]
            assert (workspace / "executed").exists()
            assert not child.tool_workers and not manager._active_tools
        finally:
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；Windows 桥接另验")
@pytest.mark.parametrize("edit", ["replace", "remove"])
def test_reused_flow_session_replaces_previous_custom_capability(runtime, edit):
    store, workspace = runtime
    async def controlled_model(history, **kwargs):
        last_user = max(index for index, row in enumerate(history) if row["role"] == "user")
        if not any(row["role"] == "tool" for row in history[last_user + 1:]):
            return {"role": "assistant", "content": None, "tool_calls": [call()]}
        return {"role": "assistant", "content": "本轮已核对实际工具结果"}
    code = ("from pathlib import Path\ndef main(arguments):\n"
            "    path = Path('old-count')\n"
            "    path.write_text(str(int(path.read_text()) + 1) if path.exists() else '1')\n"
            "    return 'old'\n")
    async def scenario():
        manager = SessionManager(store, model=controlled_model)
        flows = FlowService(manager)
        projects = ProjectService(manager, flows)
        try:
            flow = flows.create()
            flow["nodes"] = [{"id": "one", "title": "复用节点", "prompt": "运行选定工具",
                               "reuse_session": True, "tool_names": ["authority_probe"],
                               "custom_tools": [definition(code)]}]
            flow = flows.save(flow)
            project = projects.create("复用能力边界", str(workspace), flow_id=flow["id"],
                                      execution_environment="sandbox", permission_policy="full_access")
            flow_id = project["flow_id"]
            flows.start(flow_id)
            await asyncio.wait_for(flows.tasks[flow_id], 8)
            first = flows.get(flow_id)
            assert first["run"]["status"] == "completed", first["run"]
            session_id = first["run"]["nodes"]["one"]["session_id"]
            assert (workspace / "old-count").read_text() == "1"
            first["nodes"][0]["custom_tools"] = [definition()] if edit == "replace" else []
            if edit == "remove":
                first["nodes"][0]["tool_names"] = ["read_file", "list_files"]
            flows.save(first)
            flows.start(flow_id)
            await asyncio.wait_for(flows.tasks[flow_id], 8)
            second = flows.get(flow_id)
            assert second["run"]["status"] == "completed", second["run"]
            assert second["run"]["nodes"]["one"]["session_id"] == session_id
            assert (workspace / "old-count").read_text() == "1"
            assert (workspace / "executed").exists() is (edit == "replace")
            session = manager.sessions[session_id]
            result = [row for row in session.record["messages"] if row["role"] == "tool"][-1]["content"]
            if edit == "remove":
                assert "未知工具" in result or "拒绝" in result, result
                assert manager._session_custom_registry(session) == {}
            else:
                assert "[退出码] 0" in result, result
        finally:
            await flows.shutdown()
            await manager.shutdown()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="真实 Linux sandbox；Windows 桥接另验")
@pytest.mark.parametrize("mode", ["exception", "non-json", "overflow", "timeout", "cancel", "readonly", "confirmation-denied"])
def test_real_tool_terminal_paths_preserve_pairing_and_drain(runtime, monkeypatch, mode):
    store, workspace = runtime
    statements = {
        "exception": "raise RuntimeError('controlled-custom-exception')",
        "non-json": "return {1, 2}",
        "overflow": "print('x' * 100000); return 'large-output-completed'",
        "timeout": "time.sleep(30)",
        "cancel": "time.sleep(30)",
        "readonly": "return True",
        "confirmation-denied": "return True",
    }
    code = ("import time\nfrom pathlib import Path\ndef main(arguments):\n"
            "    Path('started').touch()\n    " + statements[mode] + "\n"
            "    Path('late-side-effect').touch()\n    return True\n")
    if mode == "timeout":
        monkeypatch.setattr(config, "COMMAND_TIMEOUT", .3)
    if mode == "overflow":
        monkeypatch.setattr(config, "TOOL_MAX_OUTPUT", 2048)
    observed = []
    async def controlled_model(history, **kwargs):
        results = [row for row in history if row["role"] == "tool"]
        if not results:
            return {"role": "assistant", "content": None, "tool_calls": [call()]}
        observed.extend(results)
        return {"role": "assistant", "content": "已核对实际工具结果"}
    async def scenario():
        manager = SessionManager(store, model=controlled_model)
        try:
            session = root_session(manager, workspace, definition(code))
            if mode in {"readonly", "confirmation-denied"}:
                session.record["permission_policy"] = "readonly" if mode == "readonly" else "smart"
                monkeypatch.setattr(manager, "_confirm", lambda *args: False)
            manager.submit(session, "执行工具并报告实际结果")
            if mode == "cancel":
                async with asyncio.timeout(8):
                    while not (workspace / "started").exists():
                        if session.task.done():
                            pytest.fail(f"工具未启动：{session.record['status']}")
                        await asyncio.sleep(.02)
                manager.cancel(session)
            await asyncio.wait_for(session.task, 8)
            messages = session.record["messages"]
            calls = [entry for row in messages for entry in row.get("tool_calls", [])]
            results = [row for row in messages if row["role"] == "tool"]
            assert [entry["id"] for entry in calls] == ["authority-call"]
            assert [row["tool_call_id"] for row in results] == ["authority-call"]
            result = results[0]["content"]
            if mode == "exception":
                assert "controlled-custom-exception" in result and "[退出码] 0" not in result
            elif mode == "non-json":
                assert "not JSON serializable" in result and "[退出码] 0" not in result
            elif mode == "overflow":
                assert "截断" in result and len(result) < 3000, result
            elif mode == "timeout":
                assert "超时" in result, result
            elif mode == "cancel":
                # 会话取消在执行中发布保守中断结果，避免承诺未知副作用已回滚。
                assert result.startswith("[中断] 执行结果未知"), result
            elif mode == "confirmation-denied":
                assert "取消" in result, result
            else:
                assert "拒绝" in result or "拦截" in result, result
            if mode in {"readonly", "confirmation-denied"}:
                assert not (workspace / "started").exists()
            if mode != "cancel":
                assert len(observed) == 1
            assert not (workspace / "late-side-effect").exists()
            assert not session.tool_workers and not manager._active_tools
            assert not session.busy
            persisted = store.read_record(store.directory(session.id) / "session.json")
            assert [row["tool_call_id"] for row in persisted["messages"] if row["role"] == "tool"] == ["authority-call"]
        finally:
            await manager.shutdown()
    asyncio.run(scenario())
