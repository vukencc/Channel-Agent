"""Model-specific context windows bound each request without leaking between sessions."""

import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.context import build_model_history, estimate_tokens, prepare_model_history
from agent_core.core.history_context import HistoryContext
from agent_core.core.model_settings import active_endpoint, model_profile
from agent_core.core.runtime_models import RuntimeModelRegistry
from agent_core.core.session_limits import (
    context_budget_metadata, effective_limits, limit, limits_scope, validate_limits,
)
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


@pytest.fixture
def automatic_input_limits(monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', True)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 0)
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 0, raising=False)
    monkeypatch.setattr(config, 'MODEL_RESPONSE_RESERVE_TOKENS', 4096, raising=False)
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {})
    monkeypatch.setattr(config, 'MODEL_PROFILES', {})


def test_known_profile_uses_declared_capacity_and_output_headroom(automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {
        'large': {'model': 'large-model', 'context_tokens': 1_000_000,
                  'response_reserve_tokens': 8_000, 'parameters': {'max_tokens': 12_000}},
    })
    with model_profile('large'):
        limits = effective_limits({'model_profile': 'large'})
        assert limits['MODEL_INPUT_TOKENS'] == 988_000
        assert limits['MODEL_INPUT_CHARS'] == 3_952_000


def test_explicit_global_and_session_caps_never_exceed_model_capacity(automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 16_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 64_000)
    monkeypatch.setattr(config, 'MODEL_PROFILES', {
        'small': {'model': 'small-model', 'context_tokens': 8_000,
                  'response_reserve_tokens': 2_000},
    })
    record = {'model_profile': 'small', 'budget_overrides': {
        'MODEL_INPUT_TOKENS': 20_000, 'MODEL_INPUT_CHARS': 100_000}}
    with model_profile('small'):
        limits = effective_limits(record)
        assert limits['MODEL_INPUT_TOKENS'] == 6_000
        assert limits['MODEL_INPUT_CHARS'] <= 24_000
    assert effective_limits({})['MODEL_INPUT_TOKENS'] == 16_000


def test_unknown_custom_model_uses_conservative_legacy_window(automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 1_000_000)
    unknown = {'model_profile': 'unlisted'}
    with model_profile('unlisted'):
        limits = effective_limits(unknown)
        assert limits['MODEL_INPUT_TOKENS'] == 16_000
        assert limits['MODEL_INPUT_CHARS'] == 64_000


def test_unknown_runtime_model_does_not_inherit_default_capacity(
        tmp_path, automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_CONTEXT_TOKENS', 1_000_000)
    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(name='unknown', model='other', base_url='https://example.test/v1')
    endpoint = registry.resolve(public['id'])
    limits = effective_limits({'model_profile': public['id']}, endpoint=endpoint)
    assert limits['MODEL_INPUT_TOKENS'] == 16_000
    assert limits['MODEL_INPUT_CHARS'] == 64_000


def test_parallel_capacity_scopes_are_isolated(automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {
        'small': {'model': 'small', 'context_tokens': 32_000,
                  'response_reserve_tokens': 4_000},
        'large': {'model': 'large', 'context_tokens': 128_000,
                  'response_reserve_tokens': 8_000},
    })

    async def inspect(profile):
        with model_profile(profile), limits_scope({'model_profile': profile}):
            before = limit('MODEL_INPUT_TOKENS')
            await asyncio.sleep(0)
            return before, limit('MODEL_INPUT_TOKENS')

    async def run():
        return await asyncio.gather(inspect('small'), inspect('large'))

    small, large = asyncio.run(run())
    assert small == (28_000, 28_000)
    assert large == (120_000, 120_000)


def test_session_limits_accept_million_token_cap_and_reject_invalid_values(automatic_input_limits):
    assert validate_limits({'MODEL_INPUT_TOKENS': 1_000_000})['MODEL_INPUT_TOKENS'] == 1_000_000
    for value in (-1, 0, True, 1.5):
        with pytest.raises(ValueError):
            validate_limits({'MODEL_INPUT_TOKENS': value})


def test_runtime_capacity_persists_and_validates_bounds(tmp_path, automatic_input_limits):
    root = tmp_path / 'private-state'
    registry = RuntimeModelRegistry(root)
    entry = registry.add(name='large', model='custom', base_url='https://example.test/v1',
                         context_tokens=1_000_000, response_reserve_tokens=8_192)
    restored = RuntimeModelRegistry(root).resolve(entry['id'])
    assert restored.context_tokens == 1_000_000
    assert restored.response_reserve_tokens == 8_192
    assert effective_limits({'model_profile': entry['id']}, endpoint=restored)['MODEL_INPUT_TOKENS'] == 991_808

    registry.update(entry['id'], name='renamed', model='custom',
                    base_url='https://example.test/v1')
    retained = RuntimeModelRegistry(root).resolve(entry['id'])
    assert retained.context_tokens == 1_000_000
    assert retained.response_reserve_tokens == 8_192

    for capacity, reserve in ((0, 0), (-1, 0), (True, 0), (10_000, -1), (10_000, 10_000)):
        with pytest.raises(ValueError):
            registry.add(name='invalid', model='custom', base_url='https://example.test/v1',
                         context_tokens=capacity, response_reserve_tokens=reserve)
    assert RuntimeModelRegistry(root).ids() == [entry['id']]


def test_missing_runtime_model_ends_with_error_instead_of_queued(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        try:
            session = manager.create()
            session.record['model_profile'] = 'runtime-' + '0' * 32
            manager.submit(session, 'hello')
            await asyncio.gather(session.task, return_exceptions=True)
            await manager.flush()
            assert session.record['status'] == 'error'
            assert session.record['error']
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_web_model_capacity_roundtrip_and_invalid_reserve(tmp_path, automatic_input_limits):
    store = SessionStore(tmp_path / 'private-state', tmp_path / 'workspace')
    payload = {'name': 'Web model', 'model': 'custom', 'base_url': 'https://example.test/v1',
               'api_key': 'private-key', 'context_tokens': 128_000,
               'response_reserve_tokens': 8_000, 'parameters': {'max_tokens': 4_000}}
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            created = client.post('/api/models', json=payload)
            assert created.status_code == 201
            identifier = created.json()['id']
            assert created.json()['context_tokens'] == 128_000
            assert created.json()['response_reserve_tokens'] == 8_000
            assert 'private-key' not in created.text

            edited = client.put('/api/models/' + identifier, json={
                **payload, 'api_key': '', 'context_tokens': 256_000,
                'response_reserve_tokens': 16_000})
            assert edited.status_code == 200
            assert edited.json()['context_tokens'] == 256_000
            assert edited.json()['response_reserve_tokens'] == 16_000
            listed = client.get('/api/models').json()['models']
            assert any(item['id'] == identifier and item['context_tokens'] == 256_000 for item in listed)
            invalid = client.post('/api/models', json={
                **payload, 'name': 'invalid', 'context_tokens': 8_000,
                'response_reserve_tokens': 8_000})
            assert invalid.status_code == 409
            assert 'private-key' not in invalid.text
        restored = RuntimeModelRegistry(store.root).resolve(identifier)
        assert restored.context_tokens == 256_000
        assert restored.response_reserve_tokens == 16_000
        assert restored.api_key == 'private-key'
    finally:
        store.close()


def test_capacity_metadata_is_not_sent_as_provider_options(tmp_path, automatic_input_limits):
    from agent_core.core import llm

    registry = RuntimeModelRegistry(tmp_path / 'state')
    public = registry.add(name='large', model='custom', base_url='https://example.test/v1',
                          context_tokens=1_000_000, response_reserve_tokens=8_000,
                          parameters={'temperature': .2})
    endpoint = registry.resolve(public['id'])
    with model_profile(public['id'], endpoint):
        assert llm.model_options() == {'temperature': .2}
    assert endpoint.options() == {'temperature': .2}


def test_large_profile_does_not_compact_small_input_at_legacy_limit(
        tmp_path, automatic_input_limits, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {
        'large': {'model': 'large', 'context_tokens': 1_000_000,
                  'response_reserve_tokens': 8_000},
    })
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def run():
        manager = SessionManager(store, model=lambda *_args, **_kwargs: None)
        try:
            session = manager.create()
            messages = [{'role': 'system', 'content': 'Keep the input.'},
                        {'role': 'user', 'content': 'a' * 100_000}]
            with model_profile('large'), limits_scope({'model_profile': 'large'}):
                history, metrics = await HistoryContext(manager).prepare(session, messages)
            assert metrics['input_limit_tokens'] == 992_000
            assert metrics['compacted'] is False
            assert metrics['utilization'] < .9
            assert history[-1]['content'].endswith('a' * 100_000)
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


def test_summary_metrics_match_final_working_history(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1_800)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY', True)
    messages = [{'role': 'system', 'content': 's'},
                {'role': 'user', 'content': 'Order A17'},
                {'role': 'assistant', 'content': 'x' * 2_000},
                {'role': 'user', 'content': 'Continue'}]
    original = copy.deepcopy(messages)

    async def judge(_prompt):
        return 'Order A17'

    history, metrics = asyncio.run(prepare_model_history(messages, judge=judge))
    assert metrics['summary'] == 'applied'
    rebuilt, expected = build_model_history(history)
    assert rebuilt == history
    for key in ('sent_chars', 'sent_tokens', 'total_chars', 'schema_tokens',
                'estimated_input_utilization', 'capacity_utilization', 'utilization'):
        assert metrics[key] == expected[key]
    assert messages == original


def test_runtime_snapshot_remains_bound_between_preparation_and_request(
        tmp_path, automatic_input_limits):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    seen = []

    async def model(_history, **_kwargs):
        endpoint = active_endpoint.get()
        seen.append((endpoint.model, endpoint.context_tokens,
                     context_budget_metadata()['context_capacity_tokens']))
        return {'role': 'assistant', 'content': 'done'}

    async def run():
        manager = SessionManager(store, model=model)
        try:
            public = manager.model_registry.add(name='first', model='model-a',
                base_url='https://example.test/v1', context_tokens=128_000,
                response_reserve_tokens=8_000)
            session = manager.create()
            manager.set_model_profile(session, public['id'])

            async def prepare_then_call(_session):
                # The outer turn scope has already captured the endpoint for context sizing.
                assert context_budget_metadata()['context_capacity_tokens'] == 128_000
                manager.model_registry.update(public['id'], name='second', model='model-b',
                    base_url='https://example.test/v1', context_tokens=256_000,
                    response_reserve_tokens=16_000)
                await manager._call_model([{'role': 'user', 'content': 'hello'}],
                                          session_id=session.id)

            manager._run = prepare_then_call
            await manager._run_with_budget(session)
            assert seen == [('model-a', 128_000, 128_000)]
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.mark.parametrize('structured', [False, True])
def test_context_metrics_include_schema_and_distinct_capacity_utilization(
        automatic_input_limits, monkeypatch, structured):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {
        'large': {'model': 'large', 'context_tokens': 1_000_000,
                  'response_reserve_tokens': 8_000},
    })
    messages = [{'role': 'system', 'content': 'Follow the tool schema.'},
                {'role': 'user', 'content': 'Answer briefly.'}]
    schemas = [{'type': 'function', 'function': {'name': 'inspect',
                'description': 'Inspect a fixture.', 'parameters': {'type': 'object'}}}]
    with model_profile('large'), limits_scope({'model_profile': 'large'}):
        if structured:
            metrics = HistoryContext._metrics(messages, schemas, messages[-1], messages)
        else:
            _, metrics = build_model_history(messages, schemas=schemas)
    assert metrics['schema_tokens'] == estimate_tokens(json.dumps(schemas, ensure_ascii=False))
    assert metrics['input_limit_tokens'] == 992_000
    assert metrics['input_limit_chars'] == 3_968_000
    assert metrics['context_capacity_tokens'] == 1_000_000
    assert metrics['response_reserve_tokens'] == 8_000
    assert metrics['estimated_input_utilization'] == pytest.approx(metrics['sent_tokens'] / 992_000)
    assert metrics['capacity_utilization'] == pytest.approx(metrics['sent_tokens'] / 1_000_000)
    assert metrics['capacity_utilization'] < metrics['estimated_input_utilization']
