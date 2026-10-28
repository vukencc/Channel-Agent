import copy
import json

import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError, build_model_history


def _append_call(call_id, content, expected_chars):
    return {'id': call_id, 'function': {'name': 'append_file', 'arguments': json.dumps({
        'path': 'long.txt', 'content': content, 'expected_chars': expected_chars,
    }, ensure_ascii=False)}}


def _history():
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'save all parts'}]
    for index in range(2):
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [_append_call(str(index), '中' * 4000, index * 4000)]},
            {'role': 'tool', 'tool_call_id': str(index), 'content': f'[完成] next_offset={(index + 1) * 4000}'},
        ])
    return messages


@pytest.fixture
def legacy_budget(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 16000)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 64000)


def test_added_schema_uses_headroom_compaction_only_when_needed(legacy_budget):
    messages = _history()
    history, metrics = build_model_history(messages)
    assert history == messages
    assert metrics['compacted_file_parts'] == 0

    history, metrics = build_model_history(messages, schemas=[{'description': '中' * 9000}])

    arguments = json.loads(history[2]['tool_calls'][0]['function']['arguments'])
    assert '历史片段已省略4000字' in arguments['content']
    assert arguments['path'] == 'long.txt'
    assert arguments['expected_chars'] == 0
    assert metrics['compacted_file_parts'] == 1
    assert metrics['omitted_turns'] == 0
    assert metrics['sent_tokens'] <= 16000
    assert metrics['total_chars'] <= 64000


def test_headroom_compaction_preserves_latest_pair_receipts_and_durable_history(legacy_budget):
    messages = _history()
    original = copy.deepcopy(messages)

    history, _ = build_model_history(messages, schemas=[{'description': '中' * 9000}])

    assert history[-2:] == original[-2:]
    assert len(history) == len(original)
    assert [message for message in history if message['role'] == 'tool'] == [
        message for message in original if message['role'] == 'tool']
    assert [message['tool_calls'][0]['id'] for message in history if 'tool_calls' in message] == [
        message['tool_call_id'] for message in history if message['role'] == 'tool']
    assert messages == original


def test_headroom_compaction_preserves_pending_call_arguments(legacy_budget):
    messages = _history()
    messages[2]['tool_calls'].append(_append_call('pending', '待' * 2100, 8000))
    messages[4]['tool_calls'][0] = _append_call('1', '中' * 2100, 4000)
    original = copy.deepcopy(messages)

    history, metrics = build_model_history(messages, schemas=[{'description': '中' * 9000}])

    assert metrics['compacted_file_parts'] == 1
    assert history[2]['tool_calls'][1] == original[2]['tool_calls'][1]
    assert history[-2:] == original[-2:]
    assert messages == original


def test_fixed_prefix_without_headroom_still_fails(legacy_budget, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', True)
    messages = _history()
    messages[0]['content'] = '前' * 16000
    original = copy.deepcopy(messages)

    with pytest.raises(ContextBudgetError) as error:
        build_model_history(messages, schemas=[{'description': 'schema'}])

    assert error.value.metrics['sent_tokens'] > 16000
    assert messages == original
