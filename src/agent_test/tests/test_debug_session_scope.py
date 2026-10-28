"""DEBUG 只展示选定根会话的树；受控元数据不调用真实模型。"""
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from agent_test.runtime import DebugRecorder
from test_debug_timeline_api import debug_client


@pytest.fixture
def debug_tree(debug_client):
    manager = debug_client.app.state.manager
    first = manager.create(title='first')
    child = manager.create(title='child', parent=first)
    second = manager.create(title='second')
    other_child = manager.create(title='other child', parent=second)
    return manager, first, child, second, other_child


def test_debug_scope_filters_agent_tree_timeline_legacy_active_and_runtime(debug_client, debug_tree):
    manager, first, child, second, other_child = debug_tree
    with ExitStack() as stack:
        for value in (first, child, second, other_child):
            stack.enter_context(manager.debug.scope(value.id))
            with manager.debug.span('context.prepare'):
                pass
            stack.enter_context(manager.debug.span('model.request'))
        response = debug_client.get('/api/debug', params={'session_id': child.id})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['scope_session_id'] == first.id
    assert {row['id'] for row in data['agents']} == {first.id, child.id}
    for key in ('timeline', 'events', 'active'):
        assert data['trace'][key]
        assert {row['session_id'] for row in data['trace'][key]} <= {first.id, child.id}
    assert second.id not in response.text
    assert other_child.id not in response.text


def test_debug_scope_generations_reset_when_cursor_changes_root(debug_client, debug_tree):
    manager, first, child, second, _ = debug_tree
    old = manager.debug.event('phase.changed', session_id=second.id, state='idle')
    first_data = debug_client.get('/api/debug', params={'session_id': first.id}).json()
    manager.debug.event('phase.changed', session_id=child.id, state='running')
    switched = debug_client.get('/api/debug', params={
        'session_id': second.id, 'cursor': first_data['trace']['cursor'],
        'generation': first_data['trace']['generation']}).json()
    assert switched['scope_session_id'] == second.id
    assert switched['trace']['generation'] != first_data['trace']['generation']
    assert switched['trace']['reset'] is True
    assert any(row['seq'] == old for row in switched['trace']['timeline'])
    latest = manager.debug.event('phase.changed', session_id=second.id, state='running')
    resumed = debug_client.get('/api/debug', params={
        'session_id': second.id, 'cursor': switched['trace']['cursor'],
        'generation': switched['trace']['generation']}).json()
    assert resumed['trace']['incremental'] is True
    assert [row['seq'] for row in resumed['trace']['timeline']] == [latest]
    missing_generation = debug_client.get('/api/debug', params={
        'session_id': second.id, 'cursor': resumed['trace']['cursor']}).json()['trace']
    assert missing_generation['reset'] is True
    assert any(row['seq'] == old for row in missing_generation['timeline'])


def test_debug_default_scope_prefers_busy_child_root_then_recent_root(debug_client, debug_tree):
    manager, first, child, second, _ = debug_tree
    manager.debug.event('phase.changed', session_id=second.id, state='running')
    child.task = SimpleNamespace(done=lambda: False)
    try:
        busy = debug_client.get('/api/debug').json()
        assert busy['scope_session_id'] == first.id
        assert {row['id'] for row in busy['agents']} == {first.id, child.id}
    finally:
        child.task = None
    assert debug_client.get('/api/debug').json()['scope_session_id'] == second.id


def test_debug_missing_or_invalid_scope_never_falls_back_to_all_history(debug_client, debug_tree):
    manager, first, _, second, _ = debug_tree
    assert debug_client.get('/api/debug', params={'session_id': 'missing'}).status_code == 404
    assert debug_client.get('/api/debug', params={'session_id': '../invalid'}).status_code == 404
    manager.debug.event('phase.changed', session_id=first.id, state='idle')
    manager.debug.event('phase.changed', session_id=second.id, state='idle')
    manager.sessions.clear()
    empty = debug_client.get('/api/debug').json()
    assert empty['scope_session_id'] is None
    assert empty['agents'] == []
    assert empty['trace']['timeline'] == []
    assert empty['trace']['events'] == []
    assert empty['trace']['active'] == []
    assert empty['runtime']['active_agents'] == 0
    assert empty['runtime']['active_model_calls'] == 0
    assert empty['runtime']['active_tools'] == []


def test_debug_scoped_detail_rejects_other_root_before_loading_messages(debug_client, debug_tree):
    manager, first, child, second, _ = debug_tree
    seq = manager.debug.event('model.final', session_id=second.id, message_index=1)
    scoped = debug_client.get('/api/debug', params={'session_id': child.id}).json()
    response = debug_client.get(f'/api/debug/events/{seq}/detail', params={
        'session_id': child.id, 'generation': scoped['trace']['generation']})
    assert response.status_code == 404, response.text
    own_seq = manager.debug.event('phase.changed', session_id=child.id, state='running')
    own = debug_client.get(f'/api/debug/events/{own_seq}/detail', params={
        'session_id': child.id, 'generation': scoped['trace']['generation']})
    assert own.status_code == 200, own.text
    assert own.json()['scope_session_id'] == first.id
    wrong_generation = debug_client.get(f'/api/debug/events/{own_seq}/detail', params={
        'session_id': second.id, 'generation': scoped['trace']['generation']})
    assert wrong_generation.status_code == 410


def test_debug_scope_dropped_counts_exclude_other_roots(debug_client, debug_tree):
    manager, first, _, second, _ = debug_tree
    manager.debug = DebugRecorder(enabled=True, capacity=3)
    manager.debug.event('phase.changed', session_id=first.id, state='idle')
    for _ in range(5):
        manager.debug.event('phase.changed', session_id=second.id, state='idle')
    scoped = debug_client.get('/api/debug', params={'session_id': first.id}).json()
    assert scoped['trace']['dropped'] == 1
    assert scoped['trace']['timeline'] == []
    other = debug_client.get('/api/debug', params={'session_id': second.id}).json()
    assert other['trace']['dropped'] == 2
    assert [row['seq'] for row in other['trace']['timeline']] == [4, 5, 6]


def test_debug_runtime_counts_only_selected_sessions(debug_client, debug_tree):
    manager, first, child, second, _ = debug_tree
    first.task = SimpleNamespace(done=lambda: False)
    second.task = SimpleNamespace(done=lambda: False)
    manager._active_model_calls = 3
    manager._active_model_sessions = {first.id: 1, child.id: 1, second.id: 1}
    manager._active_tools = {
        'one': {'session_id': child.id, 'name': 'read_file', 'state': 'running'},
        'two': {'session_id': second.id, 'name': 'write_file', 'state': 'running'},
    }
    try:
        data = debug_client.get('/api/debug', params={'session_id': first.id}).json()
        assert data['runtime']['active_agents'] == 1
        assert data['runtime']['active_model_calls'] == 2
        assert [row['session_id'] for row in data['runtime']['active_tools']] == [child.id]
        assert manager.concurrency_status()['active_model_calls'] == 3
    finally:
        first.task = second.task = None
        manager._active_model_calls = 0
        manager._active_model_sessions.clear()
        manager._active_tools.clear()


def test_debug_scope_strips_outside_relationships_and_cross_root_communication(debug_client, debug_tree):
    manager, first, child, second, _ = debug_tree
    manager.debug.event('agent.created', session_id=first.id, related_session_id=second.id)
    manager.debug.event('message.sent', session_id=first.id, related_session_id=second.id,
                        message_seq=1)
    manager.debug.event('message.read', session_id=first.id, related_session_id=second.id,
                        message_seq=1)
    manager.debug.event('message.sent', session_id=first.id, related_session_id=child.id,
                        message_seq=2)
    with manager.debug.scope(first.id, parent_session_id=second.id):
        with manager.debug.span('context.prepare'):
            data = debug_client.get('/api/debug', params={'session_id': first.id}).json()
    assert second.id not in str(data)
    messages = [row for row in data['trace']['timeline'] if row['kind'].startswith('message.')]
    assert len(messages) == 1
    assert messages[0]['related_session_id'] == child.id
    raw_messages = [row for row in manager.debug.snapshot()['timeline'] if row['kind'].startswith('message.')]
    assert len(raw_messages) == 3
    assert any(row.get('related_session_id') == second.id for row in raw_messages)


def test_debug_scope_keeps_root_visible_when_agent_capacity_is_one(debug_client, debug_tree):
    manager, first, child, _, _ = debug_tree
    manager.debug = DebugRecorder(enabled=True, capacity=1)
    child.task = SimpleNamespace(done=lambda: False)
    try:
        data = debug_client.get('/api/debug', params={'session_id': child.id}).json()
        assert [row['id'] for row in data['agents']] == [first.id]
        assert data['agents_omitted'] == 1
        assert data['runtime']['active_agents'] == 1
    finally:
        child.task = None


def test_debug_scope_rejects_orphan_and_cyclic_trees(debug_client, debug_tree):
    manager, first, child, second, other_child = debug_tree
    first.record['delegated_from'] = child.id
    second.record['delegated_from'] = 'missing-parent'
    assert debug_client.get('/api/debug', params={'session_id': child.id}).status_code == 404
    assert debug_client.get('/api/debug', params={'session_id': other_child.id}).status_code == 404
    assert debug_client.get('/api/debug').json()['scope_session_id'] is None


def test_debug_runtime_model_count_is_restored_after_request_error(debug_client, debug_tree, monkeypatch):
    import asyncio

    manager, first, _, _, _ = debug_tree

    async def failing_model(history, **kwargs):
        assert manager.concurrency_status({first.id})['active_model_calls'] == 1
        raise RuntimeError('controlled model boundary failure')

    monkeypatch.setattr(manager, 'model', failing_model)
    with pytest.raises(RuntimeError, match='controlled model boundary failure'):
        asyncio.run(manager._call_model([], session_id=first.id))
    assert manager.concurrency_status({first.id})['active_model_calls'] == 0
    assert manager._active_model_sessions == {}


def test_debug_drop_counter_index_is_bounded_and_marks_incomplete_scoped_counts():
    recorder = DebugRecorder(enabled=True, capacity=1)
    for index in range(10003):
        recorder.event('phase.changed', session_id=f'session-{index}', state='idle')
    assert len(recorder._dropped_by_session) == 10000
    scoped = recorder.snapshot(session_ids={'session-0'}, scope_session_id='session-0')
    assert scoped['dropped_incomplete'] is True
    assert scoped['dropped'] == 0
    assert recorder.snapshot()['dropped'] == 10002


def test_debug_scope_membership_changes_reset_cached_timeline(debug_client, debug_tree):
    manager, first, child, _, _ = debug_tree
    child_seq = manager.debug.event('phase.changed', session_id=child.id, state='idle')
    before = debug_client.get('/api/debug', params={'session_id': first.id}).json()['trace']
    assert any(row['seq'] == child_seq for row in before['timeline'])
    removed = manager.sessions.pop(child.id)
    after_removal = debug_client.get('/api/debug', params={
        'session_id': first.id, 'cursor': before['cursor'], 'generation': before['generation']}).json()['trace']
    assert after_removal['generation'] != before['generation']
    assert after_removal['reset'] is True
    assert after_removal['incremental'] is False
    assert all(row.get('session_id') != child.id for row in after_removal['timeline'])
    manager.sessions[child.id] = removed
    after_addition = debug_client.get('/api/debug', params={
        'session_id': first.id, 'cursor': after_removal['cursor'],
        'generation': after_removal['generation']}).json()['trace']
    assert after_addition['generation'] == before['generation']
    assert after_addition['reset'] is True
    assert after_addition['incremental'] is False
    assert any(row['seq'] == child_seq for row in after_addition['timeline'])


def test_debug_scope_generation_ignores_order_and_runtime_state(debug_client, debug_tree):
    manager, first, child, _, _ = debug_tree
    before = debug_client.get('/api/debug', params={'session_id': first.id}).json()['trace']
    manager.sessions[child.id] = manager.sessions.pop(child.id)
    child.record['status'] = 'running'
    child.task = SimpleNamespace(done=lambda: False)
    try:
        after = debug_client.get('/api/debug', params={
            'session_id': first.id, 'cursor': before['cursor'], 'generation': before['generation']}).json()['trace']
        assert after['generation'] == before['generation']
        assert after['reset'] is False
        assert after['incremental'] is True
    finally:
        child.task = None
