import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_core.core.flow_staged_actions import FlowStagedActions


def make_staged(tmp_path):
    definition = {"name": "send", "description": "发送", "code": "def main(arguments): pass", "network": False,
                  "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"], "additionalProperties": False}}
    session = SimpleNamespace(id="session", cancelled=asyncio.Event(), record={"flow_project_id": "p", "last_run": {"turn_id": "one"}})
    calls = []
    async def tool(session, call, evidence):
        calls.append(call)
        evidence["source"] = "executed"
        return "完成"
    manager = SimpleNamespace(store=SimpleNamespace(directory=lambda _: tmp_path), permission_override=None, _tool=tool)
    async def check(*args, **kwargs):
        return ""
    gate = SimpleNamespace(manager=manager, session=session, blocked="", pending_tools=set(),
                           tool_key=lambda name, args: (name, "key"), check=check)
    staged = FlowStagedActions(gate)
    staged._authorized = lambda: {"send": definition}
    staged._binding = lambda: {"turn": "one"}
    staged.definitions = {"send": definition}
    from agent_core.core.flow_action_preflight import FlowActionPreflight
    staged.preflight = FlowActionPreflight([definition])
    return staged, calls


def call(identifier, name):
    return {"id": identifier, "function": {"name": name, "arguments": '{"text":"private text"}'}}


def test_audit_three_stages_without_raw_text(tmp_path):
    asyncio.run(_audit_three_stages(tmp_path))


async def _audit_three_stages(tmp_path):
    staged, calls = make_staged(tmp_path)
    preview = staged.preflight.preview_for_original("send")
    await staged.execute(call("a", preview), {})
    staged.begin_request()
    await staged.execute(call("b", "send"), {})
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [(row["stage"], row["status"]) for row in rows] == [("preview", "approved"), ("commit_intent", "dispatching"), ("commit_result", "completed")]
    assert len({row["draft_id"] for row in rows}) == 1
    assert "private text" not in (tmp_path / "audit.jsonl").read_text()
    assert len(calls) == 1


def test_audit_failure_blocks_dispatch_and_uncertain_result_never_retries(tmp_path, monkeypatch):
    asyncio.run(_audit_failures(tmp_path, monkeypatch))


def test_unsuccessful_tool_diagnostic_preserves_stronger_blockers(tmp_path):
    staged, _ = make_staged(tmp_path)
    key = ("send", "key")
    staged.unresolved.add(key)
    staged.unsuccessful.add(key)
    staged.gate.pending_tools.add(key)
    assert staged.handoff_checkpoint()[0] == "flow_action_tool_unsuccessful"
    staged.gate.pending_tools.add(("other", "disputed"))
    assert staged.handoff_checkpoint()[0] == "flow_source_review"
    staged.gate.pending_tools.clear()
    staged.invalidated.add(key)
    assert staged.handoff_checkpoint()[0] == "flow_action_expired"
    staged.invalidated.clear()
    staged.gate.blocked = "动作审计失败"
    assert staged.handoff_checkpoint()[1] == "动作审计失败"
    staged.gate.blocked = ""
    staged.session.cancelled.set()
    assert staged.handoff_checkpoint()[0] != "flow_action_tool_unsuccessful"


async def _audit_failures(tmp_path, monkeypatch):
    import agent_core.core.flow_staged_actions as module
    staged, calls = make_staged(tmp_path)
    preview = staged.preflight.preview_for_original("send")
    await staged.execute(call("a", preview), {})
    staged.begin_request()
    real = module.append_audit
    monkeypatch.setattr(module, "append_audit", lambda *a, **k: (_ for _ in ()).throw(OSError("private")))
    await staged.execute(call("b", "send"), {})
    assert not calls and staged.gate.blocked
    monkeypatch.setattr(module, "append_audit", real)
    staged.gate.blocked = ""
    await staged.execute(call("c", "send"), {})
    assert not calls

    staged, calls = make_staged(tmp_path / "second")
    await staged.execute(call("d", staged.preflight.preview_for_original("send")), {})
    staged.begin_request()
    def fail_result(path, line, **kwargs):
        if json.loads(line)["stage"] == "commit_result":
            raise OSError("private")
        return real(path, line, **kwargs)
    monkeypatch.setattr(module, "append_audit", fail_result)
    result = await staged.execute(call("e", "send"), {})
    assert len(calls) == 1 and staged.gate.blocked and "核对" in result and result.startswith("[失败] ")
    await staged.execute(call("f", "send"), {})
    assert len(calls) == 1


def test_revocation_flush_and_preview_audit_failure(tmp_path, monkeypatch):
    asyncio.run(_revocation_and_preview_failure(tmp_path, monkeypatch))


async def _revocation_and_preview_failure(tmp_path, monkeypatch):
    import agent_core.core.flow_staged_actions as module
    staged, calls = make_staged(tmp_path)
    preview = staged.preflight.preview_for_original("send")
    await staged.execute(call("a", preview), {})
    staged._expire_changed({"turn": "changed"})
    assert await staged.flush_audit()
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["approved", "context_changed"]
    assert rows[0]["draft_id"] == rows[1]["draft_id"]
    monkeypatch.setattr(module, "append_audit", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    staged, calls = make_staged(tmp_path / "failed")
    result = await staged.execute(call("b", staged.preflight.preview_for_original("send")), {})
    assert "审计失败" in result and staged.gate.blocked
    assert not staged.preflight.approved_ids() and not calls


@pytest.mark.parametrize("change", ["definition", "source", "cancel"])
def test_change_during_intent_audit_stops_dispatch(tmp_path, monkeypatch, change):
    asyncio.run(_change_during_intent(tmp_path, monkeypatch, change))


async def _change_during_intent(tmp_path, monkeypatch, change):
    import agent_core.core.flow_staged_actions as module
    staged, calls = make_staged(tmp_path)
    context = {"turn": "one", "source_digest": "original"}
    staged._binding = lambda: dict(context)
    await staged.execute(call("a", staged.preflight.preview_for_original("send")), {})
    staged.begin_request()
    real = module.append_audit
    def mutate_during_intent(path, line, **kwargs):
        row = json.loads(line)
        real(path, line, **kwargs)
        if row["stage"] == "commit_intent":
            if change == "definition":
                staged.definitions["send"]["code"] = "def main(arguments): return 1"
            elif change == "source":
                context["source_digest"] = "changed"
            else:
                staged.session.cancelled.set()
    monkeypatch.setattr(module, "append_audit", mutate_during_intent)
    if change == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await staged.execute(call("b", "send"), {})
    else:
        await staged.execute(call("b", "send"), {})
    assert not calls
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert rows[-1]["stage"] == "commit_result"
    assert rows[-1]["status"] == {"definition": "authorization_changed", "source": "context_changed", "cancel": "cancelled"}[change]
    assert all(row["session_id"] == "session" and row["turn_id"] == "one" for row in rows)
    assert all(row["tool_call_id"] == row["id"] and row["tool_name"] == row["name"] for row in rows)
    monkeypatch.setattr(module, "append_audit", real)
    staged.definitions["send"]["code"] = "def main(arguments): pass"
    context["source_digest"] = "original"
    staged.session.cancelled.clear()
    await staged.execute(call("without-reapproval", "send"), {})
    assert not calls
    result = await staged.execute(call("new-draft", staged.preflight.preview_for_original("send")), {})
    assert "草稿已批准" in result
    staged.begin_request()
    await staged.execute(call("new-commit", "send"), {})
    await staged.execute(call("repeat", "send"), {})
    assert len(calls) == 1


def test_invalid_replacement_remains_unresolved_then_valid_replacement_completes(tmp_path):
    asyncio.run(_invalid_replacement(tmp_path))


async def _invalid_replacement(tmp_path):
    staged, calls = make_staged(tmp_path)
    staged.gate.tool_key = lambda name, args: (name, "empty" if args == "{}" else "key")
    preview = staged.preflight.preview_for_original("send")
    await staged.execute(call("first", preview), {})
    first = staged.preflight.approved_ids()
    invalid = call("invalid", preview)
    invalid["function"]["arguments"] = '{"text":123}'
    await staged.execute(invalid, {})
    assert staged.pending and staged.preflight.approved_ids() == first
    revised = call("revised", preview)
    revised["function"]["arguments"] = '{"text":"revised"}'
    assert "草稿已批准" in await staged.execute(revised, {})
    assert not first.intersection(staged.preflight.approved_ids())
    staged.begin_request()
    revised["id"] = "commit"
    revised["function"]["name"] = "send"
    await staged.execute(revised, {})
    assert len(calls) == 1 and not staged.pending


def test_concurrent_formal_calls_dispatch_once(tmp_path):
    asyncio.run(_concurrent_calls(tmp_path))


async def _concurrent_calls(tmp_path):
    staged, calls = make_staged(tmp_path)
    await staged.execute(call("draft", staged.preflight.preview_for_original("send")), {})
    staged.begin_request()
    entered, release = asyncio.Event(), asyncio.Event()
    async def tool(session, invocation, evidence):
        calls.append(invocation)
        entered.set()
        await release.wait()
        evidence["source"] = "executed"
        return "完成"
    staged.manager._tool = tool
    first = asyncio.create_task(staged.execute(call("first", "send"), {}))
    await entered.wait()
    second = await staged.execute(call("second", "send"), {})
    release.set()
    await first
    assert len(calls) == 1 and "没有上一轮" in second
    rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(row["id"] == "first" and row["stage"] == "commit_result" and row["status"] == "completed" for row in rows)


def test_newly_authorized_tool_requires_its_own_preview(tmp_path):
    async def run():
        staged, writes = make_staged(tmp_path)
        # 模型响应期间工具集刚扩充，响应中猜到原名也不能从旧目录直接派发。
        from agent_core.core.flow_action_preflight import FlowActionPreflight
        staged.preflight = FlowActionPreflight([])
        async def legacy_dispatch(tool_call, evidence):
            return await staged.manager._tool(staged.session, tool_call, evidence)
        staged.gate.execute_tool = legacy_dispatch
        result = await staged.execute(call("new", "send"), {})
        assert not writes and staged.pending
        assert result.startswith("[已拒绝]")
        assert staged.session.record["last_run"]["action_preflights"][-1]["status"] == "authorization_changed"
    asyncio.run(run())
