import asyncio
import copy

import config
from core import context


def test_mixed_text_tokens_and_schema_budget(monkeypatch):
    assert context.estimate_tokens('abcd中文') == 3
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 30, raising=False)
    import pytest
    with pytest.raises(context.ContextBudgetError):
        context.build_model_history([{'role': 'system', 'content': '中' * 100}])


def test_evicted_turn_is_summarized_only_in_working_copy(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1800)
    messages = [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': '订单号 A17'},
                {'role': 'assistant', 'content': 'x' * 2000}, {'role': 'user', 'content': '继续'}]
    original = copy.deepcopy(messages)
    async def judge(prompt):
        assert 'A17' in prompt
        return '订单号 A17'
    history, metrics = asyncio.run(context.prepare_model_history(messages, judge=judge))
    assert '历史摘要' in history[0]['content'] and 'A17' in history[0]['content']
    assert metrics['summary'] == 'applied'
    assert metrics['total_chars'] == sum(metrics[key] for key in ('messages', 'schema', 'memory', 'extra'))
    assert metrics['sent_tokens'] < metrics['original_tokens']
    assert original == messages


def test_hundred_turn_budget_does_not_reserialize_history_per_message(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1200)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 1000)
    original = context.history_size
    calls = []
    def measured(messages):
        calls.append(len(messages))
        return original(messages)
    monkeypatch.setattr(context, 'history_size', measured)
    messages = [{'role': 'system', 'content': 's'}]
    for i in range(120):
        messages.extend([{'role': 'user', 'content': str(i)}, {'role': 'assistant', 'content': '中' * 200}])
    history, metrics = context.build_model_history(messages)
    assert metrics['omitted_turns'] > 100
    assert history[-1] == messages[-1]
    assert len(calls) < 15, '预算应增量计量，不能反复序列化整份历史'
