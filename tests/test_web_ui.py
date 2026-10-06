"""浏览器入口复用真实会话链路，验证认证、审批与文件边界。"""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from ai_agent_startup import config
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.web.app import create_app


TOKEN = 'local-test-token'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def model(history, **kwargs):
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '文件操作结束'}
        if history[-1]['content'] == 'write a file':
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'web-write', 'function': {'name': 'create_file',
                    'arguments': json.dumps({'path': 'web.txt', 'content': 'actual data'})}}]}
        if kwargs.get('emit'):
            kwargs['emit']('content', '流式回复')
        return {'role': 'assistant', 'content': '真实测试模型边界'}

    app = create_app(store=store, token=TOKEN, model=model)
    try:
        with TestClient(app, base_url='http://localhost', headers=HEADERS) as value:
            yield value
    finally:
        store.close()


def new_session(client, **extra):
    response = client.post('/api/sessions', json={'title': 'test', **extra})
    assert response.status_code == 201, response.text
    return response.json()['id']


def wait_session(client, identifier, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get('/api/sessions/' + identifier)
        assert response.status_code == 200, response.text
        state = response.json()
        if predicate(state):
            return state
        time.sleep(.01)
    pytest.fail('Web 会话未达到预期状态')


def test_api_requires_token_and_rejects_cross_origin_and_host(client):
    assert client.get('/api/sessions', headers={'Authorization': ''}).status_code == 401
    assert client.post('/api/sessions', json={'title': 'x'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/sessions', headers={'Host': 'evil.example'}).status_code == 403
    assert client.get('/api/meta').status_code == 200


def test_web_session_chat_rename_settings_and_page_history(client):
    identifier = new_session(client)
    assert client.patch('/api/sessions/' + identifier, json={
        'title': 'renamed', 'permission_policy': 'smart',
    }).status_code == 200
    assert client.post(f'/api/sessions/{identifier}/messages', json={'text': 'hello'}).status_code == 202
    state = wait_session(client, identifier, lambda row: not row['busy'])
    assert state['title'] == 'renamed' and state['permission_policy'] == 'smart'
    assert any(row['role'] == 'assistant' for row in state['messages'])
    page = client.get(f'/api/sessions/{identifier}?limit=1').json()
    assert len(page['messages']) == 1 and page['next_before'] > 0
    assert client.get('/api/sessions').json()['runtime']['active_model_calls'] == 0


def test_web_confirmation_is_bound_to_current_request_and_denial_keeps_files(client):
    identifier = new_session(client)
    client.post(f'/api/sessions/{identifier}/messages', json={'text': 'write a file'})
    state = wait_session(client, identifier, lambda row: bool(row['confirmation']))
    assert client.post(f'/api/sessions/{identifier}/confirmation', json={
        'confirmation_id': 'wrong', 'allowed': True,
    }).status_code == 409
    assert client.post(f'/api/sessions/{identifier}/confirmation', json={
        'confirmation_id': state['confirmation']['id'], 'allowed': False,
    }).status_code == 200
    wait_session(client, identifier, lambda row: not row['busy'])
    assert client.get(f'/api/sessions/{identifier}/file?path=web.txt').status_code == 404


def test_web_full_access_writes_and_export_files_are_real(client):
    identifier = new_session(client)
    client.patch(f'/api/sessions/{identifier}', json={'permission_policy': 'full_access'})
    client.post(f'/api/sessions/{identifier}/messages', json={'text': 'write a file'})
    wait_session(client, identifier, lambda row: not row['busy'])
    assert client.get(f'/api/sessions/{identifier}/file?path=web.txt').json()['content'] == 'actual data'
    export = client.get(f'/api/sessions/{identifier}/export?format=json')
    assert export.status_code == 200 and export.json()['id'] == identifier
    assert client.get(f'/api/sessions/{identifier}/file?path=../outside').status_code == 403


def test_web_delete_requires_consent_and_preserves_workspace(client):
    identifier = new_session(client)
    workspace = client.app.state.manager.workspace(client.app.state.manager.sessions[identifier])
    (workspace / 'keep.txt').write_text('keep')
    assert client.request('DELETE', f'/api/sessions/{identifier}', json={'confirm': False}).status_code == 422
    assert client.get(f'/api/sessions/{identifier}').status_code == 200
    deleted = client.request('DELETE', f'/api/sessions/{identifier}', json={'confirm': True})
    assert deleted.status_code == 200
    assert (workspace / 'keep.txt').read_text() == 'keep'
    assert client.get(f'/api/sessions/{identifier}').status_code == 404


def test_web_memory_requires_explicit_consent_and_persists(client):
    identifier = new_session(client)
    assert client.put(f'/api/sessions/{identifier}/memory', json={'content': 'saved', 'confirm': False}).status_code == 422
    assert client.put(f'/api/sessions/{identifier}/memory', json={'content': 'saved', 'confirm': True}).status_code == 200
    assert client.get(f'/api/sessions/{identifier}/memory').json()['content'] == 'saved'


def test_web_explorer_rejects_symlink_escape_and_hides_session_state(client, tmp_path):
    identifier = new_session(client)
    workspace = client.app.state.manager.workspace(client.app.state.manager.sessions[identifier])
    outside = tmp_path / 'private.txt'
    outside.write_text('private')
    (workspace / 'linked.txt').symlink_to(outside)
    assert client.get(f'/api/sessions/{identifier}/file?path=linked.txt').status_code == 403
    assert client.get(f'/api/sessions/{identifier}/files?path=../../state').status_code == 403
    assert client.get(f'/api/sessions/{identifier}/file?path=/etc/passwd').status_code == 403


def test_web_meta_does_not_return_credentials_or_model_parameters(client, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_PROFILES', {'private': {'api_key': 'must-not-leak'}})
    response = client.get('/api/meta')
    assert response.status_code == 200
    assert 'must-not-leak' not in response.text
    assert 'full_access' in response.json()['policies']


def test_web_payload_validation_and_task_list(client):
    assert client.post('/api/sessions', json={'title': 'x', 'workspace_mode': 'outside'}).status_code == 422
    assert client.post('/api/sessions', json={'title': 'x', 'unexpected': True}).status_code == 422
    assert client.get('/api/tasks').json() == {'tasks': [], 'commands': []}


@pytest.mark.parametrize('directory', [False, True])
def test_web_file_boundary_holds_when_path_changes_after_validation(client, tmp_path, monkeypatch, directory):
    from ai_agent_startup.web import app as web_app
    identifier = new_session(client)
    workspace = client.app.state.manager.workspace(client.app.state.manager.sessions[identifier])
    target = workspace / 'changing'
    outside = tmp_path / 'outside'
    if directory:
        target.mkdir()
        outside.mkdir()
        (outside / 'private.txt').write_text('private')
    else:
        target.write_text('inside')
        outside.write_text('private')
    original = web_app.resolve_path
    changed = False

    def change_after_validation(path):
        nonlocal changed
        result = original(path)
        if path == 'changing' and not changed:
            changed = True
            if directory:
                target.rmdir()
            else:
                target.unlink()
            target.symlink_to(outside, target_is_directory=directory)
        return result

    monkeypatch.setattr(web_app, 'resolve_path', change_after_validation)
    route = 'files' if directory else 'file'
    response = client.get(f'/api/sessions/{identifier}/{route}?path=changing')
    assert response.status_code == 403
    assert 'private' not in response.text


def test_web_launcher_reads_host_and_port_from_dotenv(tmp_path):
    (tmp_path / '.env').write_text('WEB_UI_HOST=::1\nWEB_UI_PORT=9876\n')
    environment = {**os.environ, 'AI_AGENT_PROJECT_ROOT': str(tmp_path),
                   'OPENCODE_API_KEY': 'test-key', 'BASE_URL': 'https://example.invalid/v1',
                   'MODEL': 'test-model', 'DOC_DIR': str(tmp_path), 'WEB_UI_TOKEN': 'test-token'}
    environment.pop('WEB_UI_HOST', None)
    environment.pop('WEB_UI_PORT', None)
    script = ('import json,uvicorn; from ai_agent_startup.web.server import main; '
              'uvicorn.run=lambda app,**options:print(json.dumps({key:options[key] for key in ("host","port")})); main()')
    result = subprocess.run([sys.executable, '-c', script], env=environment,
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == {'host': '::1', 'port': 9876}


def test_web_snapshot_counts_messages_for_fast_turn_and_reconnection(client):
    identifier = new_session(client)
    initial = next(row for row in client.get('/api/sessions').json()['sessions'] if row['id'] == identifier)
    assert initial['total_messages'] == 1
    client.post(f'/api/sessions/{identifier}/messages', json={'text': 'instant reply'})
    state = wait_session(client, identifier, lambda row: not row['busy'])
    updated = next(row for row in client.get('/api/sessions').json()['sessions'] if row['id'] == identifier)
    assert updated['total_messages'] == state['total_messages'] == 3


def test_web_explorer_empty_path_selects_project_root(client):
    identifier = new_session(client)
    response = client.get(f'/api/sessions/{identifier}/files?path=')
    assert response.status_code == 200
    assert response.json() == {'path': '', 'entries': []}


def test_web_plan_remains_readable_after_session_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def model(history, **kwargs):
        await asyncio.Event().wait()

    try:
        with TestClient(create_app(store=store, token=TOKEN, model=model),
                        base_url='http://localhost', headers=HEADERS) as client:
            identifier = new_session(client)
            manager = client.app.state.manager
            owner = manager.sessions[identifier]
            client.portal.call(manager.task_plans.create, owner, 'inspect', [
                {'key': 'step', 'title': 'Inspect', 'acceptance': 'Checked', 'depends_on': []},
            ], 0)
            assert client.post(f'/api/sessions/{identifier}/messages', json={'text': 'work'}).status_code == 202
            wait_session(client, identifier, lambda row: row['busy'])
            assert client.post(f'/api/sessions/{identifier}/stop').status_code == 200
            assert owner.cancelled.is_set()
            response = client.get(f'/api/sessions/{identifier}/plan')
            assert response.status_code == 200, response.text
            assert response.json()['goal'] == 'inspect'
    finally:
        store.close()


def test_web_plan_read_is_forbidden_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    try:
        with TestClient(create_app(store=store, token=TOKEN, model=model),
                        base_url='http://localhost', headers=HEADERS) as client:
            identifier = new_session(client)
            assert client.get(f'/api/sessions/{identifier}/plan').status_code == 403
    finally:
        store.close()
