"""Private runtime endpoint settings, HTTP redaction, and request isolation."""
import asyncio
import json
import os
import stat
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_agent_startup import config
from ai_agent_startup.core import llm
from ai_agent_startup.core.model_settings import model_profile
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.web.app import create_app


def registry(tmp_path):
    from ai_agent_startup.core.runtime_models import RuntimeModelRegistry
    return RuntimeModelRegistry(tmp_path)


def test_registry_private_atomic_persistence_and_blank_key_retention(tmp_path):
    settings = registry(tmp_path)
    public = settings.add(name='My service', model='chat-test', base_url='https://example.test/v1',
                          api_key='secret-test', parameters={'temperature': .2})
    assert public['has_api_key'] is True
    assert 'secret-test' not in json.dumps(public)
    assert stat.S_IMODE(settings.path.stat().st_mode) == 0o600
    updated = settings.update(public['id'], name='Updated', model='chat-test',
                              base_url='https://example.test/v1', api_key='')
    assert updated['has_api_key'] is True
    restored = registry(tmp_path).resolve(public['id'])
    assert restored.api_key == 'secret-test'
    assert 'secret-test' not in repr(restored)
    settings.delete(public['id'])
    assert not registry(tmp_path).contains(public['id'])


@pytest.mark.parametrize('url', ['file:///tmp/key', 'https://user:secret@example.test/v1',
                                'https://example.test/v1?api_key=secret',
                                'https://example.test/v1#secret'])
def test_registry_rejects_credential_urls_without_echo(tmp_path, url):
    settings = registry(tmp_path)
    with pytest.raises(ValueError) as error:
        settings.add(name='test', model='m', base_url=url, api_key='secret-test')
    assert 'secret' not in str(error.value)
    assert not settings.path.exists()


@pytest.mark.parametrize('link', ['symbolic', 'hard'])
def test_private_registry_rejects_linked_file(tmp_path, link):
    settings = registry(tmp_path)
    outside = tmp_path / 'outside.json'
    outside.write_text('{"sentinel": true}')
    if link == 'symbolic':
        settings.path.symlink_to(outside)
    else:
        os.link(outside, settings.path)
    with pytest.raises(ValueError):
        settings.add(name='test', model='m', base_url='https://example.test/v1', api_key='s')
    assert outside.read_text() == '{"sentinel": true}'


def test_frozen_snapshots_and_parallel_endpoint_contexts(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    a = settings.add(name='a', model='model-a', base_url='https://a.test/v1', api_key='key-a',
                     parameters={'temperature': .1})
    b = settings.add(name='b', model='model-b', base_url='https://b.test/v1', api_key='key-b',
                     parameters={'temperature': .9})
    snapshot = settings.resolve(a['id'])
    settings.update(a['id'], name='a', model='changed', base_url='https://a.test/v1', api_key='new')
    assert snapshot.model == 'model-a' and snapshot.api_key == 'key-a'
    with pytest.raises(TypeError):
        snapshot.parameters['temperature'] = 1
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
    captured = []
    class Client:
        def __init__(self, endpoint):
            self.endpoint = endpoint
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            await asyncio.sleep(0)
            captured.append((self.endpoint.copy(), kwargs))
            return None
    monkeypatch.setattr(llm, 'endpoint_client', Client)
    async def invoke(identifier, endpoint):
        with model_profile(identifier, endpoint):
            await llm._open_stream([])
    async def run():
        await asyncio.gather(invoke(a['id'], snapshot), invoke(b['id'], settings.resolve(b['id'])))
    asyncio.run(run())
    assert {(endpoint['base_url'], endpoint['api_key'], args['model'], args['temperature'])
            for endpoint, args in captured} == {
        ('https://a.test/v1', 'key-a', 'model-a', .1),
        ('https://b.test/v1', 'key-b', 'model-b', .9)}


def test_runtime_failure_does_not_use_global_fallback(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    entry = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [{'model': 'global-fallback'}])
    monkeypatch.setattr(config, 'MAX_RETRIES', 1)
    endpoints = []
    def fail(endpoint):
        endpoints.append(endpoint)
        raise httpx.ConnectError('unavailable')
    monkeypatch.setattr(llm, 'endpoint_client', fail)
    with model_profile(entry['id'], settings.resolve(entry['id'])):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(llm._open_stream([]))
    assert len(endpoints) == 1


def test_http_model_crud_redacts_keys_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {'builtin': {'model': 'base', 'parameters': {}}})
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            assert client.get('/api/models').json()['models'][0]['read_only'] is True
            payload = dict(name='Personal', model='m', base_url='https://example.test/v1',
                           api_key='do-not-return-this')
            result = client.post('/api/models', json=payload)
            assert result.status_code == 201
            public = result.json()
            assert 'do-not-return-this' not in result.text
            assert public['id'] in client.get('/api/meta').json()['models']
            selected = client.post('/api/sessions', json={}).json()
            response = client.patch('/api/sessions/' + selected['id'], json={'model_profile': public['id']})
            assert response.status_code == 200
            assert response.json()['model_profile'] == public['id']
            stored_session = store.directory(selected['id']) / 'session.json'
            assert 'do-not-return-this' not in stored_session.read_text()
            assert 'do-not-return-this' not in client.get('/api/sessions/' + selected['id'] + '/export?format=json').text
            assert 'do-not-return-this' not in (store.root / 'audit.jsonl').read_text()
            bad = client.post('/api/models', json={**payload, 'parameters': {'temperature': 'do-not-return-this'}})
            assert bad.status_code == 422
            assert 'do-not-return-this' not in bad.text
            assert client.put('/api/models/' + public['id'], json={**payload, 'api_key': ''}).json()['has_api_key']
            assert client.delete('/api/models/builtin', headers={'Content-Length': '0'}).status_code == 403
            blocked = client.delete('/api/models/' + public['id'], headers={'Content-Length': '0'})
            assert blocked.status_code == 409
            assert client.get('/api/sessions/' + selected['id']).json()['model_profile'] == public['id']
            assert client.app.state.manager.model_registry.contains(public['id'])
            assert client.patch('/api/sessions/' + selected['id'], json={'model_profile': None}).status_code == 200
            assert client.delete('/api/models/' + public['id'], headers={'Content-Length': '0'}).status_code == 200
    finally:
        store.close()


def test_registry_failed_write_preserves_previous_snapshot(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    entry = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='key-a')
    def fail(*args, **kwargs):
        raise OSError('atomic replace unavailable')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(ValueError):
        settings.update(entry['id'], name='a', model='changed', base_url='https://a.test/v1', api_key='new')
    assert settings.resolve(entry['id']).model == 'm'
    assert registry(tmp_path).resolve(entry['id']).api_key == 'key-a'
    assert not list(tmp_path.glob('.runtime-models-*.tmp'))


def test_registry_rejects_replaced_root_and_unsafe_permissions(tmp_path):
    location = tmp_path / 'state'
    settings = registry(location)
    entry = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='key-a')
    settings.path.chmod(0o644)
    with pytest.raises(ValueError):
        registry(location)
    settings.path.chmod(0o600)
    location.rename(tmp_path / 'old-state')
    location.mkdir()
    with pytest.raises(ValueError):
        settings.update(entry['id'], name='a', model='new', base_url='https://a.test/v1', api_key='secret')
    assert not (location / settings.filename).exists()


def test_literal_endpoint_key_never_inherits_global_credentials(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    entry = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='')
    monkeypatch.setattr(config, 'API_KEY', 'global-provider-private')
    monkeypatch.setattr(llm, '_fallback_clients', {})
    monkeypatch.setattr(config, 'validate_proxy_environment', lambda: None)
    captured = []
    def make_client(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(chat=SimpleNamespace(completions=object()))
    monkeypatch.setattr(llm, 'AsyncOpenAI', make_client)
    llm.endpoint_client(settings.resolve(entry['id']).endpoint())
    assert captured[0]['base_url'] == 'https://a.test/v1'
    assert captured[0]['api_key'] == 'not-required'


def test_runtime_provider_exception_does_not_expose_reflected_key(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    entry = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    def fail(endpoint):
        raise RuntimeError('provider reflected private-key')
    monkeypatch.setattr(llm, 'endpoint_client', fail)
    with model_profile(entry['id'], settings.resolve(entry['id'])):
        with pytest.raises(llm.RuntimeModelRequestError) as error:
            asyncio.run(llm.call_model([], emit=lambda *_: None))
    assert 'private-key' not in str(error.value)


def test_model_http_denies_cross_origin_and_busy_changes(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store), base_url='http://localhost') as client:
            payload = dict(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
            response = client.post('/api/models', json=payload, headers={'Origin': 'https://elsewhere.test'})
            assert response.status_code == 403
            assert not (store.root / '.runtime-models.json').exists()
            identifier = client.post('/api/models', json=payload).json()['id']
            session_id = client.post('/api/sessions', json={}).json()['id']
            assert client.patch('/api/sessions/' + session_id, json={'model_profile': identifier}).status_code == 200
            running = client.app.state.manager.sessions[session_id]
            running.task = SimpleNamespace(done=lambda: False)
            assert client.put('/api/models/' + identifier, json=payload).status_code == 409
            assert client.delete('/api/models/' + identifier, headers={'Content-Length': '0'}).status_code == 409
            running.task = None
    finally:
        store.close()


def test_runtime_clients_are_not_retained_after_credential_rotation(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='key-a')
    monkeypatch.setattr(llm, '_fallback_clients', {})
    monkeypatch.setattr(config, 'validate_proxy_environment', lambda: None)
    monkeypatch.setattr(llm, 'AsyncOpenAI', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=object())))
    first = llm.endpoint_client(settings.resolve(public['id']).endpoint())
    second = llm.endpoint_client(settings.resolve(public['id']).endpoint())
    assert first is not second
    settings.update(public['id'], name='a', model='m', base_url='https://a.test/v1', api_key='new-key')
    settings.delete(public['id'])
    assert not llm._fallback_clients


def test_runtime_clients_close_on_retry_and_after_owned_stream(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='key-a')
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    clients = []
    class Stream:
        closed = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
        async def close(self):
            self.closed = True
    class Client:
        def __init__(self):
            self.closed = False
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            if self is clients[0]:
                raise httpx.ConnectError('retry')
            return Stream()
        async def close(self):
            self.closed = True
    def client(endpoint):
        value = Client()
        clients.append(value)
        return value
    monkeypatch.setattr(llm, 'endpoint_client', client)
    async def run():
        with model_profile(public['id'], settings.resolve(public['id'])):
            stream = await llm._open_stream([])
            assert clients[0].closed
            assert not clients[1].closed
            await stream.close()
            assert clients[1].closed
    asyncio.run(run())


def test_provider_finish_reason_never_persists_arbitrary_text(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='provider-secret')
    class Stream:
        def __aiter__(self):
            async def frames():
                yield SimpleNamespace(choices=[SimpleNamespace(finish_reason='provider-secret',
                    delta=SimpleNamespace(content='answer', reasoning_content=None, tool_calls=None))])
            return frames()
        async def close(self):
            pass
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            return Stream()
        async def close(self):
            pass
    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: Client())
    events = []
    with model_profile(public['id'], settings.resolve(public['id'])):
        with pytest.raises(llm.ModelResponseError) as error:
            asyncio.run(llm.call_model([], emit=lambda *event: events.append(event)))
    assert 'provider-secret' not in str(error.value)
    assert 'provider-secret' not in json.dumps(events)


def test_registry_disallows_credentials_under_tool_readable_roots(tmp_path):
    from ai_agent_startup.core.runtime_models import RuntimeModelRegistry
    root = tmp_path / 'workspace' / 'state'
    settings = RuntimeModelRegistry(root, forbidden_roots=[tmp_path / 'workspace'])
    assert settings.ids() == list(config.MODEL_PROFILES)
    with pytest.raises(ValueError):
        settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    assert not settings.path.exists()
    unsafe = RuntimeModelRegistry(root)
    unsafe.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    with pytest.raises(ValueError):
        RuntimeModelRegistry(root, forbidden_roots=[tmp_path / 'workspace'])


def test_cancelled_runtime_client_initialization_still_closes_client(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    started, release = threading.Event(), threading.Event()
    closed = []
    class Client:
        async def close(self):
            closed.append(True)
    def make_client(endpoint):
        started.set()
        assert release.wait(2)
        return Client()
    monkeypatch.setattr(llm, 'endpoint_client', make_client)
    async def run():
        with model_profile(public['id'], settings.resolve(public['id'])):
            pending = asyncio.create_task(llm._open_stream([]))
            assert await asyncio.to_thread(started.wait, 2)
            pending.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
    asyncio.run(run())
    assert closed == [True]


def test_blank_key_endpoint_change_cannot_reuse_another_provider_credential(tmp_path):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://original.test/v1', api_key='private-key')
    with pytest.raises(ValueError) as error:
        settings.update(public['id'], name='a', model='m', base_url='https://different.test/v1', api_key='')
    assert 'private-key' not in str(error.value)
    original = settings.resolve(public['id'])
    assert original.api_key == 'private-key' and original.base_url == 'https://original.test/v1'
    settings.update(public['id'], name='a', model='m', base_url='https://original.test/v1/', api_key='')
    assert settings.resolve(public['id']).api_key == 'private-key'
    settings.update(public['id'], name='a', model='m', base_url='https://different.test/v1', api_key='new-key')
    assert settings.resolve(public['id']).api_key == 'new-key'


def test_runtime_call_preserves_local_budget_checkpoint_exception(tmp_path, monkeypatch):
    from ai_agent_startup.core.budgets import BudgetExceeded
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key')
    async def deny_reservation(*args, **kwargs):
        raise BudgetExceeded('会话模型预算已耗尽')
    monkeypatch.setattr(llm, 'reserve_request', deny_reservation)
    closed = []
    class Client:
        async def close(self):
            closed.append(True)
    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: Client())
    with model_profile(public['id'], settings.resolve(public['id'])):
        with pytest.raises(BudgetExceeded, match='会话模型预算已耗尽'):
            asyncio.run(llm.call_model([], emit=lambda *_: None))
    assert closed == [True]


def test_runtime_provider_options_do_not_inherit_environment_provider_settings(tmp_path, monkeypatch):
    settings = registry(tmp_path)
    public = settings.add(name='a', model='m', base_url='https://a.test/v1', api_key='private-key',
                          parameters={'top_p': .8})
    monkeypatch.setattr(config, 'MODEL_PARAMETERS', {'temperature': .7, 'tool_choice': 'none'})
    monkeypatch.setattr(config, 'THINKING_MODE', 'enabled')
    monkeypatch.setattr(config, 'REASONING_EFFORT', 'high')
    with model_profile(public['id'], settings.resolve(public['id'])):
        assert llm.model_options() == {'top_p': .8}

@pytest.mark.parametrize('knowledge_setting', ['DOC_DIR', 'RAG_SOURCES'])
def test_manager_rejects_private_credentials_under_a_knowledge_root(tmp_path, monkeypatch, knowledge_setting):
    from ai_agent_startup.core.sessions import SessionManager
    state = tmp_path / 'private-state'
    if knowledge_setting == 'DOC_DIR':
        monkeypatch.setattr(config, 'DOC_DIR', state)
    else:
        monkeypatch.setattr(config, 'RAG_SOURCES', {'fixture': {'path': state}})
    async def run():
        store = SessionStore(state, tmp_path / 'work')
        manager = SessionManager(store)
        try:
            with pytest.raises(ValueError, match='独立状态目录'):
                manager.model_registry.add(name='test', model='m', base_url='http://localhost:9999/v1', api_key='test-secret')
            assert not manager.model_registry.path.exists()
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())