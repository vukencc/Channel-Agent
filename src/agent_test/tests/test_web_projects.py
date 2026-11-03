"""独立模板导入项目；受控模型调用真实 Core 文件工具。"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


@pytest.fixture
def projects_api(tmp_path, monkeypatch, sandbox_env):
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'ENABLE_AGENT_TASKS'):
        monkeypatch.setattr(config, name, False)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    calls = []

    async def model(history, *, session_id, emit):
        calls.append(session_id)
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '项目验证完成'}
        return {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'project-write', 'function': {'name': 'create_file',
                'arguments': json.dumps({'path': 'proof.txt', 'content': 'project-only'})}}]}

    app = create_app(store=store, model=model)
    try:
        with TestClient(app, base_url='http://localhost') as client:
            yield client, tmp_path, calls
    finally:
        store.close()


def template(client, **extra):
    response = client.post('/api/flows', json={'title': '可复用模板', **extra})
    assert response.status_code == 201, response.text
    flow = response.json()
    flow['nodes'] = [{'id': 'n', 'title': '实现', 'prompt': '在项目中创建 proof.txt'}]
    response = client.put('/api/flows/' + flow['id'], json=flow)
    assert response.status_code == 200, response.text
    return response.json()


def test_template_import_creates_independent_project_instance(projects_api):
    client, tmp_path, _ = projects_api
    original = template(client)
    directory = tmp_path / '空工作区'
    directory.mkdir()
    response = client.post('/api/projects', json={
        'title': '独立项目', 'workspace_path': str(directory), 'flow_id': original['id']})
    assert response.status_code == 201, response.text
    project = response.json()
    assert project['flow_id'] != original['id']
    detail = client.get('/api/flows/' + project['flow_id']).json()
    assert detail['project_id'] == project['id']
    assert detail['project']['workspace_path'] == str(directory)
    assert detail['workspace_path'] is None
    assert client.get('/api/flows/' + original['id']).json() == original
    listing = client.get('/api/sessions').json()
    assert any(row['id'] == project['id'] for row in listing['projects'])
    assert 'handoff' not in listing['projects'][0]
    assert any(row.get('project_id') == project['id'] for row in listing['flows'])


def test_project_run_uses_selected_empty_directory_and_ignores_template_paths(projects_api):
    client, tmp_path, calls = projects_api
    selected, stale = tmp_path / 'selected', tmp_path / 'stale'
    selected.mkdir()
    stale.mkdir()
    original = template(client, workspace_path=str(stale))
    original['nodes'][0]['workspace_path'] = str(stale)
    original = client.put('/api/flows/' + original['id'], json=original).json()
    response = client.post('/api/projects', json={'title': '安全目录', 'flow_id': original['id'],
        'workspace_path': str(selected), 'permission_policy': 'full_access'})
    assert response.status_code == 201, response.text
    project = response.json()
    detail = client.get('/api/flows/' + project['flow_id']).json()
    assert all(node['workspace_path'] is None for node in detail['nodes'])
    started = client.post('/api/flows/' + project['flow_id'] + '/run', json={})
    assert started.status_code == 200, started.text
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        detail = client.get('/api/flows/' + project['flow_id']).json()
        if detail['run']['status'] not in {'queued', 'running'}:
            break
        time.sleep(.01)
    assert detail['run']['status'] == 'completed', detail['run']
    assert (selected / 'proof.txt').read_text() == 'project-only'
    assert list(stale.iterdir()) == []
    assert calls
    assert client.get('/api/flows/' + original['id']).json() == original


def test_project_without_workspace_cannot_run_or_fall_back_to_application(projects_api):
    client, _, calls = projects_api
    original = template(client)
    response = client.post('/api/projects', json={'title': '稍后选择目录', 'flow_id': original['id']})
    assert response.status_code == 201, response.text
    project = response.json()
    response = client.post('/api/flows/' + project['flow_id'] + '/run', json={})
    assert response.status_code == 409, response.text
    assert not calls


def test_project_updates_require_revision_and_instances_cannot_change_owner(projects_api):
    client, tmp_path, _ = projects_api
    original = template(client)
    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir()
    two.mkdir()
    project = client.post('/api/projects', json={'title': '一', 'flow_id': original['id'],
        'workspace_path': str(one)}).json()
    changed = client.patch('/api/projects/' + project['id'], json={
        'revision': project['revision'], 'workspace_path': str(two), 'permission_policy': 'readonly'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['workspace_path'] == str(two)
    assert changed.json()['revision'] > project['revision']
    stale = client.patch('/api/projects/' + project['id'], json={
        'revision': project['revision'], 'workspace_path': str(one)})
    assert stale.status_code == 409
    detail = client.get('/api/flows/' + project['flow_id']).json()
    detail.pop('project')
    detail['project_id'] = None
    assert client.put('/api/flows/' + project['flow_id'], json=detail).status_code == 409


def test_project_delete_keeps_directory_template_and_node_history(projects_api):
    client, tmp_path, _ = projects_api
    directory = tmp_path / 'kept'
    directory.mkdir()
    (directory / 'user.txt').write_text('用户内容')
    original = template(client)
    project = client.post('/api/projects', json={'title': '可删除记录', 'flow_id': original['id'],
        'workspace_path': str(directory)}).json()
    url = '/api/projects/' + project['id']
    assert client.request('DELETE', url, json={'confirm': False}).status_code == 422
    response = client.request('DELETE', url, json={'confirm': True})
    assert response.status_code == 200, response.text
    assert client.get(url).status_code == 404
    assert client.get('/api/flows/' + project['flow_id']).status_code == 404
    assert client.get('/api/flows/' + original['id']).json() == original
    assert (directory / 'user.txt').read_text() == '用户内容'


def test_project_model_reference_cannot_be_deleted_before_first_run(projects_api):
    client, tmp_path, _ = projects_api
    directory = tmp_path / 'model-project'
    directory.mkdir()
    response = client.post('/api/models', json={'name': '项目专属模型', 'model': 'controlled-model',
        'base_url': 'http://127.0.0.1:1/v1', 'api_key': ''})
    assert response.status_code == 201, response.text
    model_id = response.json()['id']
    original = template(client)
    response = client.post('/api/projects', json={'title': '尚未运行', 'flow_id': original['id'],
        'workspace_path': str(directory), 'model_profile': model_id})
    assert response.status_code == 201, response.text
    response = client.request('DELETE', '/api/models/' + model_id, json={})
    assert response.status_code == 409, '项目引用的模型在首次运行前也必须保留'
