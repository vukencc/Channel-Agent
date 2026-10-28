"""Fallback requests use the provider-visible input and try eligible routes."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agent_core import config
from agent_core.core import llm


@pytest.fixture
def fallback_routes(monkeypatch):
    monkeypatch.setattr(config, 'MAX_RETRIES', 1)
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [
        {'model': 'small', 'context_tokens': 8_000, 'response_reserve_tokens': 1_000},
        {'model': 'large', 'context_tokens': 128_000, 'response_reserve_tokens': 4_000},
    ])
    monkeypatch.setattr(llm, 'selected_schemas', lambda *_args: [])
    attempted = []

    class Stream:
        async def close(self):
            pass

    class Client:
        def __init__(self, model):
            self.model = model
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **_kwargs):
            return self

        async def create(self, **kwargs):
            attempted.append((self.model, kwargs))
            if self.model == config.MODEL:
                raise httpx.ConnectError('primary unavailable')
            return Stream()

    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: Client(endpoint['model']))
    return attempted


def test_fallback_capacity_ignores_private_provenance(fallback_routes, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [
        {'model': 'small', 'context_tokens': 8_000, 'response_reserve_tokens': 1_000},
    ])
    history = [{'role': 'user', 'content': 'short request',
                '_private_provenance': '中' * 50_000}]

    async def run():
        stream = await llm._open_stream(history)
        await stream.close()

    asyncio.run(run())
    assert [model for model, _ in fallback_routes] == [config.MODEL, 'small']
    assert all('_private_provenance' not in message
               for _, request in fallback_routes for message in request['messages'])


def test_undersized_fallback_is_skipped_for_later_eligible_route(fallback_routes):
    history = [{'role': 'user', 'content': '中' * 10_000}]

    async def run():
        stream = await llm._open_stream(history)
        await stream.close()

    asyncio.run(run())
    assert [model for model, _ in fallback_routes] == [config.MODEL, 'large']


@pytest.mark.parametrize('capacity, expected', [(None, ['primary']), (100000, ['primary', 'fallback'])])
def test_fallback_is_checked_before_sending_or_reserving(monkeypatch, capacity, expected):
    monkeypatch.setattr(config, 'MODEL', 'primary')
    monkeypatch.setattr(config, 'MAX_RETRIES', 1)
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {})
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [{'model': 'fallback', **({'context_tokens': capacity} if capacity else {})}])
    monkeypatch.setattr(llm, 'TOOL_SCHEMAS', [])
    sent = []

    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            sent.append(kwargs['model'])
            if kwargs['model'] == 'primary':
                raise httpx.ConnectError('temporary connection error')
            return 'stream'

    monkeypatch.setattr(llm, 'endpoint_client', Client)

    async def run():
        history = [{'role': 'user', 'content': 'a' * 80000}]
        if capacity is None:
            with pytest.raises(ValueError, match='备用模型.*上下文'):
                await llm._open_stream(history)
        else:
            assert await llm._open_stream(history) == 'stream'

    asyncio.run(run())
    assert sent == expected


def test_declared_output_reserve_bounds_actual_request(monkeypatch):
    from agent_core.core.session_limits import limits_scope
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 100000)
    monkeypatch.setattr(config, 'MODEL_RESPONSE_RESERVE_TOKENS', 8192)
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {})
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
    monkeypatch.setattr(llm, 'TOOL_SCHEMAS', [])
    sent = []

    class Client:
        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            sent.append(kwargs)
            return 'stream'

    client = Client()
    client.chat = SimpleNamespace(completions=client)
    monkeypatch.setattr(llm, 'endpoint_client', lambda _: client)
    with limits_scope({}):
        asyncio.run(llm._open_stream([{'role': 'user', 'content': 'hello'}]))
    assert sent[0]['max_tokens'] == 8192
