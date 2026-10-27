"""The debug API streams bounded metadata and reads content only on request."""
import json
import time

import pytest

from agent_core import config
from agent_test.runtime import DebugRecorder
from agent_core.core.storage import LazyRecord, SessionStore


@pytest.fixture
def debug_client(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from agent_frontend.web.app import create_app

    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    async def model(history, **kwargs):
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': 'private final answer'}
        if history[-1]['content'] == 'read the file':
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'real-read', 'function': {'name': 'read_file',
                'arguments': json.dumps({'path': 'sample.txt'})}}]}
        return {'role': 'assistant', 'content': 'ordinary answer'}
    try:
        with TestClient(create_app(store=store, token='timeline-token', model=model),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer timeline-token'}) as client:
            yield client
    finally:
        store.close()


def test_debug_incremental_cursor_and_generation_reset(debug_client):
    manager = debug_client.app.state.manager
    session = manager.create(title='private title')
    initial = debug_client.get('/api/debug').json()
    generation = initial['trace']['generation']
    cursor = initial['trace']['cursor']
    manager.debug.event('phase.changed', session_id=session.id, state='running')
    response = debug_client.get('/api/debug', params={'generation': generation, 'cursor': cursor})
    assert response.status_code == 200, response.text
    trace = response.json()['trace']
    assert trace['generation'] == generation
    assert trace['cursor'] > cursor
    assert trace['gap'] is False
    assert trace['reset'] is False
    assert [event['kind'] for event in trace['timeline'] if event['kind'] == 'phase.changed'] == ['phase.changed']
    assert all(event['seq'] > cursor for event in trace['timeline'])
    assert 'private title' not in response.text

    stale = debug_client.get('/api/debug', params={'generation': 'different-generation',
                                                   'cursor': cursor})
    assert stale.status_code == 200
    assert stale.json()['trace']['reset'] is True


def test_debug_timeline_snapshot_does_not_load_lazy_messages(debug_client):
    manager = debug_client.app.state.manager
    session = manager.create(title='private title')

    def fail_if_loaded():
        pytest.fail('snapshot materialized private messages')

    session.record = LazyRecord({'id': session.id, 'status': 'idle', 'last_run': {}},
                                fail_if_loaded)
    manager.debug.event('phase.changed', session_id=session.id, state='idle')
    response = debug_client.get('/api/debug')
    assert response.status_code == 200, response.text
    trace = response.json()['trace']
    assert {'generation', 'cursor', 'oldest_seq', 'gap', 'reset', 'timeline'} <= trace.keys()
    assert session.record.loader is fail_if_loaded
    assert 'private title' not in response.text


def test_debug_expired_cursor_reports_gap_and_resets(debug_client):
    manager = debug_client.app.state.manager
    session = manager.create()
    manager.debug = DebugRecorder(enabled=True, capacity=3)
    generation = debug_client.get('/api/debug', params={'session_id': session.id}).json()['trace']['generation']
    for _ in range(5):
        manager.debug.event('phase.changed', session_id=session.id, state='running')
    response = debug_client.get('/api/debug', params={'generation': generation, 'cursor': 1})
    assert response.status_code == 200, response.text
    trace = response.json()['trace']
    assert trace['gap'] is True
    assert trace['reset'] is True
    assert trace['oldest_seq'] > 2
    assert [row['seq'] for row in trace['timeline']] == [3, 4, 5]
    assert trace['cursor'] == 5


def test_debug_detail_requires_auth_and_local_origin(debug_client):
    manager = debug_client.app.state.manager
    session = manager.create()
    seq = manager.debug.event('model.final', session_id=session.id, message_index=1)
    path = f'/api/debug/events/{seq}/detail'
    assert debug_client.get(path, headers={'Authorization': ''}).status_code == 401
    assert debug_client.get(path, headers={'Host': 'evil.example'}).status_code == 403
    assert debug_client.get(path, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert debug_client.get(path, headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    mismatched = debug_client.get(path, params={'generation': 'different-generation'})
    assert mismatched.status_code in {409, 410}


def test_debug_detail_reads_only_selected_message_and_redacts_keys(debug_client, monkeypatch):
    manager = debug_client.app.state.manager
    session = manager.create()
    secret = 'sk-test-main-secret-1234567890'
    monkeypatch.setattr(config, 'API_KEY', secret)
    session.record['messages'].append({'role': 'assistant',
                                       'content': 'private response ' + secret + ' ending'})
    seq = manager.debug.event('model.final', session_id=session.id, message_index=1)
    snapshot = debug_client.get('/api/debug')
    assert snapshot.status_code == 200
    assert 'private response' not in snapshot.text
    assert secret not in snapshot.text
    generation = snapshot.json()['trace']['generation']
    response = debug_client.get(f'/api/debug/events/{seq}/detail',
                                params={'generation': generation, 'offset': 0, 'limit': 6000})
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail['event']['seq'] == seq
    assert detail['format'] in {'text', 'markdown', 'json'}
    assert 'private response' in detail['content']
    assert secret not in response.text
    assert detail['next_offset'] is None
    assert detail['total_chars'] >= len(detail['content'])


def test_debug_tool_detail_uses_real_call_and_matching_result(debug_client):
    response = debug_client.post('/api/sessions', json={'title': 'private title'})
    assert response.status_code == 201, response.text
    session_id = response.json()['id']
    manager = debug_client.app.state.manager
    workspace = manager.workspace(manager.sessions[session_id])
    (workspace / 'sample.txt').write_text('private file body')
    response = debug_client.post(f'/api/sessions/{session_id}/messages',
                                 json={'text': 'read the file'})
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 5
    while manager.sessions[session_id].busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert not manager.sessions[session_id].busy
    trace = debug_client.get('/api/debug').json()['trace']
    tool = next(row for row in trace['timeline'] if row['kind'] == 'tool.completed'
                and row['session_id'] == session_id and row['tool_call_id'] == 'real-read')
    final = next(row for row in trace['timeline'] if row['kind'] == 'model.final'
                 and row['session_id'] == session_id)
    assert 'private file body' not in json.dumps(trace)
    for event, expected in ((tool, 'private file body'), (final, 'private final answer')):
        detail = debug_client.get(f"/api/debug/events/{event['seq']}/detail",
                                  params={'generation': trace['generation']})
        assert detail.status_code == 200, detail.text
        assert expected in detail.json()['content']
    assert tool['message_index'] < final['message_index']


def test_debug_detail_paginates_redacted_text_and_validates_bounds(debug_client, monkeypatch):
    manager = debug_client.app.state.manager
    session = manager.create()
    secret = 'sk-test-page-secret-1234567890'
    monkeypatch.setattr(config, 'API_KEY', secret)
    session.record['messages'].append({'role': 'assistant', 'content': 'A' * 40 + secret + 'B' * 40})
    seq = manager.debug.event('model.final', session_id=session.id, message_index=1)
    path = f'/api/debug/events/{seq}/detail'
    generation = debug_client.get('/api/debug').json()['trace']['generation']
    first = debug_client.get(path, params={'generation': generation,
                                           'offset': 0, 'limit': 25})
    assert first.status_code == 200, first.text
    first_data = first.json()
    assert len(first_data['content']) == 25
    assert first_data['next_offset'] == 25
    second = debug_client.get(path, params={'generation': generation,
                                            'offset': 25, 'limit': 6000})
    assert second.status_code == 200, second.text
    assert second.json()['next_offset'] is None
    assert secret not in first.text + second.text
    assert first_data['total_chars'] == second.json()['total_chars']
    assert debug_client.get(path, params={'generation': generation,
                                          'limit': 6001}).status_code == 422
    assert debug_client.get(path, params={'generation': generation,
                                          'offset': first_data['total_chars'] + 1}).status_code in {409, 422}


def test_debug_detail_rejects_unknown_or_unowned_event(debug_client):
    manager = debug_client.app.state.manager
    seq = manager.debug.event('model.final', session_id='missing-session', message_index=1)
    response = debug_client.get(f'/api/debug/events/{seq}/detail',
                                params={'generation': manager.debug.generation})
    assert response.status_code in {403, 404, 410}
    response = debug_client.get('/api/debug/events/999999/detail',
                                params={'generation': manager.debug.generation})
    assert response.status_code in {404, 410}


def test_debug_detail_is_absent_when_disabled(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from agent_frontend.web.app import create_app

    monkeypatch.setattr(config, 'DEBUG', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, token='timeline-token'),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer timeline-token'}) as client:
            assert client.get('/api/debug/events/1/detail').status_code == 404
    finally:
        store.close()
