import asyncio
from types import SimpleNamespace

import httpx
import openai
import config
from core import llm
from dev.tests.test_llm_stream import FakeStream, chunk


def test_primary_transient_failure_switches_model(monkeypatch):
    monkeypatch.setattr(config, 'MAX_RETRIES', 1)
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [{'model': 'backup'}], raising=False)
    seen = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            seen.append(kwargs['model'])
            if len(seen) == 1:
                raise openai.APIConnectionError(request=httpx.Request('POST', 'https://example.org'))
            return FakeStream([chunk(content='backup answer', finish='stop')])
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    events = []
    result = asyncio.run(llm.call_model([], emit=lambda *event: events.append(event)))
    assert result['content'] == 'backup answer'
    assert seen == [config.MODEL, 'backup']
    metrics = events[-1][1]
    assert metrics['model'] == 'backup'
    assert metrics['fallbacks']
    assert metrics['input_tokens'] >= 1 and metrics['output_tokens'] > 0
    assert 'estimated_cost_usd' in metrics


def test_provider_usage_is_preserved(monkeypatch):
    usage = SimpleNamespace(prompt_tokens=123, completion_tokens=7)
    async def open_stream(*args):
        return FakeStream([chunk(content='ok', finish='stop'), SimpleNamespace(choices=[], usage=usage)])
    monkeypatch.setattr(llm, '_open_stream', open_stream)
    monkeypatch.setattr(config, 'INPUT_COST_PER_MILLION', 1)
    monkeypatch.setattr(config, 'OUTPUT_COST_PER_MILLION', 2)
    events = []
    asyncio.run(llm.call_model([], emit=lambda *event: events.append(event)))
    metrics = events[-1][1]
    assert metrics['input_tokens'] == 123 and metrics['output_tokens'] == 7
    assert metrics['tokens_source'] == 'provider'
    assert metrics['estimated_cost_usd'] == 137 / 1e6
