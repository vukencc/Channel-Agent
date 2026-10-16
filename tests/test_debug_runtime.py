"""Bounded debug traces expose timing and status without private content."""
import asyncio
import concurrent.futures
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_agent_startup.core.debug_runtime import DebugRecorder, debug_span, trace_function, traced


def test_disabled_recorder_keeps_no_events():
    recorder = DebugRecorder(enabled=False)
    with recorder.scope('private-session'):
        with recorder.span('model.request', 'private-model') as handle:
            assert handle.id is None
        with debug_span('tool.execute', 'stat_file'):
            pass
    snapshot = recorder.snapshot()
    assert snapshot['enabled'] is False
    assert snapshot['revision'] == 0
    assert snapshot['events'] == []
    assert snapshot['active'] == []
    assert snapshot['dropped'] == 0


def test_nested_spans_show_live_duration_parent_and_copy_isolation():
    recorder = DebugRecorder(enabled=True)
    with recorder.scope('session-one'):
        with recorder.span('agent.run') as outer:
            with debug_span('model.request', 'primary') as inner:
                active = recorder.snapshot()['active']
                assert [(row['id'], row['parent_id']) for row in active] == [
                    (outer.id, None), (inner.id, outer.id)]
                assert {row['session_id'] for row in active} == {'session-one'}
                assert all(row['state'] == 'running' for row in active)
                assert all(row['duration_ms'] >= 0 for row in active)
                active[0]['name'] = 'tampered'
            outer.outcome = 'checkpoint'
    snapshot = recorder.snapshot()
    assert snapshot['active'] == []
    assert [row['state'] for row in snapshot['events']] == ['completed', 'checkpoint']
    assert snapshot['events'][-1]['name'] != 'tampered'
    assert snapshot['revision'] >= 4
    assert all(row['started_at'] > 0 for row in snapshot['events'])


def test_exception_records_only_type_and_reraises():
    recorder = DebugRecorder(enabled=True)
    secret = 'private-model-response-do-not-include'
    with recorder.scope('session-one'):
        with pytest.raises(ValueError, match=secret):
            with recorder.span('model.request'):
                raise ValueError(secret)
    event = recorder.snapshot()['events'][0]
    assert event['state'] == 'error'
    assert event['error_type'] == 'ValueError'
    assert secret not in str(recorder.snapshot())


def test_model_activity_uses_fixed_codes_without_stream_content():
    recorder = DebugRecorder(enabled=True)
    secret = 'private streamed tool arguments'
    with recorder.scope('session-one'):
        with recorder.span('model.request'):
            recorder.activity('session-one', 'content')
            active = recorder.snapshot()['active'][0]
            assert active['activity'] == 'content'
            assert active['last_activity_at'] > 0
            recorder.activity('session-one', secret)
            assert recorder.snapshot()['active'][0]['activity'] == 'content'
            recorder.activity('another-session', 'reasoning')
            assert recorder.snapshot()['active'][0]['activity'] == 'content'
            recorder.activity('session-one', 'tool_arguments')
    event = recorder.snapshot()['events'][0]
    assert event['activity'] == 'tool_arguments'
    assert secret not in str(recorder.snapshot())


@pytest.mark.parametrize(('result', 'state'), [
    ('[超时] 私有命令输出', 'timeout'),
    ('[已取消] 私有取消理由', 'cancelled'),
    ('[取消] 私有取消理由', 'cancelled'),
    ('[拒绝] 私有拒绝理由', 'rejected'),
    ('[已拒绝] 私有拒绝理由', 'rejected'),
    ('[已拦截] 私有拦截理由', 'rejected'),
    ('未知工具: private-tool', 'rejected'),
    ('[失败] 私有失败理由', 'error'),
    ('[错误] 私有失败理由', 'error'),
    ('工具执行失败: 私有失败理由', 'error'),
    ('正常工具内容', 'completed'),
])
def test_tool_total_maps_terminal_status_without_result_body(result, state):
    recorder = DebugRecorder(enabled=True)
    owner = SimpleNamespace(debug=recorder)
    session = SimpleNamespace(id='session-one', record={'status': 'running'})

    @traced('tool.total')
    async def tool(_owner, _session):
        return result

    assert asyncio.run(tool(owner, session)) == result
    event = recorder.snapshot()['events'][0]
    assert event['state'] == state
    assert result not in str(event)


def test_denied_permission_confirmation_is_rejected_without_prompt_content():
    recorder = DebugRecorder(enabled=True)
    secret = 'private confirmation prompt'

    @trace_function('permission.confirm')
    def confirm(_prompt):
        return False

    with recorder.scope('session-one'):
        assert confirm(secret) is False
    event = recorder.snapshot()['events'][0]
    assert event['module'] == 'permission.confirm'
    assert event['state'] == 'rejected'
    assert secret not in str(event)


def test_cancelled_span_is_recorded_and_propagated():
    recorder = DebugRecorder(enabled=True)
    with recorder.scope('session-one'):
        with pytest.raises(asyncio.CancelledError):
            with recorder.span('agent.run'):
                raise asyncio.CancelledError('private cancellation reason')
    event = recorder.snapshot()['events'][0]
    assert event['state'] == 'cancelled'
    assert event['error_type'] == 'CancelledError'
    assert 'private cancellation reason' not in str(event)


def test_capacity_bounds_active_and_completed_events():
    recorder = DebugRecorder(enabled=True, capacity=2)
    with recorder.scope('session-one'):
        with recorder.span('agent.run') as parent:
            with recorder.span('model.request'):
                with recorder.span('tool.execute') as dropped:
                    assert dropped.id is None
                    assert len(recorder.snapshot()['active']) == 2
            with recorder.span('context.prepare'):
                pass
    snapshot = recorder.snapshot()
    assert len(snapshot['events']) == 2
    assert snapshot['active'] == []
    assert snapshot['dropped'] >= 2
    assert {row['module'] for row in snapshot['events']} == {'agent.run', 'context.prepare'}
    assert parent.id is not None


def test_async_thread_context_and_parallel_sessions_stay_isolated():
    recorder = DebugRecorder(enabled=True)
    barrier = threading.Barrier(2)

    def worker():
        with debug_span('tool.execute', 'stat_file'):
            barrier.wait(timeout=2)
            time.sleep(.01)

    async def run_session(identifier):
        with recorder.scope(identifier):
            with debug_span('agent.run'):
                await asyncio.to_thread(worker)

    async def both():
        await asyncio.gather(run_session('first'), run_session('second'))

    asyncio.run(both())
    events = recorder.snapshot()['events']
    assert len(events) == 4
    for identifier in ('first', 'second'):
        own = [row for row in events if row['session_id'] == identifier]
        assert len(own) == 2
        parent = next(row for row in own if row['module'] == 'agent.run')
        tool = next(row for row in own if row['module'] == 'tool.execute')
        assert tool['parent_id'] == parent['id']


@pytest.mark.parametrize(('finish', 'state', 'error_type'), [
    ('success', 'completed', ''),
    ('failure', 'error', 'OSError'),
    ('cancel', 'cancelled', ''),
])
def test_tracked_future_reports_queue_to_completion_without_error_body(finish, state, error_type):
    recorder = DebugRecorder(enabled=True)
    future = concurrent.futures.Future()
    with recorder.scope('session-one'):
        with recorder.span('agent.run') as parent:
            recorder.track_future(future, 'session-one', 'persistence.save')
            active = next(row for row in recorder.snapshot()['active']
                          if row['module'] == 'persistence.save')
            assert active['parent_id'] == parent.id
            assert active['state'] == 'running'
            if finish == 'success':
                future.set_result(None)
            elif finish == 'failure':
                future.set_exception(OSError('private filesystem path'))
            else:
                future.cancel()
    event = next(row for row in recorder.snapshot()['events']
                 if row['module'] == 'persistence.save')
    assert event['state'] == state
    assert event['error_type'] == error_type
    assert event['duration_ms'] >= 0
    assert 'private filesystem path' not in str(event)


@pytest.fixture
def debug_client(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ai_agent_startup import config
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.web.app import create_app

    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def model(history, **kwargs):
        if history[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': 'read complete'}
        if history[-1]['content'] == 'read the file':
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'debug-read', 'function': {'name': 'read_file',
                    'arguments': json.dumps({'path': 'sample.txt'})}}]}
        return {'role': 'assistant', 'content': 'private model content'}

    try:
        with TestClient(create_app(store=store, token='debug-test-token', model=model),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer debug-test-token'}) as client:
            yield client
    finally:
        store.close()


def test_debug_api_requires_auth_and_local_origin(debug_client):
    assert debug_client.get('/api/debug', headers={'Authorization': ''}).status_code == 401
    assert debug_client.get('/api/debug', headers={'Host': 'evil.example'}).status_code == 403
    assert debug_client.get('/api/debug', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert debug_client.get('/api/debug', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_debug_api_returns_only_metadata_and_page(debug_client):
    client = debug_client
    manager = client.app.state.manager
    session = manager.create(title='private title')
    session.record['messages'].append({'role': 'user', 'content': 'private user text'})
    with manager.debug.scope(session.id):
        with manager.debug.span('model.request', 'primary'):
            response = client.get('/api/debug')
    assert response.status_code == 200, response.text
    data = response.json()
    assert {'trace', 'agents', 'runtime', 'agents_omitted'} <= data.keys()
    assert data['trace']['enabled'] is True
    assert any(row['module'] == 'model.request' for row in data['trace']['active'])
    assert any(row['id'] == session.id for row in data['agents'])
    assert 'active_model_calls' in data['runtime']
    assert 'private title' not in response.text
    assert 'private user text' not in response.text
    page = client.get('/debug')
    assert page.status_code == 200
    assert 'text/html' in page.headers['content-type']


def test_debug_snapshot_does_not_materialize_lazy_messages(debug_client):
    from ai_agent_startup.core.storage import LazyRecord

    client = debug_client
    manager = client.app.state.manager
    session = manager.create(title='private title')

    def fail_if_loaded():
        pytest.fail('debug snapshot loaded private messages')

    session.record = LazyRecord({'id': session.id, 'status': 'idle',
                                 'last_run': {}}, fail_if_loaded)
    response = client.get('/api/debug')
    assert response.status_code == 200, response.text
    assert session.record.loader is fail_if_loaded
    assert any(row['id'] == session.id for row in response.json()['agents'])


def test_debug_routes_are_absent_when_disabled(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ai_agent_startup import config
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.web.app import create_app

    monkeypatch.setattr(config, 'DEBUG', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with TestClient(create_app(store=store, token='debug-test-token'),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer debug-test-token'}) as client:
            assert client.get('/api/debug').status_code == 404
            assert client.get('/debug').status_code == 404
    finally:
        store.close()


def test_debug_records_real_model_and_read_file_flow(debug_client):
    client = debug_client
    response = client.post('/api/sessions', json={'title': 'test'})
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    manager = client.app.state.manager
    (manager.workspace(manager.sessions[identifier]) / 'sample.txt').write_text('private file body')
    assert client.post(f'/api/sessions/{identifier}/messages',
                       json={'text': 'read the file'}).status_code == 202
    deadline = time.monotonic() + 3
    while manager.sessions[identifier].busy and time.monotonic() < deadline:
        time.sleep(.01)
    assert not manager.sessions[identifier].busy
    response = client.get('/api/debug')
    assert response.status_code == 200
    events = [row for row in response.json()['trace']['events']
              if row['session_id'] == identifier]
    modules = {row['module'] for row in events}
    assert {'agent.run', 'model.total', 'model.request', 'tool.total', 'tool.execute'} <= modules
    assert any(row['module'] == 'tool.execute' and row['name'] == 'read_file'
               for row in events)
    assert all(row['state'] == 'completed' for row in events)
    assert 'private file body' not in response.text
    assert 'read the file' not in response.text


def test_debug_api_shows_two_active_model_requests_with_separate_sessions(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ai_agent_startup import config
    from ai_agent_startup.core.storage import SessionStore
    from ai_agent_startup.web.app import create_app

    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 2)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    release = threading.Event()
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')

    async def model(history, **kwargs):
        await asyncio.to_thread(release.wait, 3)
        return {'role': 'assistant', 'content': 'finished'}

    try:
        with TestClient(create_app(store=store, token='debug-test-token', model=model),
                        base_url='http://localhost',
                        headers={'Authorization': 'Bearer debug-test-token'}) as client:
            identifiers = []
            for _ in range(2):
                response = client.post('/api/sessions', json={'title': 'private title'})
                assert response.status_code == 201
                identifiers.append(response.json()['id'])
            for identifier in identifiers:
                assert client.post(f'/api/sessions/{identifier}/messages',
                                   json={'text': 'private prompt'}).status_code == 202
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                response = client.get('/api/debug')
                active = [row for row in response.json()['trace']['active']
                          if row['module'] == 'model.request']
                if {row['session_id'] for row in active} == set(identifiers):
                    break
                time.sleep(.01)
            else:
                pytest.fail('both model requests did not become active')
            assert response.json()['runtime']['active_model_calls'] == 2
            assert 'private prompt' not in response.text
            assert 'private title' not in response.text
    finally:
        release.set()
        store.close()


def test_debug_static_assets_and_pure_view_functions(debug_client):
    client = debug_client
    for asset, content_type in [('debug.css', 'text/css'), ('debug-view.js', 'javascript'),
                                ('debug.js', 'javascript')]:
        response = client.get('/static/' + asset)
        assert response.status_code == 200
        assert content_type in response.headers['content-type']

    source = Path(__file__).resolve().parents[1] / 'src/ai_agent_startup/web/static/debug-view.js'
    program = source.read_text() + """
const assert = require('node:assert/strict');
assert.equal(typeof document, 'undefined');
const view = globalThis.AgentDebugView;
const snapshot = {
  agents: [{id:'child',parent_id:'root'}, {id:'orphan',parent_id:'missing'}, {id:'root',parent_id:null}],
  trace: {
    active: [{id:2,session_id:'root',module:'tool.execute',duration_ms:30},
             {id:1,session_id:'root',module:'agent.run',duration_ms:100},
             {id:3,session_id:'child',module:'model.request',duration_ms:20}],
    events: [{id:4,session_id:'root',module:'tool.execute',duration_ms:30,state:'completed'},
             {id:5,session_id:'root',module:'agent.run',duration_ms:100,state:'completed'},
             {id:6,session_id:'child',module:'model.request',duration_ms:20,state:'error'},
             {id:7,session_id:'root',module:'permission.confirm',duration_ms:3,state:'denied'}]
  }
};
assert.deepEqual(view.rows(snapshot, 'root').active.map(x => x.id), [1,2]);
assert.deepEqual(view.rows(snapshot, '', true).events.map(x => x.id), [7,6]);
assert.deepEqual(view.agents(snapshot).map(x => x.id), ['orphan','root','child']);
const modules = view.modules(snapshot);
assert.equal(modules.find(x => x.key === 'agent').max_ms, 100);
assert.equal(modules.find(x => x.key === 'tool').max_ms, 30);
assert.equal(modules.find(x => x.key === 'model').errors, 1);
assert.equal(modules.find(x => x.key === 'permission').errors, 1);
assert.equal(modules.find(x => x.key === 'agent').active, 1);
"""
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is unavailable for the pure view regression')
    result = subprocess.run([node, '-e', program],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_cli_debug_panel_is_bounded_and_uses_trace_metadata(tmp_path, monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from ai_agent_startup import config
    from ai_agent_startup.core.cli import AgentCLI
    from ai_agent_startup.core.storage import SessionStore

    monkeypatch.setattr(config, 'DEBUG', True)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            assert cli.show_debug is True
            with cli.manager.debug.scope(cli.current):
                with cli.manager.debug.span('model.request'):
                    cli.manager.debug.activity(cli.current, 'reasoning')
                    display = cli.debug_text()
            rendered = (''.join(fragment[1] for fragment in display)
                        if isinstance(display, list) else str(display))
            assert len(rendered.splitlines()) <= 6
            assert 'model.request' in rendered or '模型' in rendered
            assert 'reasoning' in rendered or '推理' in rendered
            assert cli.handle('/debug off')
            assert cli.show_debug is False
            assert cli.handle('/debug on')
            assert cli.show_debug is True
            assert not cli.handle('/debug invalid')
    finally:
        store.close()


def test_cli_debug_command_is_rejected_when_feature_disabled(tmp_path, monkeypatch):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from ai_agent_startup import config
    from ai_agent_startup.core.cli import AgentCLI
    from ai_agent_startup.core.storage import SessionStore

    monkeypatch.setattr(config, 'DEBUG', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            assert cli.show_debug is False
            assert not cli.handle('/debug on')
            assert cli.show_debug is False
            assert 'DEBUG' in cli.notice
    finally:
        store.close()
