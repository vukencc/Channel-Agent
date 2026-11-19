from agent_core.core.flow_progress_review import review_candidate, parse_review, review_history
import asyncio
import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_review_requires_current_executed_reads_and_prior_success():
    messages = [{"role": "user", "content": "start"}]
    events = []
    for i, name in enumerate(["create_file", "read_file", "read_file", "read_file"]):
        call_id = str(i)
        messages.extend([{"role": "assistant", "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": call_id, "content": "ok", "_event_id": call_id, "_provenance": "executed"}])
        events.append({"event_id": call_id, "tool_call_id": call_id, "name": name, "source": "executed", "outcome": "completed"})
    record = {"flow_id": "f", "messages": messages, "last_run": {"tools": events}}
    assert review_candidate(record) == "0"
    # 任一调用缺少结果，整个工具批次都不能作为判断证据。
    messages[1]['tool_calls'].append({'id': 'missing', 'function': {'name': 'create_file', 'arguments': '{}'}})
    assert review_candidate(record) is None
    messages[1]['tool_calls'].pop()
    events[0]["source"] = "not_executed"
    assert review_candidate(record) is None


def test_existing_project_repeated_file_reads_trigger_without_a_write():
    messages = [{'role': 'user', 'content': 'review project'}]
    events = []
    for i, name in enumerate(['list_files', 'read_file', 'list_files', 'read_file', 'list_files', 'read_file']):
        ident = str(i)
        messages.extend([{'role': 'assistant', 'tool_calls': [{'id': ident, 'function': {'name': name, 'arguments': '{"path":"a"}'}}]},
                         {'role': 'tool', 'tool_call_id': ident, 'content': 'file content' if name == 'read_file' else 'a', '_event_id': ident, '_provenance': 'executed'}])
        events.append({'event_id': ident, 'tool_call_id': ident, 'name': name, 'source': 'executed', 'outcome': 'completed'})
    record = {'flow_id': 'f', 'messages': messages, 'last_run': {'tools': events}}
    candidate = review_candidate(record)
    assert candidate and candidate == review_candidate(record)
    for message in messages:
        if message.get('role') == 'tool' and message['content'] == 'file content':
            message['content'] = 'changed content'
    assert review_candidate(record) != candidate
    for message in messages:
        if message.get('role') == 'tool' and message['content'] == 'changed content':
            message['content'] = ''
    assert review_candidate(record) is None
    for message in messages:
        if message.get('role') == 'tool' and message['content'] == '':
            message['content'] = '[文件分页] path=a offset=0 chars=0 EOF\n[内容开始]\n\n[内容结束]'
    assert review_candidate(record) is None
    for index, message in enumerate(messages):
        if message.get('role') == 'assistant' and message['tool_calls'][0]['function']['name'] == 'read_file':
            message['tool_calls'][0]['function']['arguments'] = '{"path":"a","offset":' + str(index) + '}'
            messages[index + 1]['content'] = 'different page'
    assert review_candidate(record) is None


def test_review_rejects_lossy_numbers_in_structured_output():
    contract = {'fields': {'n': {'type': 'number'}}}
    assert parse_review('{"decision":"handoff","output":{"n":1.00000000000000001}}', contract) is None
    assert parse_review('{"decision":"handoff","output":{"n":1.5}}', contract) == ('handoff', '{"n": 1.5}')
    chinese = '字' * 12000
    assert parse_review('{"decision":"handoff","output":{"text":"' + chinese + '"}}')[1] == '{"text": "' + chinese + '"}'


def test_review_protocol_requires_explicit_valid_decision():
    assert parse_review("not json") is None
    assert parse_review("{\"decision\":\"handoff\",\"output\":\"done\"}") == ("handoff", "done")
    assert parse_review("{\"decision\":\"continue\",\"output\":\"check tests\"}") == ("continue", "check tests")
    assert parse_review("{\"decision\":\"handoff\"}") is None
    assert parse_review('{"decision":"handoff","output":{"state":"ok"}}') == ('handoff', '{"state": "ok"}')
    assert parse_review('{"decision":"continue","decision":"handoff","output":"x"}') is None
    assert parse_review('{"decision":[],"output":"x"}') is None
    assert parse_review('{"decision":"handoff","output":"\\ud800"}') is None
    assert parse_review('{"decision":"handoff","output":"' + 'x' * 65536 + '"}') is None


def test_structured_business_object_is_accepted_only_under_contract():
    contract = {'fields': {'state': {'type': 'string', 'enum': ['ok']}}}
    assert parse_review('{"state":"ok"}', contract) == ('handoff', '{"state":"ok"}')
    assert parse_review('{"state":"bad"}', contract) is None
    assert parse_review('{"state":"ok","decision":"continue"}', contract) is None
    assert parse_review('{"state":"ok","state":"bad"}', contract) is None


def test_review_instruction_is_last_high_priority_message():
    history = review_history([{'role': 'system', 'content': '业务对象无包装'}], {}, 10000, 10000)
    assert history[-1]['role'] == 'system'
    assert history[-1]['_context_reference'] is True
    assert 'output' in history[-1]['content']


@pytest.mark.parametrize('answer,expected', [
    ({'role': 'assistant', 'content': '{"decision":"handoff","output":"done"}'}, 'idle'),
    ({'role': 'assistant', 'content': '{"decision":"continue","output":"check tests"}'}, 'idle'),
    ({'role': 'assistant', 'content': 'invalid'}, 'checkpoint'),
    ({'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'bad', 'function': {'name': 'create_file', 'arguments': '{}'}}]}, 'checkpoint'),
])
def test_review_consumes_model_round_and_never_executes_review_tools(tmp_path, monkeypatch, sandbox_env, answer, expected):
    from agent_core.core import flow_progress_review
    monkeypatch.setattr(flow_progress_review, 'review_candidate', lambda record: 'receipt')
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'WORKSPACE_MEMORY_ENABLED', 'ENABLE_STRUCTURED_CONTEXT', 'AUTO_CONTINUE_TASKS'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, 'MAX_TOOL_ROUNDS', 3)
    store = SessionStore(tmp_path / 'state', workspace_root=sandbox_env)

    async def run():
        manager = SessionManager(store)
        seen = []
        async def model(history, *, disable_tools=False, **kwargs):
            seen.append((disable_tools, history))
            return answer if len(seen) == 1 else {'role': 'assistant', 'content': 'finished'}
        manager._call_model = model
        try:
            session = manager.create()
            manager.submit(session, 'work')
            await asyncio.wait_for(session.task, 5)
            assert session.record['status'] == expected
            assert seen[0][0] is True
            assert any(flow_progress_review.REVIEW_INSTRUCTION in (m.get('content') or '') for m in seen[0][1])
            if 'continue' in answer.get('content', ''):
                assert len(seen) == 2 and seen[1][0] is False
            else:
                assert len(seen) == 1
            if answer.get('tool_calls'):
                assert session.record['messages'][-2]['_provenance'] == 'not_executed'
                assert session.record['last_run']['tools'][0]['source'] == 'not_executed'
            else:
                assert not session.record['last_run']['tools']
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    finally:
        store.close()
