"""受控离线模型的 Flow 交接终稿回归；不代表真实模型运行或性能。"""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from agent_core.core import llm
from agent_core.core.budgets import active_budget
from agent_core.core.flow_handoff import validate_handoff
from agent_core.core.flows import FlowService
from agent_core.core.model_settings import active_endpoint, active_reasoning_effort, active_thinking_mode
from agent_core.core.messages import is_user_request
from agent_core.core.session_limits import limit
from agent_core.core.tool_settings import selected_schemas

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


CONTRACT = {"fields": {"state": {"type": "string", "enum": ["ok"]}}}


def test_prose_json_gets_one_same_turn_format_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    seen = []
    store = SessionStore(tmp_path / "state", workspace_root=tmp_path / "private")

    async def run():
        async def model(history, *, session_id, emit):
            seen.append((session_id, manager.sessions[session_id].record["last_run"]["turn_id"]))
            return {"role": "assistant", "content": '已完成。{"state":"ok"}' if len(seen) == 1 else '{"state":"ok"}'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, "生成交接", handoff_contract=CONTRACT)
        await asyncio.wait_for(session.task, 5)
        assert len(seen) == 2
        assert seen[0] == seen[1]
        assert session.record["status"] == "idle"
        assert session.record["messages"][-1]["content"] == '{"state":"ok"}'
        assert session.record["last_run"]["model_rounds"] == 2
        await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.fixture
def isolated_store(tmp_path, monkeypatch, sandbox_env):
    for name in ("RAG_ASSESS", "MEMORY_AUTO_EXTRACT", "WORKSPACE_MEMORY_ENABLED",
                 "ENABLE_STRUCTURED_CONTEXT", "AUTO_CONTINUE_TASKS"):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, "ENABLE_SESSION_BUDGETS", True)
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 6)
    store = SessionStore(tmp_path / "state", workspace_root=sandbox_env)
    yield store
    store.close()


@pytest.mark.parametrize("answer", [
    '{"state":"ok"}', '```json\n{"state":"ok"}\n```',
    '{"state":"bad"}', '{"state":true}', '{"state":"ok","extra":1}',
    '[]', '', pytest.param(' ' * 65537, id='oversize-whitespace'),
])
def test_only_nonempty_invalid_json_is_repairable(isolated_store, answer):
    seen = []

    async def run():
        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            return {"role": "assistant", "content": answer}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(seen) == 1
            assert session.record["messages"][-1]["content"] == answer
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("contract", [None, CONTRACT])
def test_contract_is_request_local_and_repair_is_single_attempt(isolated_store, contract):
    calls = []

    async def run():
        async def model(history, **kwargs):
            calls.append(1)
            return {"role": "assistant", "content": "仍然不是 JSON"}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, "第一轮", handoff_contract=contract)
            await asyncio.wait_for(session.task, 5)
            assert len(calls) == (2 if contract else 1)
            before = len(calls)
            manager.submit(session, "普通第二轮")
            await asyncio.wait_for(session.task, 5)
            assert len(calls) == before + 1
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("repair,success", [('{"state":"ok"}', True), ("仍然不是 JSON", False),
                                             ('{"state":"bad"}', False)])
def test_flow_repairs_before_forwarding_but_strict_validator_still_blocks(
        isolated_store, tmp_path, repair, success):
    project = tmp_path / "project"
    project.mkdir()
    calls = []

    async def run():
        async def model(history, *, session_id, emit):
            calls.append((session_id, copy.deepcopy(history)))
            return {"role": "assistant", "content": (
                '完成说明。{"state":"ok"}' if len(calls) == 1 else repair if len(calls) == 2 else "下游完成")}

        manager = SessionManager(isolated_store, model=model)
        try:
            service = FlowService(manager)
            flow = service.create(workspace_path=str(project))
            flow["nodes"] = [{"id": "a", "title": "A", "prompt": "上游", "handoff_contract": CONTRACT},
                             {"id": "b", "title": "B", "prompt": "下游"}]
            flow["edges"] = [{"source": "a", "target": "b"}]
            flow = service.save(flow)
            service.start(flow["id"])
            await asyncio.wait_for(service.tasks[flow["id"]], 5)
            result = service.get(flow["id"])["run"]
            assert calls[0][0] == calls[1][0]
            assert result["nodes"]["a"]["handoff_validation"]["status"] == ("passed" if success else "failed")
            if success:
                assert len(calls) == 3
                assert calls[2][0] != calls[1][0]
                assert result["status"] == "completed"
                assert result["nodes"]["a"]["output_handoff"] == '{"state":"ok"}'
                assert '{"state":"ok"}' in calls[2][1][-1]["content"]
            else:
                assert len(calls) == 2
                assert result["status"] == "failed"
                assert result["nodes"]["a"]["output_handoff"] == ""
                assert result["nodes"]["b"]["status"] == "blocked"
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_repair_retains_scopes_and_selected_permissions(isolated_store, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COST_LIMIT", 10)
    scopes = []

    async def run():
        async def model(history, *, session_id, emit):
            session = manager.sessions[session_id]
            scopes.append((session.record["last_run"]["turn_id"], active_endpoint.get(), active_budget.get(),
                           active_reasoning_effort.get(), active_thinking_mode.get(), limit("MAX_TOOL_ROUNDS"),
                           copy.deepcopy(selected_schemas(llm.TOOL_SCHEMAS)), session.record["permission_policy"]))
            return {"role": "assistant", "content": "prose" if len(scopes) == 1 else '{"state":"ok"}'}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            session.record.update(permission_policy="readonly", reasoning_effort="high", thinking_mode="enabled")
            manager.set_limits(session, {"MAX_TOOL_ROUNDS": 3})
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(scopes) == 2
            assert scopes[0][:6] == scopes[1][:6]
            assert scopes[0][2] == (manager.budget_ledger, session.id)
            assert scopes[0][3:6] == ("high", "enabled", 3)
            assert scopes[0][6]
            assert scopes[1][6] == []
            assert scopes[0][7] == scopes[1][7] == "readonly"
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_repair_cannot_exceed_selected_round_limit(isolated_store):
    calls = []

    async def run():
        async def model(history, **kwargs):
            calls.append(1)
            return {"role": "assistant", "content": "not JSON"}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.set_limits(session, {"MAX_TOOL_ROUNDS": 1})
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert calls == [1]
            assert session.record["last_run"]["model_rounds"] == 1
            assert "handoff_finalization" not in session.record["last_run"]
            assert validate_handoff(session.record["messages"][-1]["content"], CONTRACT)[1] is not None
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_busy_and_cancel_during_repair_preserve_one_turn(isolated_store):
    async def run():
        entered = asyncio.Event()
        calls = []

        async def model(history, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                entered.set()
                await asyncio.Event().wait()
            return {"role": "assistant", "content": "not JSON"}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, "唯一用户输入", handoff_contract=CONTRACT)
            await asyncio.wait_for(entered.wait(), 5)
            turn = session.record["last_run"]["turn_id"]
            assert session.busy
            with pytest.raises(ValueError, match="正在运行"):
                manager.submit(session, "不应入队", handoff_contract=CONTRACT)
            manager.cancel(session)
            await asyncio.wait_for(session.task, 5)
            assert not session.busy
            assert session.record["status"] == "cancelled"
            assert session.record["last_run"]["turn_id"] == turn
            assert [m["content"] for m in session.record["messages"] if is_user_request(m)] == ["唯一用户输入"]
            assert len(calls) == 2
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_repair_timeout_is_not_a_second_repair(isolated_store):
    calls = []

    async def run():
        async def model(history, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise TimeoutError("受控离线服务超时")
            return {"role": "assistant", "content": "not JSON"}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.set_limits(session, {"MODEL_RECOVERY_LIMIT": 0})
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(calls) == 2
            assert session.record["status"] == "error"
            assert "TimeoutError" in session.record["error"]
            assert session.record["last_run"]["recovery_attempts"] == 0
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_provider_request_has_no_repair_tools_and_unexpected_calls_never_execute(
        isolated_store, monkeypatch, sandbox_env):
    """真实请求装配与流解析，传输层为离线 double；不访问 provider。"""
    requests = []
    executed = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            requests.append(copy.deepcopy(kwargs))

            async def stream():
                tool_calls = None
                content = "完成说明。{}"
                finish = "stop"
                if len(requests) == 2:
                    content = None
                    finish = "tool_calls"
                    tool_calls = [SimpleNamespace(index=0, id="repair-write", type="function", function=SimpleNamespace(
                        name="write_file", arguments='{"path":"forbidden.txt","content":"must not exist"}'))]
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish,
                    delta=SimpleNamespace(content=content, reasoning_content=None, tool_calls=tool_calls))])

            return stream()

    monkeypatch.setattr(llm, "endpoint_client", lambda endpoint: Client())
    monkeypatch.setattr(llm, "model_options", lambda: {})
    monkeypatch.setattr(llm, "selected_model", lambda: "controlled-offline-model")

    async def run():
        manager = SessionManager(isolated_store, model=llm.call_model)
        async def forbidden_execution(*args, **kwargs):
            executed.append(1)
            raise AssertionError("格式修正不得执行工具")
        monkeypatch.setattr(manager, "_tool", forbidden_execution)
        try:
            session = manager.create()
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(requests) == 2
            assert requests[0]["tools"]
            assert "tools" not in requests[1]
            assert "tool_choice" not in requests[1]
            assert requests[0]["model"] == requests[1]["model"]
            assert requests[0]["extra_headers"] == requests[1]["extra_headers"]
            assert executed == []
            assert not (sandbox_env / "forbidden.txt").exists()
            assert session.record["status"] == "error"
            messages = session.record["messages"]
            calls = [call["id"] for message in messages for call in message.get("tool_calls", [])]
            results = [message for message in messages if message["role"] == "tool"]
            assert calls == ["repair-write"]
            assert [message["tool_call_id"] for message in results] == calls
            assert all("未执行" in message["content"] for message in results)
            assert session.record["last_run"]["tools"] == []
        finally:
            await manager.shutdown()

    asyncio.run(run())



def test_repair_context_limit_stops_before_second_provider_call(isolated_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_INPUT_CHARS", 60000)
    calls = []

    async def run():
        async def model(history, **kwargs):
            calls.append(1)
            return {"role": "assistant", "content": "非JSON说明" * 9000}

        manager = SessionManager(isolated_store, model=model)
        try:
            session = manager.create()
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert calls == [1]
            assert session.record["status"] == "checkpoint"
            assert session.record["last_run"]["stop_reason"] == "context_budget"
            assert session.record["last_run"]["model_rounds"] == 1
            assert session.record["last_run"]["handoff_finalization"]["status"] == "context_budget"
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_failed_repair_instruction_save_prevents_second_model_call(isolated_store, monkeypatch):
    calls = []

    async def run():
        async def model(history, **kwargs):
            calls.append(1)
            return {"role": "assistant", "content": "非JSON说明"}

        manager = SessionManager(isolated_store, model=model)
        original_save = manager.save
        failed = []

        def save(session):
            if (not failed and session.record["messages"][-1].get("_context_reference")):
                failed.append(1)
                future = asyncio.get_running_loop().create_future()
                future.set_exception(OSError("受控持久化失败"))
                return future
            return original_save(session)

        monkeypatch.setattr(manager, "save", save)
        try:
            session = manager.create()
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert failed == [1]
            assert calls == [1]
            assert session.record["status"] == "error"
            assert "OSError" in session.record["error"]
            await manager.flush()
            saved = isolated_store.read_record(isolated_store.directory(session.id) / "session.json")
            assert saved["status"] == "error"
            assert saved["last_run"]["turn_id"] == session.record["last_run"]["turn_id"]
        finally:
            await manager.shutdown()

    asyncio.run(run())



def test_repair_uses_existing_request_rate_budget(isolated_store, monkeypatch):
    monkeypatch.setattr(config, "MODEL_REQUESTS_PER_MINUTE", 1)
    sent = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            sent.append(kwargs)

            async def stream():
                yield SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason="stop",
                    delta=SimpleNamespace(content="完成说明。{}", reasoning_content=None, tool_calls=None))])

            return stream()

    monkeypatch.setattr(llm, "endpoint_client", lambda endpoint: Client())
    monkeypatch.setattr(llm, "model_options", lambda: {})
    monkeypatch.setattr(llm, "selected_model", lambda: "controlled-offline-model")

    async def run():
        manager = SessionManager(isolated_store, model=llm.call_model)
        try:
            session = manager.create()
            manager.submit(session, "交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(sent) == 1
            assert session.record["status"] == "checkpoint"
            assert session.record["last_run"]["stop_reason"] == "cost_or_rate_budget"
            assert len(manager.budget_ledger.state["requests"]) == 1
            assert [m["content"] for m in session.record["messages"] if is_user_request(m)] == ["交接"]
        finally:
            await manager.shutdown()

    asyncio.run(run())


def _finalization_observer():
    """只收集元数据事件，其他接口沿用空观察器。"""
    from agent_core.core.instrumentation import NullObserver

    class Observer(NullObserver):
        enabled = True

        def __init__(self):
            self.events = []

        def event(self, kind, *, session_id=None, **fields):
            self.events.append((kind, session_id, fields))

    return Observer()


def _finalization_flow(manager, project):
    service = FlowService(manager)
    flow = service.create(workspace_path=str(project))
    flow["nodes"] = [{"id": "a", "title": "A", "prompt": "上游", "handoff_contract": CONTRACT},
                     {"id": "b", "title": "B", "prompt": "下游"}]
    flow["edges"] = [{"source": "a", "target": "b"}]
    return service, service.save(flow)


@pytest.mark.parametrize("repair,valid", [("仍然不是 JSON", False), ('{"state":"ok"}', True)])
def test_finalization_publishes_only_valid_repair_to_final_and_memory(
        isolated_store, tmp_path, monkeypatch, repair, valid):
    """真实共享记忆写入与离线摘要 double；无真实模型或录制回放。"""
    import json

    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", True)
    monkeypatch.setattr(config, "WORKSPACE_MEMORY_ENABLED", True)
    project = tmp_path / "publication-project"
    project.mkdir()
    calls, candidates, summaries = [], [], []
    observer = _finalization_observer()

    async def run():
        async def model(history, **kwargs):
            calls.append(copy.deepcopy(history))
            return {"role": "assistant", "content": "说明文字。{}" if len(calls) == 1 else repair}

        manager = SessionManager(isolated_store, model=model, observer=observer,
                                 permission_override="full_access")

        async def summary(prompt, *, session_id):
            summaries.append(prompt)
            return json.dumps({"project": ["受控项目事实"], "progress": "本轮自报结果", "lessons": []},
                              ensure_ascii=False)

        monkeypatch.setattr(manager, "_complete", summary)
        monkeypatch.setattr(manager, "_start_memory_candidates",
                            lambda session, query, answer: candidates.append((session.id, query, answer)))
        try:
            session = manager.create(workspace_path=str(project))
            manager.submit(session, "项目交接", handoff_contract=CONTRACT)
            await asyncio.wait_for(session.task, 5)
            assert len(calls) == 2
            finals = [fields for kind, sid, fields in observer.events
                      if kind == "model.final" and sid == session.id]
            assert len(finals) == 1
            assert finals[0]["state"] == ("completed" if valid else "failed")
            assert session.record["messages"][finals[0]["message_index"]]["content"] == repair
            path = project / ".channelagent" / "memory.json"
            if valid:
                assert candidates == [(session.id, "项目交接", repair)]
                assert len(summaries) == 1
                assert json.loads(summaries[0].split("\n", 1)[1])["visible_result"] == repair
                assert session.record["last_run"]["workspace_memory"]["status"] == "saved"
                assert "受控项目事实" in path.read_text(encoding="utf-8")
            else:
                assert not any(fields["state"] == "completed" for fields in finals)
                assert candidates == []
                assert summaries == []
                assert session.record["last_run"]["handoff_validation_failed"] is True
                assert not path.exists()
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_repair_monetary_budget_keeps_existing_charges_and_blocks_downstream(
        isolated_store, tmp_path, monkeypatch):
    """真实请求装配、费用预留/结算；仅传输与 usage 为离线 double。"""
    monkeypatch.setattr(config, "SESSION_COST_LIMIT", 0.1)
    monkeypatch.setattr(config, "DAILY_COST_LIMIT", 0)
    monkeypatch.setattr(config, "MODEL_REQUESTS_PER_MINUTE", 0)
    monkeypatch.setattr(config, "INPUT_COST_PER_MILLION", 1)
    monkeypatch.setattr(config, "OUTPUT_COST_PER_MILLION", 1)
    monkeypatch.setattr(config, "MODEL_FALLBACKS", [])
    project = tmp_path / "budget-project"
    project.mkdir()
    sent, reservations = [], []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            sent.append(copy.deepcopy(kwargs))

            async def stream():
                yield SimpleNamespace(
                    usage=SimpleNamespace(prompt_tokens=40000, completion_tokens=40000),
                    choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(
                        content="说明文字。{}", reasoning_content=None, tool_calls=None))])

            return stream()

    monkeypatch.setattr(llm, "endpoint_client", lambda endpoint: Client())
    monkeypatch.setattr(llm, "model_options", lambda: {"max_tokens": 1000})
    monkeypatch.setattr(llm, "selected_model", lambda: config.MODEL)

    async def run():
        manager = SessionManager(isolated_store, model=llm.call_model)
        ledger = manager.budget_ledger
        original_reserve = ledger.reserve

        def reserve(session_id, cost):
            # 上游首次请求前保留已有费用，纠正不得重置本轮账本或更换计费身份。
            if not reservations:
                ticket = original_reserve(session_id, 0.02)
                ledger.settle(ticket, 0.02)
            reservations.append((session_id, cost, copy.deepcopy(ledger.state)))
            return original_reserve(session_id, cost)

        monkeypatch.setattr(ledger, "reserve", reserve)
        try:
            service, flow = _finalization_flow(manager, project)
            service.start(flow["id"])
            await asyncio.wait_for(service.tasks[flow["id"]], 5)
            result = service.get(flow["id"])["run"]
            assert len(sent) == 1
            assert len(reservations) == 2
            owner = reservations[0][0]
            assert reservations[1][0] == owner
            assert reservations[0][1] > 0
            assert reservations[1][1] > 0
            assert reservations[0][2]["sessions"][owner] == pytest.approx(0.02)
            assert reservations[1][2]["sessions"][owner] == pytest.approx(0.1)
            assert manager.budget_ledger is ledger
            assert ledger.state["sessions"] == {owner: pytest.approx(0.1)}
            assert sum(ledger.state["days"].values()) == pytest.approx(0.1)
            assert ledger.state["pending"] == {}
            assert ledger.state["requests"] == []
            session = manager.sessions[owner]
            assert session.record["status"] == "checkpoint"
            assert session.record["last_run"]["stop_reason"] == "cost_or_rate_budget"
            assert "SESSION_COST_LIMIT" in session.record["messages"][-1]["content"]
            assert session.record["last_run"]["model_rounds"] == 2
            assert session.record["last_run"]["handoff_finalization"]["status"] == "error"
            assert result["status"] == "failed"
            assert result["nodes"]["a"]["output_handoff"] == ""
            assert result["nodes"]["b"]["status"] == "blocked"
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("repair", ['{"state":"ok"}', "仍然不是 JSON"])
def test_failed_repaired_assistant_save_blocks_final_memory_and_downstream(
        isolated_store, tmp_path, monkeypatch, repair):
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", True)
    monkeypatch.setattr(config, "WORKSPACE_MEMORY_ENABLED", True)
    project = tmp_path / "save-project"
    project.mkdir()
    calls, failed, candidates, summaries = [], [], [], []
    observer = _finalization_observer()

    async def run():
        async def model(history, **kwargs):
            calls.append(copy.deepcopy(history))
            return {"role": "assistant", "content": "说明文字。{}" if len(calls) == 1 else repair}

        manager = SessionManager(isolated_store, model=model, observer=observer,
                                 permission_override="full_access")
        original_save = manager.save

        def save(session):
            messages = session.record["messages"]
            if (not failed and len(calls) == 2 and messages[-1].get("role") == "assistant"
                    and messages[-1].get("content") == repair):
                assert messages[-2].get("_context_reference") is True
                failed.append(copy.deepcopy(messages[-1]))
                future = asyncio.get_running_loop().create_future()
                future.set_exception(OSError("受控纠正回答保存失败"))
                return future
            return original_save(session)

        async def summary(prompt, *, session_id):
            summaries.append(prompt)
            raise AssertionError("保存失败不得提炼业务结果")

        monkeypatch.setattr(manager, "save", save)
        monkeypatch.setattr(manager, "_complete", summary)
        monkeypatch.setattr(manager, "_start_memory_candidates",
                            lambda session, query, answer: candidates.append(answer))
        try:
            service, flow = _finalization_flow(manager, project)
            service.start(flow["id"])
            await asyncio.wait_for(service.tasks[flow["id"]], 5)
            result = service.get(flow["id"])["run"]
            assert len(calls) == 2
            assert failed == [{"role": "assistant", "content": repair}]
            assert candidates == []
            assert summaries == []
            upstream = next(session for session in manager.sessions.values()
                            if session.record["last_run"].get("model_rounds") == 2)
            assert upstream.record["status"] == "error"
            assert "OSError" in upstream.record["error"]
            assert not any(kind == "model.final" and fields.get("state") == "completed"
                           for kind, sid, fields in observer.events)
            assert result["status"] == "failed"
            assert result["nodes"]["a"]["output_handoff"] == ""
            assert result["nodes"]["b"]["status"] == "blocked"
            path = project / ".channelagent" / "memory.json"
            if path.exists():
                # 错误终态可发布状态提示，但不得发布未提交的交接正文或项目事实。
                text = path.read_text(encoding="utf-8")
                assert repair not in text
                assert "说明文字" not in text
                import json
                memory = json.loads(text)
                assert memory["project"] == []
                assert memory["lessons"] == []
                assert all(item["outcome"] == "error" for item in memory["progress"])
            await manager.flush()
            saved = isolated_store.read_record(isolated_store.directory(upstream.id) / "session.json")
            assert saved["status"] == "error"
            assert saved["last_run"]["turn_id"] == upstream.record["last_run"]["turn_id"]
        finally:
            await manager.shutdown()

    asyncio.run(run())
