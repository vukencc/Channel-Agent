import copy
import json

import pytest

from agent_core.core.flow_review_sources import review_sources
from agent_core.core.tool_observations import extract_observation, observe_text


def record():
    text = "前缀甲正文隔开乙正文后缀"
    observation = observe_text(text, reader_tool="rag_search", body_spans=((2, 5), (7, 10)), recovery_notice="重查")
    call = {"id": "call", "type": "function", "function": {"name": "rag_search", "arguments": json.dumps({"query": "问题", "item_ref": "A"})}}
    result = {"role": "tool", "tool_call_id": "call", "content": str(observation), "_provenance": "executed", "_event_id": "event", "_context_observation": extract_observation(observation)}
    event = {"event_id": "event", "tool_call_id": "call", "turn_id": "turn", "name": "rag_search", "source": "executed", "outcome": "completed", "execution_progress": True}
    return {"messages": [{"role": "user", "content": "本轮"}, {"role": "assistant", "tool_calls": [call]}, result], "last_run": {"turn_id": "turn", "input_index": 0, "tools": [event]}}


def test_spans_are_separate_and_budget_never_claims_truncation_is_complete():
    data = record()
    rows = review_sources(data, char_limit=6)
    assert [r["text"] for r in rows] == ["甲正文", "乙正文"]
    assert len({r["id"] for r in rows}) == 2
    assert all(r["kind"] == "rag_search" and r["item_ref"] == "A" for r in rows)
    assert review_sources(data, char_limit=2) == []
    assert [r["text"] for r in review_sources(data, char_limit=3)] == ["甲正文"]


@pytest.mark.parametrize("mutation", ["body", "reader", "provenance", "outcome", "turn", "event_call", "duplicate_event", "duplicate_call", "duplicate_result", "old_turn", "unpaired", "fake_body", "history", "duplicate_event_call", "event_progress", "bad_call_type"])
def test_rejects_untrusted_or_ambiguous_pairs(mutation):
    data = record()
    messages, event = data["messages"], data["last_run"]["tools"][0]
    if mutation == "body": messages[2]["content"] += "改"
    elif mutation == "reader": messages[2]["_context_observation"]["reader_tool"] = "read_file"
    elif mutation == "provenance": messages[2]["_provenance"] = "result_unknown"
    elif mutation == "outcome": event["outcome"] = "timeout"
    elif mutation == "turn": event["turn_id"] = "old"
    elif mutation == "event_call": event["tool_call_id"] = "other"
    elif mutation == "duplicate_event": data["last_run"]["tools"].append(copy.deepcopy(event))
    elif mutation == "duplicate_call": messages[1]["tool_calls"].append(copy.deepcopy(messages[1]["tool_calls"][0]))
    elif mutation == "duplicate_result": messages.append(copy.deepcopy(messages[2]))
    elif mutation == "old_turn": messages.append({"role": "user", "content": "新轮"})
    elif mutation == "unpaired": messages.insert(2, {"role": "assistant", "content": "中断"})
    elif mutation == "fake_body": messages[2].pop("_context_observation")
    elif mutation == "duplicate_event_call":
        other = copy.deepcopy(event)
        other["event_id"] = "other"
        data["last_run"]["tools"].append(other)
    elif mutation == "event_progress": event["execution_progress"] = False
    elif mutation == "bad_call_type": messages[1]["tool_calls"][0]["type"] = "other"
    elif mutation == "history": messages[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"query": "q", "source": "history"})
    assert review_sources(data, char_limit=100) == []


def test_read_file_path_and_handoff_are_not_evidence():
    data = record()
    text = "分页头\n文件正文\n分页尾"
    observation = observe_text(text, reader_tool="read_file", body_spans=((4, 8),), recovery_notice="重读")
    call, result = data["messages"][1]["tool_calls"][0], data["messages"][2]
    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "客户.txt"})}
    result.update(content=text, _context_observation=extract_observation(observation))
    data["last_run"]["tools"][0]["name"] = "read_file"
    data["messages"][0]["content"] = "上游声称的混合交接"
    rows = review_sources(data, char_limit=100)
    assert len(rows) == 1 and rows[0]["text"] == "文件正文" and rows[0]["path"] == "客户.txt"


@pytest.mark.parametrize("structured", [False, True])
def test_real_read_file_in_persisted_session(tmp_path, monkeypatch, structured):
    import asyncio
    from agent_core import config
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", structured)
    monkeypatch.setattr(config, "CONTEXT_SUMMARY", False)
    monkeypatch.setattr(config, "MEMORY_AUTO_EXTRACT", False)
    monkeypatch.setattr(config, "RAG_ASSESS", False)
    work = tmp_path / "work"
    work.mkdir()
    calls = 0
    async def model(history, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"a.txt\"}"}}]}
        return {"role": "assistant", "content": "完成"}
    async def run():
        store = SessionStore(tmp_path / "state", work)
        manager = SessionManager(store, model=model)
        try:
            session = manager.create()
            (manager.workspace(session) / "a.txt").write_text("真实原文🙂", encoding="utf-8")
            manager.submit(session, "读取")
            await session.task
            rows = review_sources(store.load_all()[0], char_limit=100)
            assert len(rows) == 1
            assert rows[0]["text"] == "真实原文🙂" and rows[0]["path"] == "a.txt"
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def with_user_input(data, text="客户直接输入"):
    import hashlib
    data["id"] = "session"
    message = data["messages"][0]
    message["content"] = "节点提示|" + text + "|上游交接"
    start = len("节点提示|")
    end = start + len(text)
    message["_flow_user_input"] = {"owner": "session", "turn": "turn", "start": start, "end": end,
        "full_digest": hashlib.sha256(message["content"].encode()).hexdigest(),
        "span_digest": hashlib.sha256(text.encode()).hexdigest()}
    return data


def test_direct_user_input_is_separate_and_raw_sources_have_priority():
    data = with_user_input(record())
    rows = review_sources(data, char_limit=12)
    assert [r["text"] for r in rows] == ["甲正文", "乙正文", "客户直接输入"]
    assert rows[-1]["kind"] == "user_input" and rows[-1]["scope"] == "user_report" and rows[-1]["id"] == "U1"
    assert [r["text"] for r in review_sources(data, char_limit=6)] == ["甲正文", "乙正文"]


@pytest.mark.parametrize("mutation", ["owner", "turn", "range", "full_digest", "span_digest", "missing_id", "old", "overlap_handoff"])
def test_direct_input_rejects_wrong_identity_spans_and_handoff(mutation):
    data = with_user_input(record())
    message, marker = data["messages"][0], data["messages"][0]["_flow_user_input"]
    if mutation == "owner": marker["owner"] = "other"
    elif mutation == "turn": marker["turn"] = "old"
    elif mutation == "range": marker["end"] = len(message["content"]) + 1
    elif mutation == "full_digest": marker["full_digest"] = "0" * 64
    elif mutation == "span_digest": marker["span_digest"] = "0" * 64
    elif mutation == "missing_id": data.pop("id")
    elif mutation == "old": data["messages"].append({"role": "user", "content": "新轮"})
    elif mutation == "overlap_handoff": message["_flow_handoff"] = {"start": marker["start"], "end": marker["end"]}
    assert not any(row["kind"] == "user_input" for row in review_sources(data, char_limit=100))


def test_direct_input_without_tool_sources_allows_adjacent_handoff():
    data = with_user_input(record())
    data["messages"] = data["messages"][:1]
    data["last_run"]["tools"] = []
    marker = data["messages"][0]["_flow_user_input"]
    data["messages"][0]["_flow_handoff"] = {"start": marker["end"], "end": len(data["messages"][0]["content"])}
    rows = review_sources(data, char_limit=6)
    assert rows == [{"id": "U1", "kind": "user_input", "text": "客户直接输入", "scope": "user_report"}]
    assert review_sources(data, char_limit=5) == []


def test_agent_message_does_not_start_new_user_turn():
    data = with_user_input(record())
    data["messages"].append({"role": "user", "content": "上游自述", "_agent_message": {}})
    rows = review_sources(data, char_limit=100)
    assert [row["text"] for row in rows] == ["甲正文", "乙正文", "客户直接输入"]


def test_call_id_can_be_reused_in_an_older_turn():
    data = record()
    old = copy.deepcopy(data["messages"])
    old[0]["content"] = "旧轮"
    data["messages"] = old + data["messages"]
    data["last_run"]["input_index"] = len(old)
    rows = review_sources(data, char_limit=6)
    assert [row["text"] for row in rows] == ["甲正文", "乙正文"]


def test_duplicate_source_spans_do_not_consume_budget():
    data = record()
    duplicate = copy.deepcopy(data["messages"][1:])
    duplicate[0]["tool_calls"][0]["id"] = "duplicate"
    duplicate[1]["tool_call_id"] = "duplicate"
    duplicate[1]["_event_id"] = "duplicate_event"
    event = copy.deepcopy(data["last_run"]["tools"][0])
    event.update(event_id="duplicate_event", tool_call_id="duplicate")
    different = copy.deepcopy(duplicate)
    different[0]["tool_calls"][0]["id"] = "different"
    different[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"query": "q", "item_ref": "B"})
    different[1]["tool_call_id"] = "different"
    different[1]["_event_id"] = "different_event"
    other_event = copy.deepcopy(event)
    other_event.update(event_id="different_event", tool_call_id="different")
    data["messages"] += duplicate + different
    data["last_run"]["tools"] += [event, other_event]
    rows = review_sources(data, char_limit=12)
    assert [(row["item_ref"], row["text"]) for row in rows] == [("A", "甲正文"), ("A", "乙正文"), ("B", "甲正文"), ("B", "乙正文")]


def test_duplicate_file_path_is_deduplicated_but_other_path_is_retained():
    data = record()
    observation = observe_text("同一段", reader_tool="read_file", body_spans=((0, 3),), recovery_notice="重读")
    call, result = data["messages"][1]["tool_calls"][0], data["messages"][2]
    call["function"] = {"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}
    result.update(content=str(observation), _context_observation=extract_observation(observation))
    data["last_run"]["tools"][0]["name"] = "read_file"
    for identifier, path in (("two", "a.txt"), ("three", "b.txt")):
        pair = copy.deepcopy(data["messages"][1:3])
        pair[0]["tool_calls"][0]["id"] = identifier
        pair[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": path})
        pair[1].update(tool_call_id=identifier, _event_id=identifier)
        event = copy.deepcopy(data["last_run"]["tools"][0])
        event.update(event_id=identifier, tool_call_id=identifier)
        data["messages"] += pair
        data["last_run"]["tools"].append(event)
    rows = review_sources(data, char_limit=6)
    assert [(row["path"], row["text"]) for row in rows] == [("a.txt", "同一段"), ("b.txt", "同一段")]
