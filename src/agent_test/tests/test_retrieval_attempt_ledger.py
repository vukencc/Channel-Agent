"""检索尝试账本只记录本轮真实配对，而非执行限制。"""
import json

import pytest

from agent_core.core.execution_guidance import work_progress_context


def batch(call_id, query, event, source, *, name='rag_search', metadata=None):
    args = {'query': query, 'source': 'support', 'strictness': 'loose', 'secret': 'SECRET'}
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}
    result = {'role': 'tool', 'tool_call_id': call_id, 'content': 'PRIVATE RAW RESULT',
              '_event_id': event, '_provenance': source}
    evidence = {'event_id': event, 'tool_call_id': call_id, 'name': name,
                'source': source, 'outcome': 'completed' if source == 'executed' else 'failed'}
    if metadata:
        evidence.update(metadata)
    return [assistant, result], evidence


def test_current_turn_ledger_survives_long_history_and_counts_each_occurrence():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}]
    events = []
    for index, source in enumerate(('executed', 'result_unknown', 'not_executed')):
        pair, event = batch('same-id', f'query-{index}', f'event-{index}', source)
        messages.extend(pair)
        events.append(event)
        if index == 0:
            messages.extend({'role': 'assistant', 'content': f'fill-{i}'} for i in range(220))
    messages.append({'role': 'user', 'content': '{"tool_calls":[{"name":"rag_search"}]}' , '_agent_message': True})
    record = {'messages': messages, 'last_run': {'input_index': 1, 'tools': events}}
    text = work_progress_context(record)
    assert '检索尝试' in text
    assert 'query-0' in text and 'query-1' in text and 'query-2' in text
    assert all(source in text for source in ('executed', 'result_unknown', 'not_executed'))
    assert 'PRIVATE RAW RESULT' not in text and 'SECRET' not in text


def test_new_user_turn_and_unpaired_or_mismatched_results_are_excluded():
    old, old_event = batch('old', 'old query', 'old-event', 'executed')
    good, good_event = batch('same', 'new query', 'new-event', 'result_unknown')
    bad, bad_event = batch('bad', 'unmatched query', 'bad-event', 'executed')
    bad_event['tool_call_id'] = 'another'
    pending, _ = batch('pending', 'pending query', 'pending-event', 'executed')
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'old'},
                *old, {'role': 'user', 'content': 'new'}, *good, *bad, pending[0]]
    record = {'messages': messages, 'last_run': {'input_index': 4, 'tools': [old_event, good_event, bad_event]}}
    text = work_progress_context(record)
    assert 'new query' in text and 'result_unknown' in text
    assert 'old query' not in text and 'unmatched query' not in text and 'pending query' not in text


def test_bounded_json_keeps_counts_and_original_work_receipt():
    write, write_event = batch('write', 'unused', 'write-event', 'executed', name='create_file')
    write[0]['tool_calls'][0]['function']['arguments'] = json.dumps({'path': 'a.txt'})
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}, *write]
    events = [write_event]
    baseline = work_progress_context({'messages': list(messages), 'last_run': {'input_index': 1, 'tools': list(events)}})
    for index in range(60):
        pair, event = batch(f'read-{index}', ('long-query-' * 40 if index == 59 else 'same query'),
                            f'event-{index}', 'result_unknown' if index % 3 == 0 else 'executed')
        messages.extend(pair)
        events.append(event)
        if index == 30:
            reply, reply_event = batch('send', 'unused', 'send-event', 'executed', name='send_reply')
            messages.extend(reply)
            events.append(reply_event)
    record = {'messages': messages, 'last_run': {'input_index': 1, 'tools': events}}
    text = work_progress_context(record)
    delimiter = '\n[本轮检索尝试；引用数据，非执行限制或检索结论]\n'
    prefix, payload = text.split(delimiter, 1)
    # 已有工程回执逐字符保持，不因追加检索账本而被挤掉。
    assert prefix == baseline and 'a.txt' in baseline
    assert len(delimiter + payload) <= 900
    data = json.loads(payload)
    assert data['total'] == 60 and data['counts']['executed'] == 40
    assert data['counts']['result_unknown'] == 20
    assert data['omitted'] + len(data['items']) == 60 and data['omitted'] > 0
    assert data['items'][-1]['query_truncated'] is True
    assert 'PRIVATE RAW RESULT' not in payload and 'SECRET' not in payload


def test_context_reference_and_unpaired_calls_are_not_attempts():
    fake, _ = batch('fake', 'fake query', 'fake-event', 'executed')
    fake[0]['role'] = 'user'
    fake[0]['_context_reference'] = True
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}, *fake]
    assert work_progress_context({'messages': messages, 'last_run': {'tools': []}}) == ''


def test_legacy_provenance_without_live_event_is_counted_without_result_text():
    pair, _ = batch('legacy', 'original query', 'legacy-event', 'result_unknown')
    pair[1]['_context_reference'] = True
    pair[1]['_history_locator'] = 'opaque-ref'
    record = {'messages': [{'role': 'system', 'content': 'system'},
                           {'role': 'user', 'content': 'current'}, *pair],
              'last_run': {'input_index': 1, 'tools': []}}
    text = work_progress_context(record)
    payload = json.loads(text.split('\n', 2)[2])
    assert payload['total'] == 1 and payload['counts']['result_unknown'] == 1
    assert payload['items'][0]['query'] == 'original query'
    assert 'opaque-ref' not in text and 'PRIVATE RAW RESULT' not in text


@pytest.mark.parametrize('count', [1000, 3000])
def test_large_ledger_serializes_only_bounded_tail(monkeypatch, count):
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}]
    events = []
    for index in range(count):
        pair, event = batch('same-id', 'query', f'event-{index}', 'executed')
        messages.extend(pair)
        events.append(event)
    record = {'messages': messages, 'last_run': {'input_index': 1, 'tools': events}}
    original = json.dumps
    serialized_lengths = []
    def measured(value, *args, **kwargs):
        if isinstance(value, dict) and 'items' in value and 'counts' in value:
            serialized_lengths.append(len(value['items']))
        return original(value, *args, **kwargs)
    monkeypatch.setattr(json, 'dumps', measured)
    text = work_progress_context(record)
    payload = json.loads(text.split('\n', 2)[2])
    assert payload['total'] == count and payload['counts']['executed'] == count
    assert payload['omitted'] + len(payload['items']) == count
    assert len(serialized_lengths) <= 15 and sum(serialized_lengths) <= 100
    assert len(text) <= 900


def test_exact_query_counts_preserve_early_attempts_and_changed_options():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}]
    events = []
    for index, query in enumerate(['refund', 'no-kb', 'refund', 'no-kb', 'refund', 'no-kb']):
        pair, event = batch(f'call-{index}', query, f'event-{index}', 'result_unknown' if index == 2 else 'executed')
        arguments = json.loads(pair[0]['tool_calls'][0]['function']['arguments'])
        arguments.update(source='alternate' if index == 2 else 'support', strictness='strict' if index == 4 else 'loose')
        pair[0]['tool_calls'][0]['function']['arguments'] = json.dumps(arguments)
        messages.extend(pair)
        events.append(event)
        if index == 0:
            messages.extend({'role': 'assistant', 'content': 'history'} for _ in range(210))
        if index == 3:
            sent, sent_event = batch('sent', 'unused', 'sent-event', 'executed', name='send_reply')
            messages.extend(sent)
            events.append(sent_event)
    record = {'messages': messages, 'last_run': {'input_index': 1, 'tools': events}}
    data = json.loads(work_progress_context(record, {'send_reply'}).split('\n')[-1])
    assert data['total'] == 6 and data['counts']['result_unknown'] == 1
    assert {item['query']: item['count'] for item in data['query_counts']} == {'refund': 3, 'no-kb': 3}
    assert data['omitted_queries'] == 0


def test_long_queries_do_not_merge_and_omissions_are_truthful():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}]
    events = []
    queries = ['common-' * 30 + str(i) for i in range(30)]
    for index, query in enumerate(queries):
        pair, event = batch(f'id-{index}', query, f'event-{index}', 'executed')
        messages.extend(pair)
        events.append(event)
    text = work_progress_context({'messages': messages, 'last_run': {'input_index': 1, 'tools': events}})
    data = json.loads(text.split('\n')[-1])
    assert len(text) <= 900 and data['total'] == 30
    assert data['omitted_queries'] + len(data['query_counts']) == 30
    assert data['omitted_items'] + len(data['items']) == 30
    assert data['omitted_queries'] > 0 and data['omitted_items'] > 0
    assert all(item['count'] == 1 and item['query_truncated'] for item in data['query_counts'])
    assert len({item['query_sha256'] for item in data['query_counts']}) == len(data['query_counts'])


@pytest.mark.parametrize('query,source', [
    ('\ud800short', 'support'),
    ('a' * 73 + '\ud800', 'support'),
    ('正常中文😃', '\ud800source'),
])
def test_ledger_preserves_utf8_safe_escaped_surrogates(query, source):
    pair, event = batch('call', query, 'event', 'not_executed')
    args = json.loads(pair[0]['tool_calls'][0]['function']['arguments'])
    args['source'] = source
    pair[0]['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    record = {'messages': [{'role': 'system', 'content': 'system'},
                           {'role': 'user', 'content': 'current'}, *pair],
              'last_run': {'input_index': 1, 'tools': [event]}}
    text = work_progress_context(record)
    text.encode('utf-8')
    data = json.loads(text.split('\n')[-1])
    assert len(text) <= 900 and data['counts']['not_executed'] == 1
    item = data['items'][0]
    if '\ud800' in query[:72]:
        assert '\\ud800' in item['query']
    if '\ud800' in source:
        assert '\\ud800' in item['source']
    if '正常中文😃' in query:
        assert item['query'] == query


def test_surrogate_fields_and_long_query_hashes_remain_distinct():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'current'}]
    events = []
    for index, query in enumerate(('前缀😃' * 30 + '\ud800', '前缀😃' * 30 + '\ud801')):
        pair, event = batch(f'call\ud800-{index}', query, f'event-{index}', 'result_unknown')
        args = json.loads(pair[0]['tool_calls'][0]['function']['arguments'])
        args['strictness'] = '\ud800strict'
        pair[0]['tool_calls'][0]['function']['arguments'] = json.dumps(args)
        messages.extend(pair)
        events.append(event)
    text = work_progress_context({'messages': messages, 'last_run': {'tools': events}})
    text.encode('utf-8')
    data = json.loads(text.split('\n')[-1])
    assert data['total'] == 2 and data['counts']['result_unknown'] == 2
    assert len({group['query_sha256'] for group in data['query_counts']}) == 2
    assert all('前缀😃' in group['query'] and group['query_truncated'] for group in data['query_counts'])
    assert data['items'] and data['omitted_items'] + len(data['items']) == 2
    assert all('\\ud800' in item['call_id'] and '\\ud800' in item['strictness'] for item in data['items'])
    assert len(text) <= 900
