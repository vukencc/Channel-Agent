import asyncio
import json
from types import SimpleNamespace

from agent_core.core.flow_action_preflight import FlowActionPreflight
from agent_core.core.flow_staged_actions import FlowStagedActions


def run(coro):
    return asyncio.run(coro)


def setup(tmp_path):
    definition = {"name": "send", "description": "发送", "code": "def main(arguments): pass", "network": False,
                  "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                 "required": ["text"], "additionalProperties": False}}
    session = SimpleNamespace(id="s", cancelled=asyncio.Event(), record={"flow_project_id": "p", "last_run": {"turn_id": "t"}})
    calls = []
    async def tool(session, call, evidence):
        calls.append(call)
        evidence["source"] = "executed"
        return "完成"
    manager = SimpleNamespace(store=SimpleNamespace(directory=lambda _: tmp_path), permission_override=None, _tool=tool)
    async def check(*args, **kwargs):
        return ""
    gate = SimpleNamespace(manager=manager, session=session, blocked="", pending_tools=set(),
                           tool_key=lambda name, args: (name, args), check=check)
    staged = FlowStagedActions(gate)
    staged._authorized = lambda: {"send": definition}
    binding = {"session": "s", "turn": "t", "source_digest": "a"}
    staged._binding = lambda: dict(binding)
    staged.definitions = {"send": definition}
    staged.preflight = FlowActionPreflight([definition])
    return staged, calls, binding


def call(identifier, name, text):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps({"text": text}, ensure_ascii=False)}}


def test_single_confirmation_exactly_selected_action(tmp_path):
    async def scenario():
        staged, writes, _ = setup(tmp_path)
        preview = staged.preflight.preview_for_original("send")
        await staged.execute(call("a", preview, "第一条"), {})
        await staged.execute(call("b", preview, "第二条"), {})
        offer = await staged.confirmation_offer()
        assert offer["schema"]["function"]["name"] == "send"
        assert json.loads(offer["message"]["content"].split("\n", 1)[1]) == {"name": "send", "arguments": {"text": "第一条"}}
        assert not await staged.confirmation_validate([call("x", "send", "第二条")])
        assert not await staged.confirmation_validate([call("x", preview, "第一条")])
        assert not await staged.confirmation_validate([call("x", "send", "第一条"), call("y", "send", "第二条")])
        selected = call("x", "send", "第一条")
        assert await staged.confirmation_validate([selected])
        await staged.execute(selected, {})
        assert len(writes) == 1
        assert not await staged.confirmation_validate([selected])
    run(scenario())


def test_return_and_source_expiry_revoke_and_audit(tmp_path):
    async def scenario():
        staged, writes, binding = setup(tmp_path)
        preview = staged.preflight.preview_for_original("send")
        await staged.execute(call("a", preview, "原稿"), {})
        offer = await staged.confirmation_offer()
        assert await staged.confirmation_reject("returned")
        assert not await staged.confirmation_validate([call("x", "send", "原稿")])
        assert staged.pending and not writes
        assert await staged.confirmation_offer() is None
        await staged.execute(call("b", preview, "原稿"), {})
        offer2 = await staged.confirmation_offer()
        assert offer2
        binding["source_digest"] = "changed"
        assert not await staged.confirmation_validate([call("y", "send", "原稿")])
        evidence = {}
        await staged.reject_confirmation_call(call("y", "send", "原稿"), evidence, draft_id=offer2["draft_id"])
        assert staged.pending and not writes
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert any(row["draft_id"] == offer["draft_id"] and row["status"] == "returned" for row in rows)
        assert any(row["draft_id"] == offer2["draft_id"] and row["stage"] == "preview" and row["status"] == "approved" for row in rows)
        assert any(row["draft_id"] == offer2["draft_id"] and row["status"] == "confirmation_rejected" for row in rows)
        assert "原稿" not in (tmp_path / "audit.jsonl").read_text()
    run(scenario())


def test_full_large_argument_and_stop(tmp_path):
    async def scenario():
        staged, writes, _ = setup(tmp_path)
        content = "界" * 12000
        preview = staged.preflight.preview_for_original("send")
        draft = call("draft", preview, content)
        await staged.execute(draft, {})
        offer = await staged.confirmation_offer()
        message = offer["message"]["content"]
        assert draft["function"]["arguments"] in message
        assert json.loads(message.split("\n", 1)[1])["arguments"]["text"] == content
        assert await staged.confirmation_reject("stopped")
        assert staged.pending and not writes and not staged.preflight.approved_ids()
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        assert rows[-1]["status"] == "stopped" and content not in json.dumps(rows)
    run(scenario())


def test_definition_change_and_cancellation_cannot_confirm(tmp_path):
    async def scenario():
        staged, writes, _ = setup(tmp_path)
        preview = staged.preflight.preview_for_original("send")
        await staged.execute(call("draft", preview, "正文"), {})
        assert await staged.confirmation_offer()
        staged.definitions["send"]["code"] = "changed"
        assert not await staged.confirmation_validate([call("commit", "send", "正文")])
        assert staged.pending and not writes
        assert not staged.preflight.approved_ids()
        staged.session.cancelled.set()
        assert await staged.confirmation_offer() is None
    run(scenario())


def test_rejected_calls_are_audited_and_drafting_does_not_issue_others(tmp_path):
    async def scenario():
        staged, writes, _ = setup(tmp_path)
        preview = staged.preflight.preview_for_original("send")
        await staged.execute(call("a", preview, "一"), {})
        await staged.execute(call("b", preview, "二"), {})
        offer = await staged.confirmation_offer()
        for invocation in [call("bad-1", "send", "二"), call("bad-2", "other", "一")]:
            evidence = {}
            result = await staged.reject_confirmation_call(invocation, evidence)
            assert result.startswith("[已拒绝]") and evidence == {"source": "not_executed", "action_stage": "commit"}
        staged.begin_drafting()
        assert not staged.issued and len(staged.preflight.approved_ids()) == 2
        await staged.execute(call("direct", "send", "二"), {})
        assert not writes
        rows = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        rejected = [row for row in rows if row["status"] == "confirmation_rejected"]
        assert [row["tool_call_id"] for row in rejected] == ["bad-1", "bad-2"]
        assert all(row["draft_id"] == offer["draft_id"] for row in rejected)
        assert "一" not in json.dumps(rows, ensure_ascii=False)
    run(scenario())
