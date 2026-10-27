"""RAG assessment honors resolved model input budgets."""

import asyncio

from agent_core import config
from agent_core.core.session_limits import limits_scope
from agent_core.rag.assess import assess_rag


def test_auto_input_budget_calls_judge(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(config, 'MODEL_RESPONSE_RESERVE_TOKENS', 512)
    seen = []

    async def judge(prompt: str) -> str:
        seen.append(prompt)
        return '  评估完成  '

    result = asyncio.run(assess_rag('问题', ['相关片段'], '答案', judge))

    assert result == '评估完成'
    assert len(seen) == 1
    assert '相关片段' in seen[0]


def test_explicit_small_session_budget_degrades_without_judge(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 4096)
    monkeypatch.setattr(config, 'MODEL_RESPONSE_RESERVE_TOKENS', 512)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    seen = []

    async def judge(prompt: str) -> str:
        seen.append(prompt)
        return '评估完成'

    with limits_scope({'budget_overrides': {'MODEL_INPUT_CHARS': 512, 'MODEL_INPUT_TOKENS': 128}}):
        result = asyncio.run(assess_rag('问题', ['相关片段'], '答案', judge))

    assert result.startswith('[评估降级]')
    assert seen == []
