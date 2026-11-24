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
@pytest.mark.parametrize("prefix", ["", "[失败] ", "[错误] ", None])
def test_real_read_file_in_persisted_session(tmp_path, monkeypatch, structured, prefix):
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
            if prefix is not None:
                (manager.workspace(session) / "a.txt").write_text(prefix + "真实原文🙂", encoding="utf-8")
            manager.submit(session, "读取")
            await session.task
            record = store.load_all()[0]
            rows = review_sources(record, char_limit=100)
            if prefix is None:
                event = record["last_run"]["tools"][0]
                receipt = next(row for row in record["messages"] if row.get("tool_call_id") == "read")
                assert event["outcome"] == "error" and event["execution_progress"] is False
                assert receipt["content"].startswith("[失败] 文件不存在") and "_context_observation" not in receipt
                assert rows == []
                return
            assert len(rows) == 1
            assert rows[0]["text"] == prefix + "真实原文🙂" and rows[0]["path"] == "a.txt"
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


@pytest.mark.parametrize('invalid', [None, 'wrong_turn', 'different_run_input', 'duplicate', 'agent_message', 'handoff'])
def test_restored_user_input_requires_exact_origin_and_complete_budget(invalid):
    text = '客户直接输入'
    data = with_user_input(record(), text=text)
    original = data['messages'][0]
    ordinary = with_user_input(record(), text='来自另一轮的历史')['messages'][0]
    ordinary['_flow_user_input']['turn'] = 'unrelated'
    data['messages'] = [ordinary, original, {'role': 'user', 'content': '继续'}]
    data['last_run'] = {'turn_id': 'continuation', 'input_index': 2, 'tools': []}
    restore = ('turn', text)
    if invalid == 'wrong_turn':
        restore = ('missing', text)
    elif invalid == 'different_run_input':
        restore = ('turn', '已修改的运行输入')
    elif invalid == 'duplicate':
        ordinary['_flow_user_input']['turn'] = 'turn'
    elif invalid == 'agent_message':
        original['_agent_message'] = {}
    elif invalid == 'handoff':
        marker = original['_flow_user_input']
        original['_flow_handoff'] = {'start': marker['start'], 'end': marker['end']}
    rows = review_sources(data, char_limit=len(text), restored_input=restore)
    assert rows == ([] if invalid else [{'id': 'U1', 'kind': 'user_input', 'scope': 'user_report', 'text': text}])
    assert review_sources(data, char_limit=len(text) - 1, restored_input=restore) == []


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


def test_real_custom_read_result_is_lower_trust(sandbox_env, monkeypatch, lookup_command_transport):
    from agent_core import config
    from agent_core.core.custom_tools import custom_registry, validate_custom_tools

    monkeypatch.setattr(config, "TOOL_PERMISSION_POLICY", "full_access")
    spec = {"name": "lookup_feedback", "description": "读取", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
            "code": "import json\nfrom pathlib import Path\ndef main(arguments):\n    return json.loads(Path(arguments[\"path\"]).read_text(encoding=\"utf-8\"))\n"}
    (sandbox_env / "feedback.json").write_text("[{\"id\": \"A\", \"text\": \"反馈\"}]", encoding="utf-8")
    lookup_command_transport(spec, sandbox_env)
    content = custom_registry([spec])["lookup_feedback"].run(json.dumps({"path": "feedback.json"}))
    assert content.startswith("[退出码] 0\n[stdout]\n")
    data = record()
    data.update(flow_id="flow", flow_node_id="node", custom_tools=validate_custom_tools([spec]), tool_names=["lookup_feedback"])
    call, result = data["messages"][1]["tool_calls"][0], data["messages"][2]
    call["function"] = {"name": "lookup_feedback", "arguments": json.dumps({"path": "feedback.json"})}
    result.update(content=content)
    result.pop("_context_observation")
    data["last_run"]["tools"][0]["name"] = "lookup_feedback"
    rows = review_sources(data, char_limit=1000)
    assert len(rows) == 1
    assert rows[0]["kind"] == "custom_result" and rows[0]["scope"] == "tool_report"
    assert rows[0]["path"] == "feedback.json" and rows[0]["tool"] == "lookup_feedback"
    assert json.loads(rows[0]["text"]) == [{"id": "A", "text": "反馈"}]


def custom_record():
    data = record()
    spec = {"name": "lookup", "description": "测试", "parameters": {"type": "object", "properties": {"path": {"type": "string", "default": "input.json"}}}, "code": "def main(arguments): return []"}
    data.update(flow_id="f", flow_node_id="n", custom_tools=[spec], tool_names=["lookup"])
    data["messages"][1]["tool_calls"][0]["function"] = {"name": "lookup", "arguments": json.dumps({"path": "input.json", "ids": ["CF-01"], "limit": 10})}
    data["messages"][2].update(content="[退出码] 0\n[stdout]\n[{\"id\":\"CF-01\",\"text\":\"客户反馈\"}]\n")
    data["messages"][2].pop("_context_observation")
    data["last_run"]["tools"][0]["name"] = "lookup"
    return data


def test_custom_default_path_is_only_a_hint():
    data = custom_record()
    data["messages"][1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"ids": ["CF-01"], "limit": 10})
    rows = review_sources(data, char_limit=1000)
    assert len(rows) == 1 and rows[0]["path_hint"] == "input.json" and "path" not in rows[0]


@pytest.mark.parametrize("mutation", ["denied", "timeout", "nonzero", "truncated", "invalid_json", "fake_status", "stderr", "unknown_tool", "disabled", "no_node", "duplicate", "old", "wrong_event", "absolute", "traversal", "write_echo", "write_receipt", "long_id", "scalar"])
def test_custom_report_rejects_untrusted_cases(mutation):
    data = custom_record()
    call, result, event = data["messages"][1]["tool_calls"][0], data["messages"][2], data["last_run"]["tools"][0]
    if mutation == "denied": result["_provenance"] = "not_executed"
    elif mutation == "timeout": event["outcome"] = "timeout"
    elif mutation == "nonzero": result["content"] = result["content"].replace("退出码] 0", "退出码] 1")
    elif mutation == "truncated": result["content"] += "[输出已截断]"
    elif mutation == "invalid_json": result["content"] = "[退出码] 0\n[stdout]\n[{"
    elif mutation == "fake_status": result["content"] = "[退出码] 0\n[stdout]\n[退出码] 0\n[stdout]\n{}"
    elif mutation == "stderr": result["content"] += "\n[stderr]\n警告"
    elif mutation == "unknown_tool": data["custom_tools"] = []
    elif mutation == "disabled": data["tool_names"] = []
    elif mutation == "no_node": data.pop("flow_node_id")
    elif mutation == "duplicate": data["messages"].append(copy.deepcopy(result))
    elif mutation == "old": data["messages"].append({"role": "user", "content": "下轮"})
    elif mutation == "wrong_event": event["tool_call_id"] = "other"
    elif mutation == "absolute": call["function"]["arguments"] = json.dumps({"path": "/tmp/x"})
    elif mutation == "traversal": call["function"]["arguments"] = json.dumps({"path": "../x"})
    elif mutation == "write_echo": call["function"]["arguments"] = json.dumps({"path": "input.json", "message": "草稿正文"})
    elif mutation == "write_receipt": result["content"] = '[退出码] 0\n[stdout]\n{"saved":true}'
    elif mutation == "long_id": call["function"]["arguments"] = json.dumps({"path": "input.json", "ids": ["x" * 65]})
    elif mutation == "scalar": result["content"] = "[退出码] 0\n[stdout]\n\"文本\""
    assert review_sources(data, char_limit=1000) == []


def test_custom_report_yields_to_user_and_has_half_budget_cap():
    data = with_user_input(custom_record())
    report = json.loads(data["messages"][2]["content"].split("[stdout]\n", 1)[1])
    report_length = len(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    rows = review_sources(data, char_limit=report_length * 2)
    assert [row["kind"] for row in rows] == ["user_input", "custom_result"]
    rows = review_sources(data, char_limit=report_length * 2 - 1)
    assert [row["kind"] for row in rows] == ["user_input"]


def test_first_party_same_path_and_content_suppresses_custom_duplicate():
    data = custom_record()
    stdout = data["messages"][2]["content"].split("[stdout]\n", 1)[1].strip()
    observation = observe_text(stdout, reader_tool="read_file", body_spans=((0, len(stdout)),), recovery_notice="重读")
    call = {"id": "builtin", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"path": "input.json"})}}
    result = {"role": "tool", "tool_call_id": "builtin", "content": stdout, "_provenance": "executed", "_event_id": "builtin_event", "_context_observation": extract_observation(observation)}
    event = {"event_id": "builtin_event", "tool_call_id": "builtin", "turn_id": "turn", "name": "read_file", "source": "executed", "outcome": "completed", "execution_progress": True}
    data["messages"] += [{"role": "assistant", "tool_calls": [call]}, result]
    data["last_run"]["tools"].append(event)
    rows = review_sources(data, char_limit=1000)
    assert len(rows) == 1 and rows[0]["kind"] == "read_file"

def selector_result(arguments, output):
    data = custom_record()
    data["custom_tools"][0]["parameters"]["properties"] = {
        "path": {"type": "string"}, "limit": {"type": "integer"},
    }
    data["messages"][1]["tool_calls"][0]["function"]["arguments"] = json.dumps(arguments)
    data["messages"][2]["content"] = "[退出码] 0\n[stdout]\n" + json.dumps(output, ensure_ascii=False)
    return data


def test_selector_only_lookup_with_source_records_has_no_attributed_path():
    output = {"ok": True, "path": "data.json", "total": 2, "count": 2, "skipped": 0,
              "items": [{"id": "A", "text": "客户描述 A"}, {"id": "B", "text": "客户描述 B"}]}
    sources = review_sources(selector_result({"limit": 0}, output), char_limit=4000)
    assert len(sources) == 1
    row = sources[0]
    assert row["kind"] == "custom_result" and row["scope"] == "tool_report"
    assert "path" not in row and "path_hint" not in row
    assert json.loads(row["text"]) == output


@pytest.mark.parametrize("output", [
    {"ok": False, "items": [{"id": "A", "text": "错误回显"}]},
    {"saved": True, "items": [{"id": "A", "text": "写入回显"}]},
    [{"id": "A", "status": "written"}],
    [{"id": "A", "text": "一"}, {"id": "A", "text": "二"}],
    {"items": [{"id": "A", "text": "一"}], "count": 2},
])
def test_selector_without_path_does_not_promote_status_or_ambiguous_records(output):
    assert review_sources(selector_result({"limit": 5}, output), char_limit=4000) == []


def test_selector_without_path_requires_selection_only_declared_parameters():
    output = [{"id": "A", "text": "一"}]
    data = selector_result({"limit": 1}, output)
    data["custom_tools"][0]["parameters"]["properties"]["body"] = {"type": "string"}
    assert review_sources(data, char_limit=4000) == []
    assert review_sources(selector_result({"limit": -1}, output), char_limit=4000) == []


def echo_record():
    data = custom_record()
    spec = data["custom_tools"][0]
    spec["parameters"] = {"type": "object", "properties": {"specimen": {"type": "string"}, "method": {"type": "string"}}, "required": ["specimen", "method"], "additionalProperties": False}
    args = {"specimen": "矿石甲", "method": "建议进行光谱测定"}
    saved = {**args, "sample_id": "M1"}
    call, result, event = data["messages"][1]["tool_calls"][0], data["messages"][2], data["last_run"]["tools"][0]
    call["function"]["arguments"] = json.dumps(args, ensure_ascii=False)
    result["content"] = "[退出码] 0\n[stdout]\n" + json.dumps({"ok": True, "sample": saved}, ensure_ascii=False)
    event["action_stage"] = "commit"
    text = json.dumps(saved, ensure_ascii=False) + "\n" + json.dumps({"specimen": "矿石乙", "method": "外部记录", "sample_id": "M2"}, ensure_ascii=False)
    observation = observe_text(text, reader_tool="read_file", body_spans=((0, len(text)),), recovery_notice="重读")
    read = {"id": "read", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":\"samples.jsonl\"}"}}
    data["messages"] += [{"role": "assistant", "tool_calls": [read]}, {"role": "tool", "tool_call_id": "read", "content": text, "_provenance": "executed", "_event_id": "read-event", "_context_observation": extract_observation(observation)}]
    data["last_run"]["tools"].append({"event_id": "read-event", "tool_call_id": "read", "turn_id": "turn", "name": "read_file", "source": "executed", "outcome": "completed", "execution_progress": True})
    return data, text


def test_formal_receipt_echo_identifies_only_submitted_fields_and_preserves_source():
    from agent_core.core.flow_draft_review import review_messages
    data, text = echo_record()
    sources = review_sources(data, char_limit=4000)
    row = next(row for row in sources if row["kind"] == "read_file")
    assert row["text"] == text
    payload = json.loads(review_messages("草稿", sources)[1]["content"])
    assert payload["sources"][0]["submitted_echoes"][0]["fields"] == ["method", "specimen"]


@pytest.mark.parametrize("mutation", ["preview", "no_stage", "untrusted", "wrong_turn", "disabled", "mismatch", "duplicate_key", "duplicate_line", "truncated", "read_before", "duplicate_call"])
def test_echo_rejects_ambiguous_or_unconfirmed_evidence(mutation):
    data, text = echo_record()
    call, result = data["messages"][1]["tool_calls"][0], data["messages"][2]
    event = data["last_run"]["tools"][0]
    if mutation == "preview": event["action_stage"] = "preview"
    elif mutation == "no_stage": event.pop("action_stage")
    elif mutation == "untrusted": result["_provenance"] = "unknown"
    elif mutation == "wrong_turn": event["turn_id"] = "other"
    elif mutation == "disabled": data["tool_names"] = []
    elif mutation == "mismatch": result["content"] = result["content"].replace("矿石甲", "矿石丙")
    elif mutation == "duplicate_key": result["content"] = result["content"].replace("\"ok\": true", "\"ok\": true, \"ok\": true")
    elif mutation in {"duplicate_line", "truncated"}:
        text = text.splitlines()[0] + ("\n" + text.splitlines()[0] if mutation == "duplicate_line" else "\n{")
        observation = observe_text(text, reader_tool="read_file", body_spans=((0, len(text)),), recovery_notice="重读")
        data["messages"][-1].update(content=text, _context_observation=extract_observation(observation))
    elif mutation == "read_before": data["messages"] = [data["messages"][0]] + data["messages"][3:] + data["messages"][1:3]
    elif mutation == "duplicate_call": data["messages"][1]["tool_calls"].append(copy.deepcopy(call))
    rows = review_sources(data, char_limit=4000)
    assert all("_submitted_echoes" not in row for row in rows)
    assert any(row.get("text") == text for row in rows)


def test_echo_projection_revalidates_full_line():
    from agent_core.core.flow_draft_review import review_messages
    data, text = echo_record()
    source = next(row for row in review_sources(data, char_limit=4000) if row["kind"] == "read_file")
    source["text"] = text.splitlines()[1]
    payload = json.loads(review_messages("草稿", [source])[1]["content"])
    assert "submitted_echoes" not in payload["sources"][0]


def test_echo_never_displaces_existing_sources():
    data, text = echo_record()
    data = with_user_input(data, text='独立用户材料')
    without = copy.deepcopy(data)
    without["last_run"]["tools"][0].pop("action_stage")
    for limit in range(len(text), len(text) + 250):
        actual = review_sources(data, char_limit=limit)
        baseline = review_sources(without, char_limit=limit)
        assert [(r["id"], r["text"]) for r in actual] == [(r["id"], r["text"]) for r in baseline]


def test_echo_request_does_not_repeat_source_record():
    from agent_core.core.flow_draft_review import review_messages
    data, text = echo_record()
    history = review_messages("草稿", review_sources(data, char_limit=4000))
    assert history[1]["content"].count("建议进行光谱测定") == 1
    marker = json.loads(history[1]["content"])["sources"][0]["submitted_echoes"][0]
    assert set(marker) == {"record_start", "record_end", "fields"}
    assert text[marker["record_start"]:marker["record_end"]] == text.splitlines()[0]


def memory_read_record(path):
    data = record()
    text = "项目上下文中的记载。"
    observation = observe_text(text, reader_tool="read_file", body_spans=((0, len(text)),),
                               recovery_notice="重读")
    data["messages"][1]["tool_calls"][0]["function"] = {
        "name": "read_file", "arguments": json.dumps({"path": path})
    }
    data["messages"][2].update(content=text, _context_observation=extract_observation(observation))
    data["last_run"]["tools"][0]["name"] = "read_file"
    return data


@pytest.mark.parametrize("path", [".channelagent/memory.json", "./.channelagent/memory.json"])
def test_workspace_memory_entry_is_labeled_and_serialized(path):
    from agent_core.core.flow_draft_review import MEMORY_GUIDANCE, review_messages
    from agent_core.core.flow_review_passages import review_passages

    rows = review_sources(memory_read_record(path), char_limit=100)
    assert rows[0]["workspace_memory_path"] is True
    assert rows[0]["text"] == "项目上下文中的记载。"
    messages = review_messages("草稿", rows)
    payload = json.loads(messages[1]["content"])
    assert payload["sources"][0]["workspace_memory_path"] is True
    assert MEMORY_GUIDANCE in messages[0]["content"]
    assert review_passages(rows)[0]["text"] == rows[0]["text"]


@pytest.mark.parametrize("path", [
    "other/.channelagent/memory.json", "memory.json", ".channelagent/memory.json.bak",
    "@other/.channelagent/memory.json", "../.channelagent/memory.json",
    "/workspace/.channelagent/memory.json", "C:\\workspace\\.channelagent\\memory.json",
])
def test_unrelated_or_unverified_paths_do_not_get_memory_label(path):
    rows = review_sources(memory_read_record(path), char_limit=100)
    assert rows and "workspace_memory_path" not in rows[0]


def test_unpaired_memory_read_cannot_produce_label():
    data = memory_read_record(".channelagent/memory.json")
    data["messages"][2]["_provenance"] = "result_unknown"
    assert review_sources(data, char_limit=100) == []


def test_real_memory_path_read_retains_exact_observation(sandbox_env):
    from agent_core.core.flow_draft_review import MEMORY_GUIDANCE, review_messages
    from agent_core.core.workspace_memory import DIR_NAME, FILE_NAME
    from agent_core.tools.file_crud import read_file

    directory = sandbox_env / DIR_NAME
    directory.mkdir()
    body = "实际读取的工作区记载。"
    (directory / FILE_NAME).write_text(body, encoding="utf-8")
    observed = read_file(f"{DIR_NAME}/{FILE_NAME}")
    data = memory_read_record(f"{DIR_NAME}/{FILE_NAME}")
    data["messages"][2].update(content=str(observed),
                                _context_observation=extract_observation(observed))
    rows = review_sources(data, char_limit=100)
    assert rows[0]["text"] == body and rows[0]["workspace_memory_path"] is True
    assert MEMORY_GUIDANCE in review_messages("草稿", rows)[0]["content"]


@pytest.mark.parametrize("mutation", ["reader", "digest", "missing", "truncated", "failed_event"])
def test_read_file_prefix_requires_intact_observation_and_successful_event(sandbox_env, monkeypatch, mutation):
    from agent_core import config
    from agent_core.tools.file_crud import read_file
    from agent_core.tools.sandbox import truncate

    path = "failure.txt"
    (sandbox_env / path).write_text("[失败] 原始资料。", encoding="utf-8")
    observed = read_file(path)
    data = memory_read_record(path)
    result = data["messages"][2]
    result.update(content=str(observed), _context_observation=extract_observation(observed))
    if mutation == "reader":
        result["_context_observation"]["reader_tool"] = "rag_search"
    elif mutation == "digest":
        result["_context_observation"]["sha256"] = "a" * 64
    elif mutation == "missing":
        result.pop("_context_observation")
    elif mutation == "truncated":
        monkeypatch.setattr(config, "TOOL_MAX_OUTPUT", 4)
        result["content"] = truncate(result["content"])
    elif mutation == "failed_event":
        data["last_run"]["tools"][0].update(outcome="error", execution_progress=False)
    assert review_sources(data, char_limit=100) == []


def test_untrusted_marker_is_not_serialized():
    from agent_core.core.flow_draft_review import MEMORY_GUIDANCE, review_messages

    source = {"id": "S", "kind": "rag_search", "scope": "observed_span",
              "text": "原文。", "workspace_memory_path": True}
    messages = review_messages("草稿", [source])
    assert "workspace_memory_path" not in json.loads(messages[1]["content"])["sources"][0]
    assert MEMORY_GUIDANCE not in messages[0]["content"]


def test_managed_memory_entry_uses_host_path_spelling(monkeypatch):
    from pathlib import PurePosixPath, PureWindowsPath
    from agent_core.core import flow_review_sources

    path = r".CHANNELAGENT\MEMORY.JSON"
    monkeypatch.setattr(flow_review_sources, "PurePath", PureWindowsPath)
    assert flow_review_sources._workspace_memory_entry(path)
    assert not flow_review_sources._workspace_memory_entry(r"@other\.CHANNELAGENT\MEMORY.JSON")
    monkeypatch.setattr(flow_review_sources, "PurePath", PurePosixPath)
    assert not flow_review_sources._workspace_memory_entry(path)
    assert not flow_review_sources._workspace_memory_entry(".CHANNELAGENT/MEMORY.JSON")


def test_resumed_required_read_keeps_echo_restrictions_without_old_write_source():
    from agent_core.core.flow_required_reads import required_read_reference

    data, text = echo_record()
    original = next(row for row in review_sources(data, char_limit=4000) if row["kind"] == "read_file")
    call = data["messages"][3]["tool_calls"][0]
    result = data["messages"][4]
    restored = {"read": required_read_reference("read_file", 3, 4, result, call["function"]["arguments"])}
    data["messages"].append({"role": "user", "content": "继续"})
    data["last_run"] = {"turn_id": "resume", "input_index": 5, "tools": []}

    assert review_sources(data, char_limit=4000) == []
    assert review_sources(data, char_limit=4000, restored_reads=restored) == [original]
    assert review_sources(data, char_limit=len(text), restored_reads=restored) == []
    duplicate = copy.deepcopy(call)
    data["messages"].append({"role": "assistant", "tool_calls": [duplicate]})
    assert review_sources(data, char_limit=4000, restored_reads=restored) == []


@pytest.mark.parametrize("text", [
    "客户看到已截断提示", "正文含 [输出已截断]", "页面显示已截断）", chr(10) + "[stderr]" + chr(10) + "业务文本",
])
def test_custom_complete_json_preserves_truncation_words_as_evidence(text):
    output = {"items": [{"id": "REC-1", "text": text}]}
    rows = review_sources(selector_result({"limit": 2}, output), char_limit=1000)
    assert len(rows) == 1 and rows[0]["kind"] == "custom_result"
    assert json.loads(rows[0]["text"]) == output


@pytest.mark.parametrize("tail", ["[输出已截断]", "…（输出超过 20 字符，已截断）"])
def test_custom_truncation_outside_json_never_becomes_source(tail):
    data = selector_result({"limit": 2}, {"items": [{"id": "REC-1", "text": "原始资料"}]})
    message = data["messages"][2]
    message["content"] += chr(10) + tail
    assert review_sources(data, char_limit=1000) == []



@pytest.mark.parametrize("body", [
    '{"items":[{"id":"R1","text":"第一条","text":"第二条"}]}',
    '{"items":[{"id":"R1","text":"完整业务文本","nested":[NaN]}]}',
    '{"items":[{"id":"R1","text":"长度异常数据","number":1e999}]}',
])
def test_custom_unreliable_structured_value_cannot_become_a_source(body):
    data = selector_result({"limit": 2}, [])
    data["messages"][2]["content"] = chr(10).join(("[退出码] 0", "[stdout]", body))
    assert review_sources(data, char_limit=1000) == []
