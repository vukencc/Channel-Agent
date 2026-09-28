import copy
import json

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.context import build_model_history, history_size


def test_compacts_only_old_file_payloads_without_mutating_transcript():
    messages = [{'role':'system','content':'system'}, {'role':'user','content':'create'},
        {'role':'assistant','content':'','tool_calls':[{'id':'a','function':{
            'name':'create_file','arguments':json.dumps({'path':'a.txt','content':'x'*22000})}}]},
        {'role':'tool','tool_call_id':'a','content':'[完成]'},
        {'role':'user','content':'change'},
        {'role':'assistant','content':'','tool_calls':[{'id':'b','function':{
            'name':'read_file','arguments':'{"path":"a.txt"}'}}]},
        {'role':'tool','tool_call_id':'b','content':'current page'}]
    original = copy.deepcopy(messages)
    history, metrics = build_model_history(messages)
    assert messages == original
    assert metrics['sent_chars'] < metrics['original_chars'] - 20000
    assert history[-1]['content'] == 'current page'
    assert history[2]['tool_calls'][0]['id'] == history[3]['tool_call_id']
    assert '历史片段已省略' in history[2]['tool_calls'][0]['function']['arguments']


def test_budget_evicts_whole_old_turns_preserving_current_user_and_tools(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1200)
    messages = [{'role':'system','content':'system'}, {'role':'user','content':'old'},
        {'role':'assistant','content':'x'*1400}, {'role':'user','content':'current'},
        {'role':'assistant','content':'','tool_calls':[{'id':'a','function':{'name':'read_file','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'a','content':'data'}]
    history, metrics = build_model_history(messages)
    assert metrics['omitted_turns'] == 1
    assert history_size(history) <= 1200
    assert history[1]['content'] == 'current'
    assert history[-1]['tool_call_id'] == history[-2]['tool_calls'][0]['id']
    assert '完整原始对话' in history[0]['content']


def test_oversized_current_user_never_silently_truncated(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1000)
    with pytest.raises(ValueError, match='完整记录未删除'):
        build_model_history([{'role':'system','content':'s'}, {'role':'user','content':'x'*1100}])


def test_completed_writes_in_current_turn_are_compacted_but_recent_work_is_kept():
    messages = [{'role':'system','content':'system'}, {'role':'user','content':'large edit'}]
    for i in range(5):
        messages += [{'role':'assistant','content':'','tool_calls':[{'id':str(i),'function':{
            'name':'edit_file','arguments':json.dumps({'path':'a','old_text':'anchor','new_text':'x'*3000})}}]},
            {'role':'tool','tool_call_id':str(i),'content':'[完成]'}]
    history, metrics = build_model_history(messages)
    assert metrics['compacted_file_parts'] == 3
    assert len(json.loads(history[-2]['tool_calls'][0]['function']['arguments'])['new_text']) == 3000
    assert len(json.loads(messages[2]['tool_calls'][0]['function']['arguments'])['new_text']) == 3000


def test_long_completed_command_scripts_do_not_accumulate_in_context():
    messages = [{'role':'system','content':'s'}, {'role':'user','content':'build files'}]
    for i in range(15):
        messages += [{'role':'assistant','content':'','tool_calls':[{'id':str(i),'function':{
            'name':'run_command','arguments':json.dumps({'command':'echo ' + 'x'*5000})}}]},
            {'role':'tool','tool_call_id':str(i),'content':'[退出码] 0'}]
    history, metrics = build_model_history(messages)
    assert metrics['sent_chars'] < 20000
    assert metrics['omitted_turns'] == 0
    assert len(history) == len(messages)


def test_many_file_pages_fit_without_discarding_current_user_or_recent_pages(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 20000)
    messages = [{'role':'system','content':'s'}, {'role':'user','content':'inspect large file'}]
    for i in range(12):
        messages += [{'role':'assistant','content':'','tool_calls':[{'id':str(i),'function':{
            'name':'read_file','arguments':json.dumps({'path':'a','offset':i*4000})}}]},
            {'role':'tool','tool_call_id':str(i),'content':str(i) + 'x'*4000}]
    history, metrics = build_model_history(messages)
    assert metrics['sent_chars'] <= 20000
    assert metrics['omitted_turns'] == 0
    assert history[1] == messages[1]
    assert history[-4:] == messages[-4:]
    assert len(history) == len(messages)
