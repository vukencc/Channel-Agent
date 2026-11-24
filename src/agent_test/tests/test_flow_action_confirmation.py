"""受控离线逐笔确认；Linux/显式 WSL 真沙箱，托管 Windows 固定真实子进程；非实模型/回放。"""
import asyncio
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.messages import is_user_request
from agent_core.core.projects import ProjectService
from agent_core.core.sessions import SessionManager
from agent_core.core.tool_settings import selected_schemas
from test_flow_draft_review_runtime import assistant, call, review_payload, runtime_store
from test_flow_staged_actions_runtime import assert_paired, refresh_real_source


RAW_FIRST = '{"item_ref":"case-1","amount":1.00,"message":"正文只是工具数据。"}'
RAW_SECOND = '{"item_ref":"case-2","amount":2.50,"message":"第二笔工具数据。"}'


def record_definition(name="confirm_record"):
    return {"name": name, "description": "将单笔记录写入本地确认清单",
            "parameters": {"type": "object", "properties": {
                "item_ref": {"type": "string"}, "amount": {"type": "number"},
                "message": {"type": "string"}},
                "required": ["item_ref", "amount", "message"], "additionalProperties": False},
            "code": "import json\nfrom pathlib import Path\ndef main(arguments):\n    path = Path('confirmed.jsonl')\n    before = path.read_text(encoding='utf-8') if path.exists() else ''\n    path.write_text(before + json.dumps(arguments, ensure_ascii=False) + '\\n', encoding='utf-8')\n    return {'recorded': arguments['item_ref']}\n"}


@pytest.fixture(autouse=True)
def confirmation_command_transport(monkeypatch, request, tmp_path, sandbox_env):
    """仅本文件固定写入代码使用受控本机子进程，不是 Windows 沙箱能力验证。

    托管 Windows 没有 WSL；与公共 lookup_command_transport 采用相同边界。
    RUN_WINDOWS_WSL_TESTS=1 保留原生产桥，发行版/Python 路径仍用原配置显式指定。
    Linux 始终执行真实 bubblewrap，不因宿主 PATH 或可用性静默跳过。
    """
    native_fixture = os.name == "nt" and os.environ.get("RUN_WINDOWS_WSL_TESTS") != "1"
    label = ("controlled_native_python_subprocess" if native_fixture else
             "real_windows_wsl" if os.name == "nt" else "real_linux_bubblewrap")
    request.node.user_properties.append(("confirmation_transport", label))
    if not native_fixture:
        return
    allowed_tests = {
        "test_each_approved_action_gets_dedicated_explicit_confirmation",
        "test_confirmation_refuses_stop_unrelated_duplicate_or_changed_authority",
        "test_confirmation_budget_rejects_large_reference_without_truncating_private_data",
        "test_confirmation_retry_survives_existing_single_round_stage_boundary",
    }
    assert request.node.originalname in allowed_tests
    root = Path(sandbox_env).resolve(strict=True)
    assert root.is_relative_to(tmp_path.resolve(strict=True))
    from agent_core.core.command_review import bound_review
    from agent_core.tools import sandbox, wsl_command

    def execute_fixed_confirmation(operation, arguments):
        assert operation == "run_command"
        command = arguments["command"]
        review = bound_review(command, arguments.get("network", False))
        assert (review is not None and review.name == "confirm_record"
                and review.code == record_definition()["code"] and not review.network)
        assert arguments.get("network", False) is False
        assert arguments.get("runtime_profile", "system") == "system"
        assert arguments.get("resource_profile", "standard") == "standard"
        values = json.loads(review.arguments_json)
        assert values in [json.loads(RAW_FIRST), json.loads(RAW_SECOND)]
        assert sandbox.sandbox_root().resolve(strict=True) == root
        assert (root / "confirmed.jsonl").resolve().parent == root
        assert not sandbox.cancellation_requested()
        if not sandbox.ask_permission("run_command", command, arguments.get("reason", ""), command=command):
            return "[已取消] 用户未确认（拒绝或确认超时），命令未执行。"
        # 不执行任意命令：逐字白名单源码及两组已知参数经独立 argv 进入真实进程。
        script = ("import base64,json,sys; ns={}; "
                  "exec(compile(base64.b64decode(sys.argv[1]),'<test-confirmation>','exec'),ns); "
                  "print(json.dumps(ns['main'](json.loads(sys.argv[2])),ensure_ascii=False,allow_nan=False))")
        code = base64.b64encode(review.code.encode("utf-8")).decode("ascii")
        env = {name: os.environ[name] for name in ("SystemRoot", "TEMP", "TMP") if name in os.environ}
        with sandbox.execution_authority():
            sandbox.audit("command_started", strict=True, test_transport=label)
            process = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", script,
                                      code, review.arguments_json], cwd=root, env=env,
                                     stdin=subprocess.DEVNULL, capture_output=True,
                                     encoding="utf-8", timeout=config.COMMAND_TIMEOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            sandbox.audit("command", strict=True, exit_code=process.returncode, test_transport=label)
        # 协议外壳沿用命令工具；退出码、stdout、stderr 均来自真实进程。
        parts = [f"[退出码] {process.returncode}",
                 f"[stdout]\n{sandbox.truncate(process.stdout)}" if process.stdout else "[stdout] （空）"]
        if process.stderr:
            parts.append(f"[stderr]\n{sandbox.truncate(process.stderr)}")
        return "\n".join(parts)

    monkeypatch.setattr(wsl_command, "execute_wsl_tool", execute_fixed_confirmation)


def raw_call(identifier, name, raw):
    result = call(identifier, name)
    result["function"]["arguments"] = raw
    return result


def assert_confirmation(history, schemas, raw):
    assert [row["function"]["name"] for row in schemas] == ["confirm_record"], "确认轮只开放当前这一笔原工具"
    assert schemas[0]["function"]["parameters"] == record_definition()["parameters"]
    # 原始数值拼写及完整参数不经重新序列化；不依赖提示的内部字段名或固定文案。
    references = [row for row in history if row.get("_context_reference")]
    assert history[-1].get("_context_reference") and history[-1]["role"] == "user"
    assert "confirm_record" in history[-1]["content"] and raw in history[-1]["content"], "本次确认输入必须提供原名和精确可复制的原参数"
    assert all(not is_user_request(row) for row in references)


async def confirmation_scenario(runtime_store, monkeypatch, mode="success", structured=False):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    store, workspace = runtime_store
    (workspace / "additional.txt").write_text("合法退审后补读的新来源。", encoding="utf-8")
    errors, requests = [], []
    index = 0
    preview = None
    expected_confirmations = []
    invalid_attempt = mode in {"invalid_preview", "invalid_commit"}
    if mode in {"timeout_once", "timeout"}:
        monkeypatch.setattr(config, "MODEL_RECOVERY_LIMIT", 1)
    if mode == "early_once_single_stage":
        monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 1)
        monkeypatch.setattr(config, "AUTO_CONTINUE_TASKS", True)

    async def model(history, *, session_id, emit):
        nonlocal index, preview
        assert review_payload(history) is None, "本例正文没有争议断言，不应新增语义模型调用"
        index += 1
        schemas = copy.deepcopy(selected_schemas(TOOL_SCHEMAS))
        requests.append((copy.deepcopy(history), schemas))
        names = {row["function"]["name"] for row in schemas}
        session = manager.sessions[session_id]
        assert (manager.history_context is not None) == structured
        if index == 1:
            assert "confirm_record" not in names
            assert "read_file" in names
            matches = [row["function"]["name"] for row in schemas
                       if "confirm_record" in row["function"].get("description", "")]
            assert len(matches) == 1
            preview = matches[0]
            if invalid_attempt:
                attempted = preview if mode == "invalid_preview" else "confirm_record"
                return assistant(calls=[raw_call("invalid-attempt", attempted, '{"amount":1.00}')])
            calls = [raw_call("draft-first", preview, RAW_FIRST)]
            if mode == "batch":
                calls.append(raw_call("draft-second", preview, RAW_SECOND))
            return assistant(calls=calls)
        if invalid_attempt and index == 2:
            assert "confirm_record" not in names and preview in names
            rejected = next(row for row in history if row.get("tool_call_id") == "invalid-attempt")
            assert rejected["_provenance"] == "not_executed"
            return assistant(calls=[
                raw_call("draft-first", preview, RAW_FIRST),
                raw_call("invalid-same-item", preview, '{"item_ref":"case-1","amount":1.00}'),
            ])
        if mode == "revise" and index == 3:
            assert "read_file" in names and preview in names and "confirm_record" not in names
            return assistant(calls=[call("additional-source", "read_file", path="additional.txt")])
        if mode == "revise" and index == 4:
            assert "read_file" in names and preview in names and "confirm_record" not in names
            return assistant(calls=[raw_call("draft-renewed", preview, RAW_FIRST)])
        confirming = ((invalid_attempt and index == 3) or (not invalid_attempt and index == 2)
                      or (mode in {"batch", "timeout_once", "timeout", "early_once_single_stage"} and index == 3)
                      or (mode == "revise" and index == 5))
        if confirming:
            raw = RAW_SECOND if mode == "batch" and index == 3 else RAW_FIRST
            assert_confirmation(history, schemas, raw)
            expected_confirmations.append(raw)
            if mode == "early_once_single_stage" and index == 2:
                return assistant("草稿已经批准，提前交接。")
            if mode == "timeout" or (mode == "timeout_once" and index == 2):
                raise TimeoutError("受控专门确认响应超时")
            if mode == "batch" and index == 2:
                assert RAW_SECOND not in str(history[-1].get("content", "")), "逐笔确认不能把另一笔参数混入当前提案"
            if mode == "revise" and index == 2:
                return assistant('{"decision":"revise","reason":"补读来源后再决定。"}')
            if mode == "stop":
                return assistant('{"decision":"stop","reason":"不办理本笔。"}')
            if mode in {"mixed_stop", "mixed_revise", "mixed_stop_text"}:
                content = ('停止，不执行这笔操作。' if mode == "mixed_stop_text" else
                           json.dumps({"decision": "stop" if mode == "mixed_stop" else "revise"}))
                return assistant(content, calls=[raw_call("mixed-formal", "confirm_record", raw)])
            if mode == "reasoning_stop":
                reply = assistant(calls=[raw_call("formal-" + str(index), "confirm_record", raw)])
                reply["reasoning_content"] = '内部推理讨论 {"decision":"stop"}，不构成 content 的节点决定。'
                return reply
            if mode == "revoke":
                manager.permission_override = "readonly"
            if mode == "source_change":
                # 独立证据刷新发生在确认请求期间；读取与观察元数据均来自真实工具。
                await refresh_real_source(manager, session, "external-source", "additional.txt")
            if mode == "unrelated":
                return assistant(calls=[call("unrelated", "read_file", path="additional.txt")])
            if mode == "duplicate":
                return assistant(calls=[raw_call("formal-first", "confirm_record", raw),
                                        raw_call("formal-duplicate", "confirm_record", raw)])
            if mode == "wrong_arguments":
                return assistant(calls=[raw_call("formal-wrong", "confirm_record", RAW_SECOND)])
            return assistant(calls=[raw_call("formal-" + str(index), "confirm_record", raw)])
        assert "confirm_record" not in names, "成功消费后恢复普通授权目录，不能留下旧批准"
        return assistant("已读取真实工具回执。")

    async def checked_model(history, *, session_id, emit):
        try:
            return await model(history, session_id=session_id, emit=emit)
        except AssertionError as exc:
            errors.append(exc)
            raise

    manager = SessionManager(store, model=checked_model)
    service = FlowService(manager)
    projects = ProjectService(manager, service)
    try:
        project = projects.create("逐笔确认", str(workspace), permission_policy="full_access",
                                  execution_environment="sandbox")
        flow = service.get(project["flow_id"])
        flow["nodes"] = [{"id": "a", "title": "办理", "prompt": "提交本地确认清单并核对回执",
                          "tool_names": ["read_file", "confirm_record", "another_record"],
                          "custom_tools": [record_definition(), record_definition("another_record")]}]
        flow["edges"] = []
        if mode in {"stop", "unrelated", "duplicate", "wrong_arguments", "revoke", "source_change", "timeout", "mixed_stop", "mixed_revise", "mixed_stop_text"}:
            flow["nodes"].append({"id": "downstream", "title": "下游", "prompt": "仅接收正式办理后的交接", "tool_names": []})
            flow["edges"] = [{"source": "a", "target": "downstream"}]
        service.save(flow)
        service.start(flow["id"])
        await service.tasks[flow["id"]]
        if errors:
            raise errors[0]
        session = next(iter(manager.sessions.values()))
        path = workspace / "confirmed.jsonl"
        writes = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        if mode.startswith("mixed_"):
            assert writes == [], "同一 content 已停止或退审时，正确 formal 调用也不能实写"
        assert_paired(session.record)
        assert not session.tool_workers and not manager._active_tools
        recovered = 1 if mode in {"timeout_once", "timeout"} else 0
        total_limit = config.MAX_TOOL_ROUNDS * (1 + config.TASK_MAX_CONTINUATIONS) if mode == "early_once_single_stage" else config.MAX_TOOL_ROUNDS
        assert session.record["last_run"]["model_rounds"] == len(requests) - recovered <= total_limit
        if recovered:
            assert session.record["last_run"]["recovery_attempts"] == 1
            assert requests[1][1] == requests[2][1]
            assert requests[1][0][-1] == requests[2][0][-1], "恢复仍只能确认同一笔原提案"
        return session.record, service.get(flow["id"])["run"], requests, writes, expected_confirmations
    finally:
        await service.shutdown()
        await manager.shutdown()


@pytest.mark.parametrize("structured", [False, True], ids=["legacy", "structured"])
@pytest.mark.parametrize("mode", [
    "success", "batch", "revise", "timeout_once", "reasoning_stop", "invalid_preview", "invalid_commit",
])
def test_each_approved_action_gets_dedicated_explicit_confirmation(runtime_store, monkeypatch, structured, mode):
    record, run, requests, writes, confirmations = asyncio.run(
        confirmation_scenario(runtime_store, monkeypatch, mode, structured))
    assert run["status"] == "completed", run
    expected_writes = [json.loads(RAW_FIRST), json.loads(RAW_SECOND)] if mode == "batch" else [json.loads(RAW_FIRST)]
    assert writes == expected_writes
    assert confirmations == ([RAW_FIRST, RAW_SECOND] if mode == "batch" else [RAW_FIRST, RAW_FIRST] if mode in {"revise", "timeout_once"} else [RAW_FIRST])
    calls = [entry for row in record["messages"] for entry in row.get("tool_calls", [])]
    formal = [entry for entry in calls if entry["function"]["name"] == "confirm_record"]
    formal = [entry for entry in formal if entry["id"] != "invalid-attempt"]
    assert [entry["function"]["arguments"] for entry in formal] == ([RAW_FIRST, RAW_SECOND] if mode == "batch" else [RAW_FIRST])
    receipts = {row["tool_call_id"]: row for row in record["messages"] if row["role"] == "tool"}
    assert all(receipts[entry["id"]]["_provenance"] == "executed" and "[退出码] 0" in receipts[entry["id"]]["content"] for entry in formal)
    if mode == "revise":
        assert receipts["additional-source"]["_provenance"] == "executed"
        assert len([entry for entry in calls if entry["function"]["name"] != "confirm_record" and entry["id"].startswith("draft-")]) == 2
    if mode in {"invalid_preview", "invalid_commit"}:
        for identifier in ("invalid-attempt", "invalid-same-item"):
            assert receipts[identifier]["_provenance"] == "not_executed"
        events = {event["tool_call_id"]: event for event in record["last_run"]["action_preflights"]}
        assert events["invalid-same-item"]["status"] == "invalid_draft"
        assert events["formal-3"]["draft_id"] == events["draft-first"]["draft_id"]


@pytest.mark.parametrize("mode", ["stop", "unrelated", "duplicate", "wrong_arguments", "revoke", "source_change", "timeout", "mixed_stop", "mixed_revise", "mixed_stop_text"])
def test_confirmation_refuses_stop_unrelated_duplicate_or_changed_authority(runtime_store, monkeypatch, mode):
    record, run, requests, writes, confirmations = asyncio.run(
        confirmation_scenario(runtime_store, monkeypatch, mode))
    assert confirmations == ([RAW_FIRST, RAW_FIRST] if mode == "timeout" else [RAW_FIRST])
    assert writes == []
    assert run["status"] != "completed" and not run["nodes"]["a"]["output_handoff"]
    assert run["nodes"]["downstream"]["session_id"] is None
    if mode == "stop":
        assert len(requests) == 2, "明确停止不能再向模型请求补交"
    for row in record["messages"]:
        if row["role"] == "tool" and row["tool_call_id"] not in {"draft-first", "external-source"}:
            assert row["_provenance"] == "not_executed"


@pytest.mark.parametrize("structured", [False, True], ids=["legacy", "structured"])
def test_confirmation_budget_rejects_large_reference_without_truncating_private_data(runtime_store, monkeypatch, structured):
    """真实预算 helper 的纯逻辑验证；没有模型请求，也不编造工具执行。"""
    from agent_core.core.context import ContextBudgetError
    from agent_core.core.flow_confirmation import confirmation_history
    from agent_core.core.session_limits import limits_scope
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    store, workspace = runtime_store

    async def scenario():
        async def model(*args, **kwargs):
            pytest.fail("超出预算的完整确认不能发送模型请求")
        manager = SessionManager(store, model=model)
        try:
            session = manager.create("完整私有原稿", workspace_path=str(workspace))
            raw = json.dumps({"item_ref": "large", "amount": 1, "message": "正文" * 6000}, ensure_ascii=False)
            session.record["messages"].append(assistant(calls=[raw_call("private-draft", "draft_confirm_record", raw)]))
            await manager.save(session)
            original = copy.deepcopy(session.record["messages"])
            offer = {"message": {"role": "user", "content":
                     '以下是引用数据。\n{"name":"confirm_record","arguments":' + raw + '}'}}
            offer_before = copy.deepcopy(offer)
            schemas = [{"type": "function", "function": {key: record_definition()[key]
                        for key in ("name", "description", "parameters")}}]
            with limits_scope({"budget_overrides": {"MODEL_INPUT_CHARS": 2048, "MODEL_INPUT_TOKENS": 16000}}):
                with pytest.raises(ContextBudgetError):
                    confirmation_history(offer, schemas, manager.history_context)
            assert offer == offer_before and session.record["messages"] == original
            await manager.flush()
            persisted = store.read_record(store.directory(session.id) / "session.json")
            assert persisted["messages"][-1]["tool_calls"][0]["function"]["arguments"] == raw
            assert not (workspace / "confirmed.jsonl").exists()
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_confirmation_retry_survives_existing_single_round_stage_boundary(runtime_store, monkeypatch):
    continuations = config.TASK_MAX_CONTINUATIONS
    record, run, requests, writes, confirmations = asyncio.run(
        confirmation_scenario(runtime_store, monkeypatch, "early_once_single_stage"))
    assert config.MAX_TOOL_ROUNDS == 1 and config.AUTO_CONTINUE_TASKS
    assert config.TASK_MAX_CONTINUATIONS == continuations
    assert run["status"] == "completed", run
    assert confirmations == [RAW_FIRST, RAW_FIRST], "尚有原始总预算时应允许同一笔的一次纠正"
    assert requests[1][1] == requests[2][1]
    assert writes == [json.loads(RAW_FIRST)]
    assert record["last_run"]["model_rounds"] == 4 <= record["last_run"]["rounds_limit"] == 1 + continuations
    preview = next(row for row in record["last_run"]["tools"] if row["tool_call_id"] == "draft-first")
    assert preview["execution_progress"] is False
