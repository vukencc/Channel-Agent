import asyncio
import json

import pytest
from agent_frontend.desktop.runtime import RuntimeBridge


@pytest.mark.parametrize(('model', 'url', 'key', 'available'), [
    (None, None, None, False), ('model', 'https://example.invalid', None, False),
    ('model', None, 'private-key-marker', False),
    ('model', 'https://example.invalid', 'private-key-marker', True),
])
def test_default_model_availability_is_public_boolean(tmp_path, monkeypatch, model, url, key, available):
    from fastapi.testclient import TestClient
    from agent_core import config
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore
    from agent_frontend.web.app import create_app

    monkeypatch.setattr(config, 'MODEL', model)
    monkeypatch.setattr(config, 'BASE_URL', url)
    monkeypatch.setattr(config, 'API_KEY', key)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        bridge = RuntimeBridge(tmp_path)
        bridge._store = store
        bridge._manager = SessionManager(store)
        payloads = []
        bridge.state_changed.connect(payloads.append)
        bridge._publish()
        assert json.loads(payloads[0])['default_model_available'] is available
        assert 'private-key-marker' not in payloads[0]
        app = create_app(store=store, token='test-token')
        with TestClient(app, base_url='http://localhost',
                        headers={'Authorization': 'Bearer test-token'}) as client:
            response = client.get('/api/models')
            assert response.json()['default_model_available'] is available
            assert 'private-key-marker' not in response.text
    finally:
        store.close()


def test_designer_request_returns_safe_error_to_matching_panel(tmp_path, monkeypatch):
    import agent_core.core.flow_designer as designer

    async def fail(*args, **kwargs):
        raise ValueError("设计模型返回无效 JSON")

    monkeypatch.setattr(designer, "design_flow", fail)
    bridge = RuntimeBridge(tmp_path)
    results = []
    bridge.finished.connect(lambda action, payload: results.append((action, json.loads(payload))))
    asyncio.run(bridge._design({"messages": [], "request_id": "request-1"}, 0))
    assert results == [("flow_design", {"error": "设计模型返回无效 JSON", "request_id": "request-1"})]


def test_bridge_reports_budget_failure_with_selected_options(tmp_path):
    from agent_core.core.budgets import BudgetExceeded
    from agent_core.core.model_settings import active_profile, active_reasoning_effort
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    seen = []

    async def model(*args, **kwargs):
        seen.append((active_profile.get(), active_reasoning_effort.get()))
        raise BudgetExceeded('private diagnostic')

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        bridge = RuntimeBridge(tmp_path)
        bridge._manager = SessionManager(store, model=model)
        bridge._mutations = asyncio.Lock()
        results = []
        bridge.finished.connect(lambda action, payload: results.append(json.loads(payload)))
        asyncio.run(bridge._design({'messages': [{'role': 'user', 'content': '设计流程'}],
                                    'model_profile': None, 'reasoning_effort': 'xhigh',
                                    'request_id': 'budget'}, 0))
        assert seen == [(None, 'xhigh')]
        assert '预算' in results[0]['error']
        assert 'private' not in results[0]['error']
    finally:
        store.close()


def test_designer_proxy_error_does_not_echo_exception_details(tmp_path):
    import pytest
    from agent_core.core.flow_designer import design_flow
    from agent_core.core.model_transport import ModelProxyError
    from agent_core.core.sessions import SessionManager
    from agent_core.core.storage import SessionStore

    async def fail(*args, **kwargs):
        raise ModelProxyError('private proxy credentials https://private.invalid')

    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with pytest.raises(ValueError, match='系统代理') as error:
            asyncio.run(design_flow(SessionManager(store, model=fail),
                                   [{'role': 'user', 'content': '设计流程'}]))
        assert 'private' not in str(error.value)
        assert error.value.__suppress_context__
    finally:
        store.close()
