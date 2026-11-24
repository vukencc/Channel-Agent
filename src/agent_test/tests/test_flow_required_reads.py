"""受控离线模型回调与真实临时读取；非实模型、非保存回放、非 UI 夹具。"""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import pytest

from agent_core.core.budgets import active_budget
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.tool_settings import selected_schemas
from agent_core.tools import sandbox

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from test_flow_draft_review_runtime import assistant, call, runtime_store
from test_flow_staged_actions_runtime import assert_paired


FROZEN_EQUIVALENT_TASK = "1. 调用 read_feedback 读取入站记录（默认读取全部记录；条目较多时可使用 limit 分批，或通过 ids 定位）。"

TASK = "1. 调用 read_feedback 读取入站记录（条目较多时可使用 limit 分批、ids 定位）"


def read_tool(name="read_feedback"):
    return {"name": name, "description": "只读读取入站记录，不修改文件",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1}},
                "required": ["path"], "additionalProperties": False},
            "code": "def main(arguments):\n    import json\n    from pathlib import Path\n    rows = json.loads(Path(arguments['path']).read_text(encoding='utf-8'))\n    return rows\n"}


def frozen_equivalent_read_tool():
    """合成冻结首岗的开放选择器 schema；不改变其可执行源码。"""
    definition = read_tool()
    definition["parameters"].pop("additionalProperties")
    definition["parameters"]["required"] = []
    definition["parameters"]["properties"]["path"]["default"] = "feedback.json"
    return definition


def no_argument_read_tool():
    """库存无参工具的合成等价物，只读取固定临时输入。"""
    definition = read_tool()
    definition["description"] = "读取当前工作区feedback.json并原样返回完整JSON，不做任何修改或分类。文件缺失或非JSON时抛出异常。"
    definition["parameters"] = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    definition["code"] = definition["code"].replace("Path(arguments['path'])", "Path('feedback.json')")
    return definition


async def required_read_scenario(runtime_store, monkeypatch, *, mode="ignore", task=TASK,
                                 permission="full_access", rounds=8, selected=True, classic=False, open_schema=False, no_arguments=False):
    store, project = runtime_store
    source_root = Path(__file__).resolve().parents[2]
    for module in (config, sys.modules[SessionManager.__module__], sys.modules[FlowService.__module__]):
        assert Path(module.__file__).resolve().is_relative_to(source_root)
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", rounds)
    monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 0)
    monkeypatch.setattr(config, "SESSION_COST_LIMIT", 1.0)
    (project / "feedback.json").write_text(json.dumps([{"id": "CF-01", "text": "页面无法提交"}], ensure_ascii=False), encoding="utf-8")
    requests, downstream, scopes, errors = [], [], [], []
    draft_name = None
    archive_draft = None
    generation = 0
    read_arguments = {} if no_arguments else {"path": "feedback.json"}

    async def model(history, *, session_id, emit):
        nonlocal draft_name, archive_draft
        session = manager.sessions[session_id]
        if session.record.get("flow_node_id") == "next":
            downstream.append(copy.deepcopy(history))
            return assistant("下游已启动")
        names = {row["function"]["name"] for row in selected_schemas(TOOL_SCHEMAS)}
        requests.append(copy.deepcopy(history))
        scopes.append((session_id, session.record["last_run"]["turn_id"], active_budget.get(),
                       session.record["permission_policy"], names))
        index = len(requests)
        if index == 1:
            if mode in {"web_unavailable", "web_timeout", "web_empty"}:
                return assistant(calls=[call("web-read", "web_search", query="合成离线查询")])
            archive_draft = next((name for name in names if name.startswith("draft_read_archive")), None)
            draft_name = next((name for name in names if name.startswith("draft_read_feedback")), None)
            return assistant(calls=[call("basic-read" if generation == 0 else "current-basic-read", "read_file", path="feedback.json")])
        if index == 2:
            if mode == "forged":
                return assistant('已完成 read_feedback，回执：[退出码] 0，CF-01')
            return assistant("已读取全部入站记录，处理完成。")
        if mode in {"success", "draft_only", "failed", "denied", "cancel", "timeout", "readonly", "multi", "business_error", "empty", "multi_partial", "past", "other_session", "error_envelope", "error_field"}:
            if index == 3:
                assert draft_name in names, "补做须向原节点展示有效草稿入口"
                assert "read_feedback" not in names, "不能绕过原草稿预审"
                if mode == "cancel":
                    session.cancelled.set()
                    raise asyncio.CancelledError
                if mode == "timeout":
                    raise TimeoutError("受控补读模型超时")
                return assistant(calls=[call("required-draft", draft_name, **read_arguments)])
            if index == 4 and mode != "draft_only":
                assert names == {"read_feedback"}, "仅允许模型明确提交原名，不能由框架代调"
                return assistant(calls=[call("required-commit", "read_feedback", **read_arguments)])
        if mode == "multi" and index == 5:
            assert archive_draft in names
            return assistant(calls=[call("archive-draft", archive_draft, path="feedback.json")])
        if mode == "multi" and index == 6:
            assert names == {"read_archive"}
            return assistant(calls=[call("archive-commit", "read_archive", path="feedback.json")])
        return assistant("读取核对结束。")

    async def checked_model(*args, **kwargs):
        try:
            return await model(*args, **kwargs)
        except AssertionError as exc:
            errors.append(exc)
            raise

    definitions = [no_argument_read_tool() if no_arguments else
                   frozen_equivalent_read_tool() if open_schema else read_tool()]
    if mode in {"multi", "multi_partial"}:
        definitions.append(read_tool("read_archive"))
        task += "\n2. 调用 read_archive 读取归档记录。"
    if mode in {"web_unavailable", "web_timeout", "web_empty"}:
        import httpx
        from agent_core.tools import web_search as web_module
        monkeypatch.setattr(config, "WEB_SEARCH_API_KEY", "" if mode == "web_unavailable" else "synthetic-offline-token")
        actual_client = httpx.Client

        def offline_request(request):
            if mode == "web_unavailable":
                raise AssertionError("无密钥时不能创建网络请求")
            if mode == "web_empty":
                return httpx.Response(200, json={"results": []}, request=request)
            raise httpx.ReadTimeout("受控离线 HTTP 超时", request=request)

        def offline_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(offline_request)
            return actual_client(*args, **kwargs)

        monkeypatch.setattr(web_module.httpx, "Client", offline_client)
    if mode == "file_empty":
        (project / "feedback.json").write_text("", encoding="utf-8")
    if mode == "file_prefix":
        (project / "feedback.json").write_text("[失败] 页面故障原文，仅为资料而非工具状态", encoding="utf-8")
    install_read_transport(monkeypatch, project, definitions)
    manager = SessionManager(store, model=checked_model,
                             confirmation_handler=lambda prompt, timeout: mode != "denied")
    service = FlowService(manager)
    if mode in {"empty", "business_error", "error_envelope", "error_field"}:
        value = {"empty": [], "business_error": {"ok": False, "error": "反馈接口拒绝读取"},
                 "error_envelope": {"error": "反馈接口拒绝读取"},
                 "error_field": {"id": "CF-01", "error": "页面显示错误"}}[mode]
        (project / "feedback.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    if mode == "failed":
        (project / "feedback.json").write_text("不是JSON", encoding="utf-8")
    try:
        if classic:
            session = manager.create()
            manager.set_workspace_path(session, str(project))
            manager.set_permission_policy(session, permission)
            manager.set_tool_names(session, ["read_file"])
            manager.submit(session, task)
            await asyncio.wait_for(session.task, 5)
            assert_paired(session.record)
            return session.record, None, requests, downstream, scopes
        projects = ProjectService(manager, service)
        project_record = projects.create("点名读取回归", str(project), permission_policy=permission)
        flow = service.get(project_record["flow_id"])
        flow["nodes"] = [
            {"id": "read", "title": "原节点", "prompt": task,
             "tool_names": (["web_search"] if mode in {"web_unavailable", "web_timeout", "web_empty"} else
                            [definition["name"] for definition in definitions]) if selected else [], "custom_tools": definitions,
             "reuse_session": True},
            {"id": "next", "title": "下游", "prompt": "下游核对", "tool_names": []},
        ]
        flow["edges"] = [{"source": "read", "target": "next"}]
        flow = service.save(flow)
        service.start(flow["id"])
        await asyncio.wait_for(service.tasks[flow["id"]], 5)
        run = service.get(flow["id"])["run"]
        if mode in {"past", "other_session"}:
            assert run["status"] == "completed" and downstream
            prior_session = manager.sessions[scopes[0][0]]
            prior_turn = prior_session.record["last_run"]["turn_id"]
            prior_receipt = next(row for row in prior_session.record["messages"] if row.get("tool_call_id") == "required-commit")
            assert prior_receipt["_provenance"] == "executed" and "[退出码] 0" in prior_receipt["content"]
            if mode == "other_session":
                flow = service.get(flow["id"])
                flow["nodes"][0]["reuse_session"] = False
                service.save(flow)
            requests.clear()
            downstream.clear()
            scopes.clear()
            mode = "ignore"
            generation += 1
            service.start(flow["id"])
            await asyncio.wait_for(service.tasks[flow["id"]], 5)
            run = service.get(flow["id"])["run"]
            assert manager.sessions[scopes[0][0]].record["last_run"]["turn_id"] != prior_turn
        record = manager.sessions[scopes[0][0]].record
        if errors:
            raise errors[0]
        assert_paired(record)
        return record, run, requests, downstream, scopes
    finally:
        await service.shutdown()
        await manager.shutdown()


def test_named_read_cannot_be_replaced_by_basic_file_read(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(runtime_store, monkeypatch))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "basic-read")
    assert receipt["_provenance"] == "executed" and "CF-01" in receipt["content"]
    assert record["status"] == "checkpoint", "读到正确文件也不能替代公开第一步点名的 read_feedback 真实回执"
    assert len(requests) == 3, "应在原节点原预算内给一次自主补做机会"
    assert run["nodes"]["next"]["status"] == "blocked" and not downstream
    assert run["nodes"]["read"]["output_handoff"] == ""
    assert "读取" in run["nodes"]["read"]["error"] and "回执" in run["nodes"]["read"]["error"]


@pytest.mark.parametrize("permission", ["full_access", "smart"])
def test_original_node_can_draft_then_explicitly_commit_required_read(runtime_store, monkeypatch, permission):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode="success", permission=permission))
    assert run["status"] == "completed" and len(downstream) == 1
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
    assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
    assert "CF-01" in receipt["content"]
    assert len({scope[0] for scope in scopes}) == len({scope[1] for scope in scopes}) == 1
    assert all(scope[2] is scopes[0][2] and scope[3] == permission for scope in scopes)
    assert scopes[0][2] is not None
    assert record["last_run"]["model_rounds"] == len(requests) <= 8
    assert [event["name"] for event in record["last_run"]["tools"]].count("read_feedback") == 1


@pytest.mark.parametrize("mode,permission", [
    ("draft_only", "full_access"), ("forged", "full_access"), ("failed", "full_access"),
    ("denied", "smart"), ("readonly", "readonly"), ("cancel", "full_access"),
    ("timeout", "full_access"),
])
def test_missing_or_unsuccessful_read_never_releases_downstream(runtime_store, monkeypatch, mode, permission):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, permission=permission))
    assert run["status"] != "completed" and not downstream
    assert run["nodes"]["next"]["status"] == "blocked"
    assert not run["nodes"]["read"]["output_handoff"]
    assert all(scope[3] == permission for scope in scopes)
    commits = [row for row in record["messages"] if row.get("tool_call_id") == "required-commit"]
    if mode in {"draft_only", "forged", "cancel", "timeout"}:
        assert not commits
    if mode in {"failed", "denied", "readonly"}:
        assert commits and all("[退出码] 0" not in row["content"] for row in commits)


@pytest.mark.parametrize("task", [
    "如果入站记录缺失，调用 read_feedback 读取记录。",
    "不要调用 read_feedback 读取入站记录。",
    "可选择调用 read_feedback 或 read_file 读取入站记录。",
    "若需补查：\n  1. 调用 read_feedback 读取入站记录。",
])
def test_conditional_negative_or_optional_reads_do_not_become_mandatory(runtime_store, monkeypatch, task):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, task=task))
    assert run["status"] == "completed" and len(requests) == 2 and downstream
    assert not any(row.get("tool_call_id", "").startswith("required-") for row in record["messages"])


def test_remaining_round_budget_is_not_extended_for_required_read(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, rounds=2))
    assert run["status"] != "completed" and not downstream
    assert record["last_run"]["model_rounds"] == len(requests) == 2


def test_unselected_required_tool_is_not_automatically_authorized(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, selected=False))
    assert run["status"] != "completed" and not downstream
    assert all(not any("read_feedback" in name for name in scope[4]) for scope in scopes)
    assert not any(row.get("tool_call_id", "").startswith("required-") for row in record["messages"])


def test_ordinary_agent_does_not_acquire_flow_read_enforcement(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, classic=True))
    assert run is None and record["status"] == "idle" and len(requests) == 2


def test_exit_zero_explicit_business_error_is_not_successful_read(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode="business_error"))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
    assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
    assert '"ok": false' in receipt["content"]
    assert run["status"] != "completed" and not downstream
    assert run["nodes"]["next"]["status"] == "blocked"


def test_empty_successful_read_counts_as_execution_not_available_source(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode="empty"))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
    assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
    assert "[]" in receipt["content"]
    assert run["status"] == "completed" and downstream


@pytest.fixture(autouse=True)
def read_transport_label(request):
    """明确记录本机受控子进程与真实 Linux/WSL 沙箱的区别。"""
    native = os.name == "nt" and os.environ.get("RUN_WINDOWS_WSL_TESTS") != "1"
    label = "controlled_native_read_subprocess" if native else "real_windows_wsl" if os.name == "nt" else "real_linux_bubblewrap"
    request.node.user_properties.append(("required_read_transport", label))


def install_read_transport(monkeypatch, workspace, definitions):
    """离线 Windows 仅传输本模块固定读取器；不修改沙箱或生产权限。"""
    if os.name != "nt" or os.environ.get("RUN_WINDOWS_WSL_TESTS") == "1":
        return
    import base64
    import subprocess
    from agent_core.core.command_review import bound_review
    from agent_core.tools import wsl_command

    root = Path(workspace).resolve(strict=True)
    expected = {definition["name"]: definition for definition in definitions}
    assert expected and set(expected) <= {"read_feedback", "read_archive"}
    assert all(definition == read_tool(name) or
               (name == "read_feedback" and definition in (frozen_equivalent_read_tool(), no_argument_read_tool()))
               for name, definition in expected.items())

    def execute_read(operation, arguments):
        assert operation == "run_command"
        command = arguments["command"]
        review = bound_review(command, arguments.get("network", False))
        assert review is not None and review.name in expected
        assert review.code == expected[review.name]["code"] and not review.network
        assert arguments.get("runtime_profile", "system") == "system"
        assert arguments.get("resource_profile", "standard") == "standard"
        values = json.loads(review.arguments_json)
        expected_values = {} if expected[review.name] == no_argument_read_tool() else {"path": "feedback.json"}
        assert values == expected_values
        assert sandbox.sandbox_root().resolve(strict=True) == root
        assert (root / "feedback.json").resolve(strict=True).parent == root
        assert not sandbox.cancellation_requested()
        if not sandbox.ask_permission("run_command", command, arguments.get("reason", ""), command=command):
            return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"
        script = ("import base64,json,sys; ns={}; "
                  "exec(compile(base64.b64decode(sys.argv[1]),'<controlled-required-read>','exec'),ns); "
                  "print(json.dumps(ns['main'](json.loads(sys.argv[2])),ensure_ascii=False,allow_nan=False))")
        encoded = base64.b64encode(review.code.encode("utf-8")).decode("ascii")
        env = {name: os.environ[name] for name in ("SystemRoot", "TEMP", "TMP") if name in os.environ}
        with sandbox.execution_authority():
            sandbox.audit("command_started", strict=True, test_transport="controlled_native_read_subprocess")
            process = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script, encoded, review.arguments_json],
                                     cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                     encoding="utf-8", timeout=config.COMMAND_TIMEOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sandbox.audit("command", strict=True, exit_code=process.returncode,
                          test_transport="controlled_native_read_subprocess")
        parts = [f"[退出码] {process.returncode}",
                 f"[stdout]\n{sandbox.truncate(process.stdout)}" if process.stdout else "[stdout] （空）"]
        if process.stderr:
            parts.append(f"[stderr]\n{sandbox.truncate(process.stderr)}")
        return "\n".join(parts)

    monkeypatch.setattr(wsl_command, "execute_wsl_tool", execute_read)


@pytest.mark.parametrize("mode,completed", [("multi", True), ("multi_partial", False)])
def test_every_named_required_read_needs_its_own_current_receipt(runtime_store, monkeypatch, mode, completed):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, rounds=10))
    assert (run["status"] == "completed") is completed
    assert bool(downstream) is completed
    state = record["last_run"]["flow_required_reads"]
    assert state["required"] == ["read_archive", "read_feedback"]
    assert state["completed"] == (["read_archive", "read_feedback"] if completed else ["read_feedback"])
    assert state["missing"] == ([] if completed else ["read_archive"])


def test_write_description_is_not_promoted_to_required_read():
    from agent_core.core.flow_required_reads import required_reads
    definition = read_tool()
    definition["description"] = "读取记录并修改文件写入处理状态"
    assert required_reads(TASK, [definition]) == set()


@pytest.mark.parametrize("task,expected", [
    ("可以调用 read_feedback 读取记录。", set()),
    ("别调用 read_feedback 读取记录。", set()),
    ("示例：\n```text\n1. 调用 read_feedback 读取记录。\n```", set()),
    ("调用 read_feedback 读取记录（仅在客户要求时）。", set()),
    ("若客户要求：\n1. 调用 read_feedback 读取记录。", set()),
    ("调用 read_feedback 或 read_archive 读取记录。", set()),
    ("调用 read_feedback 读取记录，或调用 read_archive 读取记录。", set()),
    ("1. 若客户要求，调用 read_archive 读取记录。\n2. 调用 read_feedback 读取记录。", {"read_feedback"}),
    ("1. 调用 read_feedback。", {"read_feedback"}),
    (TASK, {"read_feedback"}),
])
def test_only_unconditional_named_reads_are_required(task, expected):
    from agent_core.core.flow_required_reads import required_reads
    assert required_reads(task, [read_tool(), read_tool("read_archive")]) == expected


@pytest.mark.parametrize("mode", ["past", "other_session"])
def test_prior_run_and_other_session_receipts_cannot_satisfy_current_read(runtime_store, monkeypatch, mode):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode))
    assert record["status"] == "checkpoint" and not downstream
    assert run["nodes"]["next"]["status"] == "blocked"
    assert record["last_run"]["flow_required_reads"]["completed"] == []
    previous_receipts = [row for row in requests[0] if row.get("tool_call_id") == "required-commit"]
    assert bool(previous_receipts) is (mode == "past")


@pytest.mark.parametrize("task,expected", [
    ("1. 若客户要求：\n   1. 调用 read_feedback 读取记录。", set()),
    ("1. 若客户要求。\n   1. 调用 read_feedback 读取记录。", set()),
    ("调用 read_feedback 读取记录；若客户要求，调用 submit_ticket 提交工单。", {"read_feedback"}),
    ("调用 read_feedback 读取记录。若客户要求，调用 submit_ticket 提交工单。", {"read_feedback"}),
    ("调用 read_feedback 读取错误或警告记录。", {"read_feedback"}),
])
def test_read_conditions_scope_to_their_own_step(task, expected):
    from agent_core.core.flow_required_reads import required_reads
    assert required_reads(task, [read_tool()]) == expected


@pytest.mark.parametrize("mode,completed", [("error_envelope", False), ("error_field", True)])
def test_error_envelope_differs_from_a_record_containing_error(runtime_store, monkeypatch, mode, completed):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
    assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
    assert '"error"' in receipt["content"]
    assert (run["status"] == "completed") is completed
    assert bool(downstream) is completed


def test_read_instruction_after_8192_still_uses_full_legal_node_task(runtime_store, monkeypatch):
    task = "背景资料。\n" * 1500 + TASK
    assert 8192 < len(task) < 16000
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, task=task))
    assert record["status"] == "checkpoint" and not downstream
    assert record["last_run"]["flow_required_reads"]["missing"] == ["read_feedback"]


@pytest.mark.parametrize("mode,prefix", [("web_unavailable", "[不可用]"), ("web_timeout", "[搜索超时]")])
def test_real_web_search_failure_protocol_cannot_satisfy_required_read(runtime_store, monkeypatch, mode, prefix):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, task="1. 调用 web_search 查询合成资料。"))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "web-read")
    assert receipt["_provenance"] == "executed" and receipt["content"].startswith(prefix)
    assert record["status"] == "checkpoint" and not downstream
    assert record["last_run"]["flow_required_reads"]["completed"] == []
    assert record["last_run"]["flow_required_reads"]["missing"] == ["web_search"]


def test_file_body_failure_text_is_data_not_a_tool_failure(runtime_store, monkeypatch):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode="file_prefix", task="1. 调用 read_file 读取 feedback.json。"))
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "basic-read")
    assert receipt["_provenance"] == "executed" and "[失败] 页面故障原文" in receipt["content"]
    assert run["status"] == "completed" and downstream
    assert record["last_run"]["flow_required_reads"]["completed"] == ["read_file"]


def test_builtin_required_read_recognition_accepts_no_custom_definitions():
    from agent_core.core.flow_required_reads import required_reads
    assert required_reads("调用 web_search 查询合成资料。", None) == {"web_search"}


@pytest.mark.parametrize("mode,tool", [("file_empty", "read_file"), ("web_empty", "web_search")])
def test_real_builtin_empty_success_counts_without_claiming_available_source(runtime_store, monkeypatch, mode, tool):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, task=f"1. 调用 {tool} 读取合成资料。"))
    identifier = "basic-read" if tool == "read_file" else "web-read"
    receipt = next(row for row in record["messages"] if row.get("tool_call_id") == identifier)
    assert receipt["_provenance"] == "executed"
    assert receipt["content"] == "", "空文件和空搜索均返回真实空正文，不捏造观察元数据"
    assert run["status"] == "completed" and downstream
    assert record["last_run"]["flow_required_reads"]["completed"] == [tool]


@pytest.mark.parametrize("mode,permission,completed", [
    ("ignore", "full_access", False), ("success", "full_access", True),
    ("success", "smart", True), ("denied", "smart", False),
    ("readonly", "readonly", False), ("draft_only", "full_access", False),
])
def test_frozen_equivalent_open_schema_and_pagination_use_normal_flow_gates(
        runtime_store, monkeypatch, mode, permission, completed):
    definition = frozen_equivalent_read_tool()
    assert "additionalProperties" not in definition["parameters"]
    assert definition["parameters"]["required"] == []
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, task=FROZEN_EQUIVALENT_TASK,
        permission=permission, open_schema=True))
    assert (run["status"] == "completed") is completed
    assert bool(downstream) is completed
    assert record["custom_tools"] == [dict(definition, network=False)], "用途识别不能重写预审或执行参数契约"
    state = record["last_run"]["flow_required_reads"]
    assert state["required"] == ["read_feedback"]
    assert state["completed"] == (["read_feedback"] if completed else [])
    if completed:
        receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
        assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
        assert "CF-01" in receipt["content"]
        drafts = [event for event in record["last_run"]["tools"] if event["name"].startswith("draft_read_feedback")]
        assert len(drafts) == 1, "开放 schema 仍须由模型提出草稿，取得正式提交机会"
        assert scopes[2][-1] == {drafts[0]["name"]}, "补读轮只向模型提供已纳入预算的读取入口"
    else:
        assert run["nodes"]["next"]["status"] == "blocked"
        assert not run["nodes"]["read"]["output_handoff"]
    assert all(scope[3] == permission and scope[2] is scopes[0][2] for scope in scopes)


@pytest.mark.parametrize("task", [
    "仅在需要补充材料时，调用 read_feedback 读取记录。",
    "如果有技术事项：\n1. 调用 read_feedback 读取相关记录。",
    "若客户要求，调用 read_feedback 读取进度。",
    "有必要时可以调用 read_feedback 读取当前记录。",
])
def test_frozen_equivalent_other_conditional_jobs_do_not_gain_required_reads(
        runtime_store, monkeypatch, task):
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, task=task, open_schema=True))
    assert run["status"] == "completed" and downstream and len(requests) == 2
    assert not record["last_run"].get("flow_required_reads")


def test_bundled_dynamic_customer_template_required_read_sets():
    """库存模板与本轮冻结模板不同；只断言无条件点名职责，不执行业务。"""
    from agent_core.core.flow_required_reads import required_reads
    template_path = Path(__file__).resolve().parents[1] / "scripts" / "cases" / "customer_feedback_dynamic.template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    assert template["format"] == "agent-flow" and template["version"] == 1
    observed = {node["id"]: required_reads(node["prompt"], node["custom_tools"])
                for node in template["nodes"]}
    assert observed == {"classify": {"read_feedback"}, "technical": set(), "service": set(),
                        "operations": set(), "human": set()}


@pytest.mark.parametrize("task", [
    "不要修改输入。\n1. 调用 read_file 读取记录。",
    "如果需要可补查历史。\n1. 调用 read_file 读取记录。",
    "调用 read_file 读取记录，必要时分页。",
])
def test_independent_read_is_not_shadowed_by_separate_restrictions(task):
    from agent_core.core.flow_required_reads import required_reads
    assert required_reads(task, []) == {"read_file"}


@pytest.mark.parametrize("mode,permission,completed", [
    ("ignore", "full_access", False), ("success", "full_access", True),
    ("success", "smart", True), ("denied", "smart", False), ("readonly", "readonly", False),
])
def test_no_argument_bundled_equivalent_reader_keeps_normal_flow_gates(
        runtime_store, monkeypatch, mode, permission, completed):
    task = "1. 调用 read_feedback 获取 feedback.json 原始完整对象。"
    record, run, requests, downstream, scopes = asyncio.run(required_read_scenario(
        runtime_store, monkeypatch, mode=mode, task=task, permission=permission, no_arguments=True))
    assert (run["status"] == "completed") is completed and bool(downstream) is completed
    assert record["custom_tools"] == [dict(no_argument_read_tool(), network=False)]
    state = record["last_run"]["flow_required_reads"]
    assert state["required"] == ["read_feedback"]
    assert state["completed"] == (["read_feedback"] if completed else [])
    if completed:
        calls = [call for row in record["messages"] for call in row.get("tool_calls", [])
                 if call["id"] in {"required-draft", "required-commit"}]
        assert len(calls) == 2 and all(json.loads(call["function"]["arguments"]) == {} for call in calls)
        receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "required-commit")
        assert receipt["_provenance"] == "executed" and "[退出码] 0" in receipt["content"]
        assert "CF-01" in receipt["content"]
    else:
        assert run["nodes"]["next"]["status"] == "blocked"
        assert not run["nodes"]["read"]["output_handoff"]
    assert all(scope[3] == permission and scope[2] is scopes[0][2] for scope in scopes)
