"""Web Flow 边界；受控离线模型回调，不代表真实 provider 执行。"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.storage import SessionStore
from agent_frontend.web.app import create_app


@pytest.fixture
def web_flow(tmp_path, monkeypatch, sandbox_env):
    for name in ('RAG_ASSESS', 'MEMORY_AUTO_EXTRACT', 'ENABLE_AGENT_TASKS'):
        monkeypatch.setattr(config, name, False)
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'private')
    seen = []

    async def model(history, *, session_id, emit):
        text = '\n'.join(str(row.get('content', '')) for row in history)
        seen.append((session_id, text))
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': '受控工具结果总结: ' + history[-1]['content']}
        if '保持运行' in text:
            import asyncio
            await asyncio.Event().wait()
        if '受控失败' in text:
            raise RuntimeError('private-provider-failure-marker')
        for marker, name, arguments in (
            ('工具写入', 'create_file', {'path': 'proof.txt', 'content': 'web-flow-proof'}),
            ('工具读回', 'read_file', {'path': 'proof.txt'}),
        ):
            if marker in text:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'controlled-' + name, 'function': {
                        'name': name, 'arguments': json.dumps(arguments)}}]}
        return {'role': 'assistant', 'content': '受控节点总结'}

    app = create_app(store=store, token='flow-test-token', model=model)
    project = tmp_path / 'project'
    project.mkdir()
    try:
        with TestClient(app, base_url='http://localhost',
                        headers={'Authorization': 'Bearer flow-test-token'}) as client:
            yield client, project, seen
    finally:
        store.close()


def create_flow(client, project, **extra):
    response = client.post('/api/flows', json={'title': '测试流程',
        'workspace_path': str(project), **extra})
    assert response.status_code == 201, response.text
    return response.json()


def save_nodes(client, flow, prompts=('任务 A', '任务 B')):
    flow['nodes'] = [{'id': str(i), 'title': str(i), 'prompt': prompt}
                     for i, prompt in enumerate(prompts)]
    flow['edges'] = [{'source': str(i), 'target': str(i + 1)}
                     for i in range(len(prompts) - 1)]
    response = client.put('/api/flows/' + flow['id'], json=flow)
    assert response.status_code == 200, response.text
    return response.json()


def wait_flow(client, identifier, status):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        response = client.get('/api/flows/' + identifier)
        assert response.status_code == 200, response.text
        flow = response.json()
        if flow.get('run') and flow['run']['status'] in status:
            return flow
        time.sleep(.01)
    pytest.fail('Flow 未达到预期状态')


def test_flow_http_crud_revision_identity_and_explicit_delete(web_flow):
    client, project, _ = web_flow
    flow = create_flow(client, project)
    saved = save_nodes(client, flow)
    assert saved['revision'] > flow['revision']
    stale = client.put('/api/flows/' + flow['id'], json=flow)
    assert stale.status_code == 409
    other = create_flow(client, project)
    assert client.put('/api/flows/' + other['id'], json=saved).status_code == 422
    assert client.get('/api/flows/' + other['id']).json()['nodes'] == []
    for body in ({}, {'confirm': False}):
        assert client.request('DELETE', '/api/flows/' + flow['id'], json=body).status_code == 422
    assert client.request('DELETE', '/api/flows/' + flow['id'], json={'confirm': True}).json() == {'flow_id': flow['id']}
    assert client.get('/api/flows/' + flow['id']).status_code == 404


def test_flow_list_and_session_snapshot_do_not_broadcast_background(web_flow):
    client, project, _ = web_flow
    secret = 'private-background-marker'
    flow = save_nodes(client, create_flow(client, project, handoff=secret), (secret,))
    for path in ('/api/flows', '/api/sessions'):
        response = client.get(path)
        assert response.status_code == 200
        payload = response.json()
        assert isinstance(payload['flows_revision'], int)
        assert len(payload['flows_epoch']) == 32
        assert payload['flows'][0]['id'] == flow['id']
        assert secret not in response.text
        assert not {'nodes', 'edges', 'handoff', 'run'} & payload['flows'][0].keys()


def test_flow_routes_keep_auth_origin_and_validation_body_private(web_flow):
    client, project, _ = web_flow
    assert client.get('/api/flows', headers={'Authorization': ''}).status_code == 401
    assert client.post('/api/flows', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    secret = 'private-invalid-body-marker'
    response = client.post('/api/flows', json={'title': {'secret': secret}})
    assert response.status_code == 422
    assert secret not in response.text
    flow = create_flow(client, project)
    flow['nodes'] = [{'id': 'a', 'title': 'A', 'prompt': secret, 'x': secret}]
    response = client.put('/api/flows/' + flow['id'], json=flow)
    assert response.status_code == 422
    assert secret not in response.text


def test_running_flow_rejects_edits_cancel_blocks_downstream(web_flow):
    client, project, seen = web_flow
    flow = save_nodes(client, create_flow(client, project), ('保持运行', '不得执行'))
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    wait_flow(client, flow['id'], {'running'})
    assert client.put('/api/flows/' + flow['id'], json=flow).status_code == 409
    assert client.request('DELETE', '/api/flows/' + flow['id'], json={'confirm': True}).status_code == 409
    assert client.post('/api/flows/' + flow['id'] + '/cancel').status_code == 200
    result = wait_flow(client, flow['id'], {'cancelled'})
    assert result['run']['nodes']['1']['status'] == 'blocked'
    assert not any('不得执行' in content for _, content in seen)


def test_http_serial_flow_reads_initial_handoff_and_real_file_tools(web_flow):
    client, project, seen = web_flow
    (project / 'background.txt').write_text('首次输入文件标记', encoding='utf-8')
    flow = save_nodes(client, create_flow(client, project,
        permission_policy='full_access'), ('工具写入', '工具读回'))
    flow['nodes'][0]['handoff_file'] = 'background.txt'
    response = client.post('/api/flows/' + flow['id'] + '/run', json={'definition': flow})
    assert response.status_code == 200, response.text
    result = wait_flow(client, flow['id'], {'completed', 'failed'})
    assert result['run']['status'] == 'completed'
    assert (project / 'proof.txt').read_text() == 'web-flow-proof'
    assert '首次输入文件标记' in seen[0][1]
    assert '先阅读' in seen[0][1]
    assert 'web-flow-proof' in result['run']['nodes']['1']['output_handoff']
    assert '受控工具结果总结' in result['run']['nodes']['1']['received_handoff']


def test_http_failure_blocks_downstream_and_hides_provider_error(web_flow):
    client, project, seen = web_flow
    flow = save_nodes(client, create_flow(client, project), ('受控失败', '不得执行'))
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    result = wait_flow(client, flow['id'], {'failed'})
    assert result['run']['nodes']['1']['status'] == 'blocked'
    assert result['run']['nodes']['0']['output_handoff'] == ''
    assert not any('不得执行' in content for _, content in seen)
    assert 'private-provider-failure-marker' not in json.dumps(result)


def test_http_readonly_flow_cannot_write_project(web_flow):
    client, project, _ = web_flow
    flow = save_nodes(client, create_flow(client, project,
        permission_policy='readonly'), ('工具写入',))
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    result = wait_flow(client, flow['id'], {'completed', 'failed'})
    session_id = result['run']['nodes']['0']['session_id']
    session = client.get('/api/sessions/' + session_id).json()
    assert session['permission_policy'] == 'readonly'
    assert not (project / 'proof.txt').exists()
    assert any(row['role'] == 'tool' for row in session['messages'])


def test_http_project_reservation_covers_flows_classic_chat_and_memory(web_flow, tmp_path):
    client, project, _ = web_flow
    classic = client.post('/api/sessions', json={'title': '旧会话', 'workspace_path': str(project)}).json()
    first = save_nodes(client, create_flow(client, project), ('保持运行',))
    second = save_nodes(client, create_flow(client, project), ('任务',))
    other_project = tmp_path / 'other-project'
    other_project.mkdir()
    independent = save_nodes(client, create_flow(client, other_project), ('任务',))
    assert client.post('/api/flows/' + first['id'] + '/run').status_code == 200
    wait_flow(client, first['id'], {'running'})
    assert client.post('/api/flows/' + second['id'] + '/run').status_code == 409
    assert client.post('/api/flows/' + independent['id'] + '/run').status_code == 200
    assert wait_flow(client, independent['id'], {'completed'})['run']['status'] == 'completed'
    session_url = '/api/sessions/' + classic['id']
    assert client.post(session_url + '/messages', json={'text': '不应排队'}).status_code == 409
    assert client.put(session_url + '/memory', json={'content': '不得覆盖', 'confirm': True}).status_code == 409
    assert client.get(session_url + '/memory').json()['content'] != '不得覆盖'
    assert client.patch(session_url, json={'permission_policy': 'readonly'}).status_code == 409
    assert client.post('/api/maintenance/cleanup', json={'confirm': True, 'sessions': True}).status_code == 409
    assert client.post('/api/flows/' + first['id'] + '/cancel').status_code == 200
    wait_flow(client, first['id'], {'cancelled'})
    assert client.post('/api/flows/' + second['id'] + '/run').status_code == 200
    assert wait_flow(client, second['id'], {'completed'})['run']['status'] == 'completed'


def test_http_flow_model_reference_blocks_delete_and_active_edit(web_flow):
    client, project, _ = web_flow
    settings = {'name': '受控连接', 'model': 'offline', 'base_url': 'http://127.0.0.1:9/v1',
                'api_key': 'private-model-key-marker'}
    response = client.post('/api/models', json=settings)
    assert response.status_code == 201, response.text
    profile = response.json()['id']
    flow = save_nodes(client, create_flow(client, project, model_profile=profile), ('保持运行',))
    assert client.delete('/api/models/' + profile, headers={'Content-Length': '0'}).status_code == 409
    assert client.put('/api/models/' + profile, json=settings).status_code == 200
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    wait_flow(client, flow['id'], {'running'})
    assert client.put('/api/models/' + profile, json=settings).status_code == 409
    assert 'private-model-key-marker' not in client.get('/api/flows/' + flow['id']).text
    client.post('/api/flows/' + flow['id'] + '/cancel')
    wait_flow(client, flow['id'], {'cancelled'})


def test_flow_sse_state_is_lightweight_and_revision_changes(web_flow):
    client, project, _ = web_flow
    initial = client.get('/api/flows').json()['flows_revision']
    flow = save_nodes(client, create_flow(client, project,
        handoff='private-sse-background'), ('private-node-prompt',))
    endpoint = next(route.endpoint for route in client.app.routes
                    if getattr(route, 'path', '') == '/api/events')

    class RequestDouble:
        async def is_disconnected(self):
            return False

    async def first_frame():
        response = await endpoint(RequestDouble())
        try:
            return await anext(response.body_iterator)
        finally:
            await response.body_iterator.aclose()

    frame = client.portal.call(first_frame)
    assert frame.startswith('event: state\ndata: ')
    payload = json.loads(frame.split('data: ', 1)[1])
    assert payload['flows_revision'] > initial
    assert payload['flows_epoch'] == client.get('/api/flows').json()['flows_epoch']
    assert payload['flows_epoch'] == client.get('/api/sessions').json()['flows_epoch']
    assert payload['flows'][0]['id'] == flow['id']
    assert 'private-sse-background' not in frame
    assert 'private-node-prompt' not in frame
    assert not {'nodes', 'edges', 'handoff', 'run'} & payload['flows'][0].keys()


def test_http_diamond_handoff_and_project_conflict_are_atomic(web_flow, tmp_path):
    client, project, seen = web_flow
    flow = save_nodes(client, create_flow(client, project), ('A', 'B', 'C', 'D'))
    flow['edges'] = [{'source': a, 'target': b} for a, b in
                     (('0', '1'), ('0', '2'), ('1', '3'), ('2', '3'))]
    assert client.post('/api/flows/' + flow['id'] + '/run', json={'definition': flow}).status_code == 200
    result = wait_flow(client, flow['id'], {'completed'})
    assert result['run']['nodes']['3']['received_handoff'].count('受控节点总结') == 2
    assert len({identifier for identifier, _ in seen}) == 4
    conflicting = client.get('/api/flows/' + flow['id']).json()
    elsewhere = tmp_path / 'conflicting-project'
    elsewhere.mkdir()
    conflicting['nodes'][1]['workspace_path'] = str(elsewhere)
    response = client.post('/api/flows/' + flow['id'] + '/run', json={'definition': conflicting})
    assert response.status_code == 409
    unchanged = client.get('/api/flows/' + flow['id']).json()
    assert unchanged['revision'] == result['revision']
    assert unchanged['nodes'][1]['workspace_path'] is None
    assert not client.app.state.manager.project_reservations


def test_http_confirmation_timeout_denies_real_write(web_flow, monkeypatch):
    client, project, _ = web_flow
    monkeypatch.setattr(config, 'CONFIRM_TIMEOUT', .02)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'standard')
    flow = save_nodes(client, create_flow(client, project), ('工具写入',))
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    result = wait_flow(client, flow['id'], {'completed', 'failed'})
    session_id = result['run']['nodes']['0']['session_id']
    session = client.get('/api/sessions/' + session_id).json()
    assert session['confirmation'] is None
    assert not (project / 'proof.txt').exists()
    tool_results = [row['content'] for row in session['messages'] if row['role'] == 'tool']
    assert tool_results and any('拒绝' in value or '取消' in value for value in tool_results)
    assert not client.app.state.manager.project_reservations


def test_http_flow_known_model_key_redacted_in_detail_summary_and_errors(web_flow):
    client, project, _ = web_flow
    secret = 'private-known-runtime-key-marker'
    settings = {'name': '连接', 'model': 'offline', 'base_url': 'http://127.0.0.1:9/v1', 'api_key': secret}
    assert client.post('/api/models', json=settings).status_code == 201
    flow = create_flow(client, project, title=secret, handoff=secret)
    assert secret not in json.dumps(flow)
    flow = save_nodes(client, flow, (secret,))
    for path in ('/api/flows', '/api/sessions', '/api/flows/' + flow['id']):
        assert secret not in client.get(path).text
    flow['nodes'][0]['x'] = secret
    assert secret not in client.post('/api/flows/' + flow['id'] + '/run', json={'definition': flow}).text


def test_web_reopen_interrupted_persisted_flow_does_not_call_model(tmp_path, monkeypatch, sandbox_env):
    from agent_core.core.flows import FlowService
    from agent_core.core.sessions import SessionManager
    store = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    service = FlowService(SessionManager(store))
    flow = service.create()
    # 控制持久化中断现场，不伪称真实 provider 或录制 trace。
    flow['run'] = {'status': 'running', 'nodes': {}, 'error': ''}
    store.atomic_write(service.directory / (flow['id'] + '.json'), json.dumps(flow))
    store.close()
    calls = []

    async def model(*args, **kwargs):
        calls.append(True)
        raise AssertionError('重开不能重放模型')

    reopened = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=reopened, token='reopen-token', model=model),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer reopen-token'}) as client:
            restored = client.get('/api/flows/' + flow['id'])
            assert restored.status_code == 200
            assert restored.json()['run']['status'] == 'interrupted'
            assert not client.app.state.flows.tasks
            assert not calls
            first_epoch = client.get('/api/flows').json()['flows_epoch']
        with TestClient(create_app(store=reopened, token='reopen-token', model=model),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer reopen-token'}) as client:
            second_epoch = client.get('/api/flows').json()['flows_epoch']
            assert second_epoch != first_epoch
            assert len(second_epoch) == 32
            assert client.get('/api/flows/' + flow['id']).json()['run']['status'] == 'interrupted'
            assert not calls
    finally:
        reopened.close()


def test_http_run_draft_rejects_stale_revision_and_mismatched_identity(web_flow):
    client, project, seen = web_flow
    flow = create_flow(client, project)
    saved = save_nodes(client, flow, ('任务',))
    stale = dict(saved, revision=flow['revision'])
    assert client.post('/api/flows/' + flow['id'] + '/run', json={'definition': stale}).status_code == 409
    other = save_nodes(client, create_flow(client, project), ('不同任务',))
    assert client.post('/api/flows/' + flow['id'] + '/run', json={'definition': other}).status_code == 422
    for item in (saved, other):
        actual = client.get('/api/flows/' + item['id']).json()
        assert actual['revision'] == item['revision']
        assert actual['run'] is None
    assert not client.app.state.manager.project_reservations
    assert not seen


def test_http_node_model_reference_protected_before_node_starts(web_flow):
    client, project, _ = web_flow
    settings = {'name': '下游模型', 'model': 'offline', 'base_url': 'http://127.0.0.1:9/v1'}
    response = client.post('/api/models', json=settings)
    assert response.status_code == 201
    identifier = response.json()['id']
    flow = save_nodes(client, create_flow(client, project), ('保持运行', '不应提前执行'))
    flow['nodes'][1]['model_profile'] = identifier
    response = client.put('/api/flows/' + flow['id'], json=flow)
    assert response.status_code == 200
    assert client.delete('/api/models/' + identifier, headers={'Content-Length': '0'}).status_code == 409
    assert client.post('/api/flows/' + flow['id'] + '/run').status_code == 200
    wait_flow(client, flow['id'], {'running'})
    assert client.put('/api/models/' + identifier, json=settings).status_code == 409
    client.post('/api/flows/' + flow['id'] + '/cancel')
    result = wait_flow(client, flow['id'], {'cancelled'})
    assert result['run']['nodes']['1']['session_id'] is None
    assert client.put('/api/models/' + identifier, json=settings).status_code == 200


def test_classic_agent_creation_readonly_policy_denies_real_file_write(web_flow):
    client, project, _ = web_flow
    response = client.post('/api/sessions', json={'title': '只读经典Agent',
        'workspace_path': str(project), 'permission_policy': 'readonly'})
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    assert response.json()['permission_policy'] == 'readonly'
    assert 'readonly' in client.get('/api/meta').json()['policies']
    assert client.post('/api/sessions/' + identifier + '/messages', json={'text': '工具写入'}).status_code == 202
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        state = client.get('/api/sessions/' + identifier).json()
        if not state['busy']:
            break
        time.sleep(.01)
    else:
        pytest.fail('只读Agent未结束')
    assert state['permission_policy'] == 'readonly'
    assert not (project / 'proof.txt').exists()
    tool_results = [row['content'] for row in state['messages'] if row['role'] == 'tool']
    assert tool_results and any('readonly' in text or '只读' in text for text in tool_results)
    assert state['confirmation'] is None


def test_classic_agent_creation_default_policy_and_forbidden_controls(web_flow, monkeypatch):
    client, _, _ = web_flow
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'smart')
    omitted = client.post('/api/sessions', json={'title': '默认权限'})
    explicit = client.post('/api/sessions', json={'title': '空权限', 'permission_policy': None})
    assert omitted.status_code == explicit.status_code == 201
    assert omitted.json()['permission_policy'] == explicit.json()['permission_policy'] == 'smart'
    assert client.post('/api/sessions', json={'permission_policy': 'standard'}).status_code == 422
    assert client.post('/api/sessions', json={'permission_policy': 'readonly', 'tool_names': ['create_file']}).status_code == 422
