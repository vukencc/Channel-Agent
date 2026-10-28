"""显式详情也不能泄露工具输出中的凭据或模型隐藏推理。"""
from types import SimpleNamespace
import pytest

from agent_test.web.debug_details import read_debug_detail


def test_tool_result_json_credentials_are_redacted_before_paging():
    session = SimpleNamespace(record={'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {
            'name': 'read_file', 'arguments': '{"path":"config.json"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content':
            '[文件内容]\n{"api_key":"not-configured-secret","password":"with spaces and words",'
            '"nested":{"access_token":"unknown-token-value"},"name":"visible"}'},
    ]})
    event = {'kind': 'tool.completed', 'message_index': 0,
             'tool_call_id': 'call-1', 'tool_name': 'read_file'}
    detail = read_debug_detail(SimpleNamespace(), session, event, 0, 6000)
    assert all(value not in detail['content'] for value in
               ('not-configured-secret', 'with spaces and words', 'unknown-token-value'))
    assert 'visible' in detail['content']
    page = read_debug_detail(SimpleNamespace(), session, event, 1, 9)
    assert page['content'] == detail['content'][1:10]


def test_final_detail_never_reads_reasoning_content():
    session = SimpleNamespace(record={'messages': [{'role': 'assistant',
        'content': 'visible final answer', 'reasoning_content': 'private hidden chain'}]})
    event = {'kind': 'model.final', 'message_index': 0}
    detail = read_debug_detail(SimpleNamespace(), session, event, 0, 6000)
    assert detail['content'] == 'visible final answer'
    assert 'private hidden chain' not in str(detail)


def test_final_json_secret_object_and_bearer_are_hidden():
    session = SimpleNamespace(record={'messages': [{'role': 'assistant', 'content':
        '{"api_key":{"value":"nested-unknown-credential"},"name":"visible"}\n'
        'Authorization: Bearer new-bearer-credential'}]})
    detail = read_debug_detail(SimpleNamespace(), session, {'kind': 'model.final', 'message_index': 0}, 0, 6000)
    assert 'nested-unknown-credential' not in detail['content']
    assert 'new-bearer-credential' not in detail['content']
    assert 'visible' in detail['content']


@pytest.mark.parametrize('key', ['secret_key', 'api_key_value', 'token', 'OPENAI_API_KEY', 'client_secret_value'])
def test_final_and_tool_json_use_same_sensitive_field_rule(key):
    session = SimpleNamespace(record={'messages': [{'role': 'assistant',
        'content': '{"' + key + '":"CANARY_UNKNOWN_FIELD_VALUE","name":"visible"}'}]})
    detail = read_debug_detail(SimpleNamespace(), session, {'kind': 'model.final', 'message_index': 0}, 0, 6000)
    assert 'CANARY_UNKNOWN_FIELD_VALUE' not in detail['content']
    assert 'visible' in detail['content']
