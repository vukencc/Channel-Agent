"""Working HTTP 认证、运行状态和密钥脱敏；模型为受控回调。"""
import asyncio
import time

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


def test_working_endpoint_auth_live_and_redaction(tmp_path, monkeypatch, sandbox_env):
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'ENABLE_AGENT_TASKS'):
        monkeypatch.setattr(config, name, False)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    workspace = tmp_path / 'project'
    workspace.mkdir()
    secret = 'private-working-credential-marker'

    async def model(history, *, session_id, emit):
        await asyncio.Event().wait()

    app = create_app(store=store, token='working-test-token', model=model)
    try:
        with TestClient(app, base_url='http://localhost', headers={
            'Authorization': 'Bearer working-test-token',
        }) as client:
            assert client.get('/api/working', headers={'Authorization': ''}).status_code == 401
            assert client.get('/api/working', headers={'Origin': 'http://evil.example'}).status_code == 403
            assert client.get('/api/working').json() == {'runs': []}
            response = client.post('/api/models', json={'name': '离线连接', 'model': 'offline',
                'base_url': 'http://127.0.0.1:9/v1', 'api_key': secret})
            assert response.status_code == 201
            flow = client.post('/api/flows', json={'title': '模板'}).json()
            flow['nodes'] = [{'id': 'n', 'title': '分析', 'prompt': '任' * 155 + secret}]
            assert client.put('/api/flows/' + flow['id'], json=flow).status_code == 200
            project = client.post('/api/projects', json={'title': '运行项目',
                'workspace_path': str(workspace), 'flow_id': flow['id']}).json()
            flow_id = project['flow_id']
            assert client.post('/api/flows/' + flow_id + '/run', json={}).status_code == 200
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                response = client.get('/api/working')
                runs = response.json()['runs']
                if runs and runs[0]['nodes'][0]['session_id']:
                    break
                time.sleep(.02)
            assert runs and runs[0]['flow_id'] == flow_id
            assert secret not in response.text and secret[:7] not in response.text
            assert runs[0]['nodes'][0]['task_summary'].endswith('…')
            assert client.post('/api/flows/' + flow_id + '/cancel').status_code == 200
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and client.get('/api/working').json()['runs']:
                time.sleep(.02)
            assert client.get('/api/working').json() == {'runs': []}
    finally:
        store.close()
