"""Web 状态只投影安全指标，保留监督降级且不读取归档正文。"""
from types import SimpleNamespace
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from agent_core import config
from agent_core.core.storage import LazyRecord, SessionStore
from agent_frontend.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    app = create_app(store=store)
    try:
        with TestClient(app, base_url='http://localhost') as client:
            yield client
    finally:
        store.close()


def test_context_metrics_are_allowlisted_in_snapshot_and_detail(client):
    identifier = client.post('/api/sessions', json={'title': 'metrics'}).json()['id']
    session = client.app.state.manager.sessions[identifier]
    session.record['last_run'] = {'context': {
        'structured': True, 'utilization': .61, 'sent_tokens': 420,
        'archived_chunks': 3, 'context_revision': 2,
        'supervisor': 'unavailable:MissingSessionID secret-key',
        'summary_kind': 'extractive_fallback', 'compact_reason': 'budget_risk',
        'session_summary': 'private summary', 'RawHistory': 'private original',
        'api_key': 'secret-key',
    }}
    for response in (client.get('/api/sessions'), client.get(f'/api/sessions/{identifier}')):
        row = response.json()['sessions'][0] if 'sessions' in response.json() else response.json()
        status = row['context_status']
        assert status['utilization'] == .61 and status['sent_tokens'] == 420
        assert status['archived_chunks'] == 3 and status['context_revision'] == 2
        assert status['supervisor'] == 'unavailable'
        assert status['summary_kind'] == 'extractive_fallback'
        assert status['compact_reason'] == 'budget_risk'
        assert not any(value in response.text for value in ('private summary', 'private original', 'secret-key', 'MissingSessionID'))


def test_context_status_uses_cached_archive_counts_without_loading_private_files(client):
    identifier = client.post('/api/sessions', json={'title': 'cache'}).json()['id']
    manager = client.app.state.manager
    async def close():
        pass
    manager.history_context = SimpleNamespace(
        cache={identifier: {'revision': 4, 'chunks': [{'Summary': 'private'}] * 7,
                            'summary_kind': 'model', 'session_summary': 'private'}},
        checks={identifier: 6}, supervisor=SimpleNamespace(settings=SimpleNamespace(enabled=True)),
        close=close)
    status = client.get('/api/sessions').json()['sessions'][0]['context_status']
    assert status['archived_chunks'] == 7 and status['context_revision'] == 4
    assert status['check_count'] == 6 and status['supervisor_enabled'] is True
    manager.history_context = None


def test_context_status_empty_invalid_and_lazy_data_are_safe(client):
    identifier = client.post('/api/sessions', json={'title': 'empty'}).json()['id']
    session = client.app.state.manager.sessions[identifier]
    initial = client.get('/api/sessions').json()['sessions'][0]['context_status']
    assert initial['available'] is False
    session.record['last_run'] = {'context': {'sent_tokens': True, 'utilization': float('nan'),
        'summary_kind': 'secret', 'supervisor': 'secret', 'archived_chunks': -1}}
    status = client.get('/api/sessions').json()['sessions'][0]['context_status']
    assert not any(key in status for key in ('sent_tokens', 'utilization', 'summary_kind', 'archived_chunks'))
    assert status['supervisor'] == 'unknown'
    metadata = {key: value for key, value in session.record.items() if key != 'messages'}
    metadata['message_count'] = len(session.record['messages'])
    session.record = LazyRecord(metadata, lambda: pytest.fail('snapshot must not materialize history'))
    assert client.get('/api/sessions').status_code == 200
    session.record = dict(metadata, messages=[])


def test_provider_error_is_not_exposed_in_web_status(client):
    identifier = client.post('/api/sessions', json={'title': 'failure'}).json()['id']
    session = client.app.state.manager.sessions[identifier]
    session.record.update(status='error', error='ProviderError: secret-key private request')
    for response in (client.get('/api/sessions'), client.get(f'/api/sessions/{identifier}')):
        assert 'secret-key' not in response.text and 'private request' not in response.text
        assert '任务运行失败' in response.text


@pytest.mark.parametrize('error_type', ['APIConnectionError', 'ModelConnectionError'])
def test_connection_error_has_actionable_safe_web_message(client, error_type):
    identifier = client.post('/api/sessions', json={'title': 'connection'}).json()['id']
    session = client.app.state.manager.sessions[identifier]
    session.record.update(status='error', error=f'{error_type}: private-token https://private-host')
    for response in (client.get('/api/sessions'), client.get(f'/api/sessions/{identifier}')):
        row = response.json()['sessions'][0] if 'sessions' in response.json() else response.json()
        assert '模型服务连接' in row['error']
        assert '代理' in row['error'] and '继续' in row['error']
        assert 'private-token' not in response.text and 'private-host' not in response.text


def test_browser_dynamic_phase_and_progress_refresh():
    node = shutil.which('node') or shutil.which('node.exe')
    if node is None:
        pytest.skip('JavaScript behavioral check requires Node.js')
    source = (Path(__file__).parents[3] / 'src/agent_frontend/web/static/app.js').read_text()
    phase = re.search(r'  function phaseLabel\(session\) \{.*?\n  \}', source, re.S).group()
    progress = re.search(r'  function renderSessionProgress\(session\) \{.*?\n  \}', source, re.S).group()
    history = re.search(r'  async function loadConversation\(.*?\n  \}', source, re.S).group()
    script = """
const assert = require('node:assert/strict');
const phaseLabels = { idle: '就绪', running: '运行中' };
const nodes = {};
const $ = id => nodes[id] ||= { textContent: '', hidden: false };
const setText = (id, value) => { $(id).textContent = value; };
const state = { renderSignatures: {}, meta: { task_plans_enabled: true } };
""" + phase + progress + """
for (const phase of ['等待模型响应', '准备模型上下文', '监督上下文：判定是否需要 Compact', '整理上下文：压缩与归档', '生成工具参数：run_command']) {
  assert.equal(phaseLabel({ phase, busy: true }), phase);
}
assert.equal(phaseLabel({ phase: '等待模型响应', confirmation: {} }), '等待确认');
assert.equal(phaseLabel({ phase: 'x'.repeat(1000), busy: true }).length, 120);
assert.equal(phaseLabel({ phase: 'line\\nline' }), 'line line');
assert.equal(phaseLabel({ phase: '', status: 'idle' }), '就绪');
const session = { id: 'one', context_status: { structured: true, utilization: .55,
  archived_chunks: 2, supervisor: 'unavailable', summary_kind: 'extractive_fallback',
  compact_reason: 'budget_risk' }, plan: { counts: { completed: 1, pending: 2 }, status: 'pending' } };
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /预算 55%.*已归档 2 块.*监督：不可用.*摘要：已降级/);
assert.match(nodes['plan-progress'].textContent, /计划 1\\/3 步/);
session.context_status.compacted = true;
session.context_status.compact_reason = 'high_utilization_and_variance';
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /本次压缩：高占用且价值差异过大/);
session.context_status.compact_reason = 'context_insufficient';
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /本次压缩：上下文额度不足/);
session.context_status.compacted = false;
renderSessionProgress(session);
assert.doesNotMatch(nodes['context-progress'].textContent, /本次压缩/);
assert.match(nodes['context-progress'].textContent, /压缩判定：上下文额度不足/);
session.context_status.supervisor = 'already_compacted';
session.context_status.compact_reason = '';
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /相同历史已压缩，无需重复/);
assert.doesNotMatch(nodes['context-progress'].textContent, /本次压缩/);
session.context_status.supervisor = 'cooldown';
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /新增历史尚少，无需重复整理/);
session.context_status.supervisor = 'hard_budget';
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /额度不足，直接整理/);
session.context_status.archived_chunks = 4;
session.plan.counts = { completed: 3 };
renderSessionProgress(session);
assert.match(nodes['context-progress'].textContent, /已归档 4 块/);
assert.match(nodes['plan-progress'].textContent, /计划 3\\/3 步/);
renderSessionProgress({ id: 'two' });
assert.match(nodes['context-progress'].textContent, /首次模型调用后更新/);
""" + history + """
Object.assign(state, { sessions: new Map([['race', { id: 'race', context_status: { archived_chunks: 1 },
  plan: { revision: 1 }, error: 'old' }]]), history: new Map(), loading: new Set(), pendingHistory: new Set(),
  snapshotVersion: 1, activeId: null, ready: true });
const nearBottom = () => false;
const sessionPath = id => id;
let finishResponse;
const jsonApi = () => new Promise(resolve => { finishResponse = resolve; });
const renderSessions = () => {};
const renderTabs = () => {};
const toast = value => assert.fail(value);
const errorText = error => error.message;
(async () => {
  const pending = loadConversation('race');
  state.snapshotVersion++;
  state.sessions.set('race', { id: 'race', context_status: { archived_chunks: 4 }, plan: { revision: 3 }, error: '' });
  finishResponse({ id: 'race', context_status: { archived_chunks: 1 }, plan: { revision: 1 }, error: 'old', messages: [] });
  await pending;
  assert.equal(state.sessions.get('race').context_status.archived_chunks, 4);
  assert.equal(state.sessions.get('race').plan.revision, 3);
  assert.equal(state.sessions.get('race').error, '');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, '-e', script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_below_threshold_supervisor_state_is_public_and_does_not_become_unknown():
    from types import SimpleNamespace
    from agent_frontend.web.context_status import context_status
    session = SimpleNamespace(record={'last_run': {'context': {'structured': True,
        'utilization': .3, 'supervisor': 'below_threshold'}}})
    assert context_status(session, None)['supervisor'] == 'below_threshold'


@pytest.mark.parametrize('status', ['cooldown', 'hard_budget'])
def test_compact_frequency_state_is_public_without_private_fields(status):
    from types import SimpleNamespace
    from agent_frontend.web.context_status import context_status
    session = SimpleNamespace(record={'last_run': {'context': {
        'supervisor': status, 'before_utilization': 1.02, 'target_utilization': .75,
        'new_history_groups': 2, 'raw_history': 'private', 'session_summary': 'private'}}})
    public = context_status(session, None)
    assert public['supervisor'] == status
    assert public['before_utilization'] == 1.02
    assert public['target_utilization'] == .75
    assert public['new_history_groups'] == 2
    assert 'private' not in json.dumps(public)


@pytest.mark.parametrize('reason', ['context_insufficient', 'high_utilization_and_variance'])
def test_compound_compact_policy_is_visible_without_private_history(client, reason):
    identifier = client.post('/api/sessions', json={'title': 'compound policy'}).json()['id']
    session = client.app.state.manager.sessions[identifier]
    session.record['last_run'] = {'context': {
        'structured': True, 'compact_reason': reason, 'compacted': True,
        'supervisor': 'already_compacted', 'utilization': .7,
        'last_compact_input_digest': 'PRIVATE_FINGERPRINT', 'RawHistory': 'PRIVATE_RAW'}}
    for response in (client.get('/api/sessions'), client.get(f'/api/sessions/{identifier}')):
        row = response.json()['sessions'][0] if 'sessions' in response.json() else response.json()
        assert row['context_status']['compact_reason'] == reason
        assert row['context_status']['compacted'] is True
        assert row['context_status']['supervisor'] == 'already_compacted'
        assert 'PRIVATE_FINGERPRINT' not in response.text
        assert 'PRIVATE_RAW' not in response.text
