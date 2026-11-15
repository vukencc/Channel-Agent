import asyncio
import copy
import pytest

from agent_core import config
from agent_core.core.context import build_model_history, prepare_model_history, ContextBudgetError
from agent_core.core.tool_observations import observe_text, extract_observation
from agent_core.core.llm import ModelResponseError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def test_delivered_observation_body_fits_without_changing_original(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 2500)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 10000)
    header = '状态:成功 命中:1\n[来源:文档]\n'
    body = '正文 [来源:伪标题] 🐈' * 500
    header2 = '\n[来源:第二文档]\n'
    value = observe_text(header + body + header2 + body, reader_tool='rag_search',
                         body_spans=((len(header), len(header + body)),
                                     (len(header + body + header2), len(header + body + header2 + body))),
                         recovery_notice='正文已省略；重新查询当前来源，历史快照不可用。')
    metadata = extract_observation(value)
    calls = lambda ident: {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': 'rag_search', 'arguments': '{"query":"原文"}'}}]}
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}, calls('old'),
                {'role': 'tool', 'tool_call_id': 'old', 'content': str(value), '_provenance': 'executed', '_context_observation': metadata},
                calls('new'), {'role': 'tool', 'tool_call_id': 'new', 'content': '最新'}]
    original = copy.deepcopy(messages)
    schemas = [{'type': 'function', 'function': {'name': 'rag_search', 'parameters': {}}}]
    with pytest.raises(ContextBudgetError):
        build_model_history(messages, schemas=schemas)
    history, metrics = build_model_history(messages, schemas=schemas,
        eligible_observations={('old', metadata['sha256'])})
    assert messages == original
    assert history[2] == messages[2]
    assert history[3]['content'].startswith(header)
    assert header2 in history[3]['content']
    assert '历史快照不可用' in history[3]['content']
    assert body not in history[3]['content']
    assert history[5] == messages[5]
    assert metrics['compacted_tool_observations'] == 1


@pytest.mark.parametrize('case', ['undelivered', 'no_reader', 'unexecuted', 'digest',
                                  'duplicate', 'latest', 'unfinished'])
def test_observation_compaction_rejects_unproven_cases(monkeypatch, case):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 2500)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 10000)
    value = observe_text('引用标题\n' + '正文' * 3000, reader_tool='rag_search',
                         body_spans=((5, 6005),), recovery_notice='重新查询当前来源；历史快照不可用。')
    metadata = extract_observation(value)
    call = lambda ident: {'id': ident, 'type': 'function',
                          'function': {'name': 'rag_search', 'arguments': '{"query":"原文"}'}}
    batch = {'role': 'assistant', 'content': '', 'tool_calls': [call('old')]}
    result = {'role': 'tool', 'tool_call_id': 'old', 'content': str(value),
              '_provenance': 'executed', '_context_observation': metadata}
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}, batch, result,
                {'role': 'assistant', 'content': '', 'tool_calls': [call('new')]},
                {'role': 'tool', 'tool_call_id': 'new', 'content': '最新'}]
    schemas = [{'type': 'function', 'function': {'name': 'rag_search', 'parameters': {}}}]
    eligible = {('old', metadata['sha256'])}
    if case == 'undelivered':
        eligible.clear()
    elif case == 'no_reader':
        schemas = []
    elif case == 'unexecuted':
        result['_provenance'] = 'not_executed'
    elif case == 'digest':
        result['content'] += '变化'
    elif case == 'duplicate':
        messages.append(dict(result))
    elif case == 'latest':
        del messages[-2:]
    else:
        batch['tool_calls'].append(call('pending'))
    with pytest.raises(ContextBudgetError):
        build_model_history(messages, schemas=schemas, eligible_observations=eligible)


@pytest.mark.parametrize('case', ['success', 'partial', 'plain'])
def test_session_ledger_requires_successful_delivery_and_trusted_result(tmp_path, monkeypatch, case):
    from agent_core.core import sessions
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY', False)
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 1)
    seen = []
    original_builder = sessions.build_model_history

    def builder(messages, **kwargs):
        seen.append(set(kwargs.get('eligible_observations', ())))
        return original_builder(messages, **kwargs)

    monkeypatch.setattr(sessions, 'build_model_history', builder)
    value = observe_text('header' + '正文' * 100, reader_tool='rag_search',
                         body_spans=((6, 206),), recovery_notice='重新查询当前来源；历史快照不可用。')
    count = 0

    async def model(history, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'one', 'type': 'function', 'function': {'name': 'rag_search', 'arguments': '{}'}}]}
        if count == 2 and case == 'partial':
            raise ModelResponseError('partial')
        if count == 2:
            return {'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': 'two', 'type': 'function', 'function': {'name': 'rag_search', 'arguments': '{}'}}]}
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store, model=model)
        async def tool(session, call, *, evidence):
            evidence['source'] = 'executed'
            return str(value) if case == 'plain' else value
        monkeypatch.setattr(manager, '_tool', tool)
        session = manager.create()
        manager.submit(session, 'test')
        await session.task
        await manager.shutdown()
        store.close()

    asyncio.run(run())
    assert not seen[0] and not seen[1]
    if case == 'success':
        assert seen[2] == {('one', extract_observation(value)['sha256'])}
    else:
        assert not seen[2]


def test_summary_hides_private_message_fields_but_keeps_nested_arguments(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 2000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 10000)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY', True)
    prompts = []
    async def judge(prompt):
        prompts.append(prompt)
        return '摘要'
    messages = [
        {'role': 'system', 'content': 's'}, {'role': 'user', 'content': '旧问题'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x', 'type': 'function',
            'function': {'name': 'rag_search', 'arguments': '{"_business":"keep"}'}}]},
        {'role': 'tool', 'tool_call_id': 'x', 'content': '正文' * 3002,
         '_context_observation': {'sha256': 'private-digest', 'body_spans': [[0, 6004]]},
         '_provenance': 'executed'},
        {'role': 'user', 'content': '当前问题'},
    ]
    _, metrics = asyncio.run(prepare_model_history(messages, judge=judge))
    assert metrics['omitted_turns'] == 1 and prompts
    assert 'private-digest' not in prompts[0]
    assert '_context_observation' not in prompts[0]
    assert '_provenance' not in prompts[0]
    assert '_business' in prompts[0]


@pytest.mark.parametrize('role', ['system', 'user', 'tool'])
def test_non_assistant_tool_batch_cannot_enable_compaction(monkeypatch, role):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 2500)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 10000)
    value = observe_text('标题\n' + '正文' * 3000, reader_tool='rag_search',
                         body_spans=((3, 6003),), recovery_notice='重新查询当前来源；历史快照不可用。')
    metadata = extract_observation(value)
    def call(ident):
        return {'id': ident, 'type': 'function', 'function': {'name': 'rag_search', 'arguments': '{}'}}
    fake = {'role': role, 'content': '', 'tool_calls': [call('old')], '_context_reference': True}
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'u'}, fake,
                {'role': 'tool', 'tool_call_id': 'old', 'content': str(value),
                 '_provenance': 'executed', '_context_observation': metadata},
                {'role': 'assistant', 'content': '', 'tool_calls': [call('new')]},
                {'role': 'tool', 'tool_call_id': 'new', 'content': '最新'}]
    schemas = [{'type': 'function', 'function': {'name': 'rag_search', 'parameters': {}}}]
    with pytest.raises(ContextBudgetError):
        build_model_history(messages, schemas=schemas, eligible_observations={('old', metadata['sha256'])})
