"""Explicit local user maintenance preserves project and persistent user data."""
import asyncio
import json
import concurrent.futures
import os
import threading

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient

from ai_agent_startup import config
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.web.app import create_app


TOKEN = 'maintenance-test-token'


async def _model(history, **kwargs):
    return {'role': 'assistant', 'content': 'done'}


@pytest.fixture
def maintenance_client(tmp_path, monkeypatch):
    project, raw, cache, logs = (tmp_path / name for name in ('project', 'raw', 'cache', 'logs'))
    for path in (project, raw, cache, logs):
        path.mkdir()
    monkeypatch.setattr(config, 'PROJECT_ROOT', project)
    monkeypatch.setattr(config, 'DOC_DIR', raw)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', cache)
    monkeypatch.setattr(config, 'AUDIT_LOG', logs / 'audit.jsonl')
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    app = create_app(store=store, token=TOKEN, model=_model)
    try:
        with TestClient(app, base_url='http://localhost',
                        headers={'Authorization': 'Bearer ' + TOKEN}) as client:
            yield client
    finally:
        store.close()


def _new_session(client, title='maintenance'):
    response = client.post('/api/sessions', json={'title': title})
    assert response.status_code == 201, response.text
    return response.json()['id']


def _cleanup(client, **selection):
    return client.post('/api/maintenance/cleanup', json={
        'confirm': True, 'cache': False, 'logs': False, 'sessions': False, **selection,
    })


def test_default_web_access_requires_no_token_but_blocks_cross_site(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    try:
        app = create_app(store=store, model=_model)
        with TestClient(app, base_url='http://localhost') as client:
            assert client.get('/api/sessions').status_code == 200
            assert client.get('/api/meta').status_code == 200
            for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                            {'Sec-Fetch-Site': 'cross-site'}):
                assert client.get('/api/sessions', headers=headers).status_code == 403
    finally:
        store.close()


def test_explicit_factory_token_still_enforces_authentication(maintenance_client):
    client = maintenance_client
    assert client.get('/api/sessions', headers={'Authorization': ''}).status_code == 401
    assert client.get('/api/sessions').status_code == 200
    assert client.get('/api/sessions', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403


def test_maintenance_preview_counts_only_selected_resource_types(maintenance_client):
    client = maintenance_client
    manager = client.app.state.manager
    one, two = _new_session(client, 'one'), _new_session(client, 'two')
    (config.RAG_CACHE_DIR / 'one.bin').write_bytes(b'12345')
    (config.RAG_CACHE_DIR / 'nested').mkdir()
    (config.RAG_CACHE_DIR / 'nested' / 'two.bin').write_bytes(b'1234567')
    config.AUDIT_LOG.write_bytes(b'old-main\n')
    paths = [config.AUDIT_LOG]
    for identifier in (one, two):
        path = manager.store.directory(identifier) / 'audit.jsonl'
        path.write_bytes(b'old-session\n')
        paths.append(path)
    preview = client.get('/api/maintenance')
    assert preview.status_code == 200, preview.text
    result = preview.json()
    assert result['cache'] == {'files': 2, 'bytes': 12}
    assert result['logs'] == {'files': 3, 'bytes': sum(path.stat().st_size for path in paths)}
    assert result['sessions']['count'] == 2
    assert result['busy'] is False
    assert (config.RAG_CACHE_DIR / 'one.bin').read_bytes() == b'12345'


@pytest.mark.parametrize('payload', [
    {'cache': True},
    {'confirm': False, 'cache': True},
    {'confirm': True, 'cache': False, 'logs': False, 'sessions': False},
    {'confirm': True, 'cache': 'yes'},
    {'confirm': True, 'cache': True, 'unexpected': True},
])
def test_cleanup_rejects_missing_consent_or_invalid_selection_without_deletion(maintenance_client, payload):
    client = maintenance_client
    identifier = _new_session(client)
    cache_file = config.RAG_CACHE_DIR / 'keep.bin'
    cache_file.write_bytes(b'keep')
    response = client.post('/api/maintenance/cleanup', json=payload)
    assert response.status_code in {400, 409, 422}, response.text
    assert cache_file.read_bytes() == b'keep'
    assert client.app.state.manager.store.directory(identifier).exists()


def test_busy_agent_blocks_entire_cleanup_selection(maintenance_client):
    client = maintenance_client
    identifier = _new_session(client)
    target = client.app.state.manager.sessions[identifier]
    cache_file = config.RAG_CACHE_DIR / 'keep.bin'
    cache_file.write_bytes(b'keep')
    target.forking = True
    try:
        preview = client.get('/api/maintenance')
        assert preview.status_code == 200, preview.text
        assert preview.json()['busy'] is True
        response = _cleanup(client, cache=True, logs=True, sessions=True)
        assert response.status_code == 409, response.text
        assert cache_file.read_bytes() == b'keep'
        assert client.app.state.manager.store.directory(identifier).exists()
    finally:
        target.forking = False


def test_cache_and_log_cleanup_preserves_sessions_and_retains_new_audit_event(maintenance_client):
    client = maintenance_client
    identifier = _new_session(client)
    manager = client.app.state.manager
    cache_file = config.RAG_CACHE_DIR / 'delete.bin'
    cache_file.write_bytes(b'cached')
    config.AUDIT_LOG.write_bytes(b'old-main\n')
    session_log = manager.store.directory(identifier) / 'audit.jsonl'
    session_log.write_bytes(b'old-session\n')
    response = _cleanup(client, cache=True, logs=True)
    assert response.status_code == 200, response.text
    assert not cache_file.exists()
    assert manager.store.directory(identifier).exists()
    assert not session_log.exists() or b'old-session' not in session_log.read_bytes()
    assert b'old-main' not in config.AUDIT_LOG.read_bytes()
    events = [json.loads(line) for line in config.AUDIT_LOG.read_text().splitlines()]
    assert any('maintenance' in item.get('event', '') for item in events)


def test_session_cleanup_archives_records_and_preserves_workspaces_raw_memory_exports(maintenance_client):
    client = maintenance_client
    manager = client.app.state.manager
    identifiers = [_new_session(client, name) for name in ('one', 'two')]
    for identifier in identifiers:
        workspace = manager.workspace(manager.sessions[identifier])
        (workspace / 'keep.txt').write_text(identifier)
    raw_file = config.DOC_DIR / 'source.txt'
    raw_file.write_text('knowledge')
    shared_memory = manager.store.root / 'shared-memory.md'
    shared_memory.write_text('shared memory')
    exports = manager.store.root / 'exports'
    exports.mkdir(exist_ok=True)
    exported = exports / 'keep.json'
    exported.write_text('exported')
    response = _cleanup(client, sessions=True)
    assert response.status_code == 200, response.text
    assert manager.sessions == {}
    for identifier in identifiers:
        assert not manager.store.directory(identifier).exists()
        assert (manager.store.root / '.trash' / identifier / 'session.json').is_file()
        assert (manager.store.workspace_path(identifier) / 'keep.txt').read_text() == identifier
    assert raw_file.read_text() == 'knowledge'
    assert shared_memory.read_text() == 'shared memory'
    assert exported.read_text() == 'exported'


@pytest.mark.parametrize('protected', ['project', 'workspace', 'raw', 'state'])
def test_cache_cleanup_refuses_protected_root_overlap(maintenance_client, monkeypatch, protected):
    client = maintenance_client
    store = client.app.state.manager.store
    path = {'project': config.PROJECT_ROOT, 'workspace': store.workspace_root,
            'raw': config.DOC_DIR, 'state': store.root}[protected]
    path.mkdir(exist_ok=True)
    sentinel = path / 'keep.txt'
    sentinel.write_text('protected')
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', path)
    response = _cleanup(client, cache=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_text() == 'protected'


def test_cache_cleanup_rejects_symlink_root_without_unlinking_outside(maintenance_client, tmp_path, monkeypatch):
    client = maintenance_client
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'keep.txt'
    sentinel.write_text('outside')
    linked = tmp_path / 'linked-cache'
    linked.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', linked)
    response = _cleanup(client, cache=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_text() == 'outside'


@pytest.mark.parametrize('protected', ['workspace', 'raw', 'state'])
def test_log_cleanup_refuses_configured_audit_file_inside_protected_data(maintenance_client, monkeypatch, protected):
    client = maintenance_client
    store = client.app.state.manager.store
    root = {'workspace': store.workspace_root, 'raw': config.DOC_DIR, 'state': store.root}[protected]
    root.mkdir(exist_ok=True)
    sentinel = root / 'keep.txt'
    sentinel.write_text('user data')
    monkeypatch.setattr(config, 'AUDIT_LOG', sentinel)
    response = _cleanup(client, logs=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_text() == 'user data'


@pytest.mark.parametrize('directory', ['src', 'docs', 'tests', '.git'])
def test_cache_cleanup_rejects_project_source_directories(maintenance_client, monkeypatch, directory):
    client = maintenance_client
    root = config.PROJECT_ROOT / directory
    root.mkdir()
    sentinel = root / 'protected.txt'
    sentinel.write_text('project source')
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', root)
    response = _cleanup(client, cache=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_text() == 'project source'


def test_cleanup_waits_for_file_reader_before_clearing_logs_or_archiving_sessions(maintenance_client, monkeypatch):
    from ai_agent_startup.web import app as web_app

    client = maintenance_client
    identifier = _new_session(client)
    manager = client.app.state.manager
    workspace = manager.workspace(manager.sessions[identifier])
    (workspace / 'read.txt').write_text('actual file content')
    entered, release, cleanup_started = threading.Event(), threading.Event(), threading.Event()
    original = web_app.text_preview

    def held_preview(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    def request_cleanup():
        cleanup_started.set()
        return _cleanup(client, logs=True, sessions=True)

    monkeypatch.setattr(web_app, 'text_preview', held_preview)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as requests:
        reader = requests.submit(client.get, f'/api/sessions/{identifier}/file?path=read.txt')
        cleanup = None
        try:
            assert entered.wait(3)
            cleanup = requests.submit(request_cleanup)
            assert cleanup_started.wait(3)
            with pytest.raises(concurrent.futures.TimeoutError):
                cleanup.result(timeout=.15)
        finally:
            release.set()
            read_response = reader.result(timeout=3)
            cleanup_response = cleanup.result(timeout=3) if cleanup is not None else None
        assert read_response.status_code == 200, read_response.text
        assert read_response.json()['content'] == 'actual file content'
        assert cleanup_response is not None and cleanup_response.status_code == 200
        assert identifier not in manager.sessions
        assert (manager.store.root / '.trash' / identifier / 'session.json').is_file()


def test_unwritable_log_preflight_keeps_selected_cache_and_audit_unchanged(maintenance_client, monkeypatch):
    from ai_agent_startup.core import maintenance

    client = maintenance_client
    _new_session(client)
    cache = config.RAG_CACHE_DIR / 'keep.bin'
    cache.write_bytes(b'keep cache')
    config.AUDIT_LOG.write_bytes(b'old audit content\n')
    original = os.open
    attempted = []

    def deny_writable_audit(path, flags, *args, **kwargs):
        if path == config.AUDIT_LOG.name and flags & os.O_ACCMODE == os.O_RDWR:
            attempted.append(path)
            raise PermissionError('simulated read-only audit file')
        return original(path, flags, *args, **kwargs)

    # The wrapper preserves dir_fd support; keep the capability check meaningful.
    monkeypatch.setattr(maintenance.os, 'open', deny_writable_audit)
    monkeypatch.setattr(maintenance.os, 'supports_dir_fd', os.supports_dir_fd | {deny_writable_audit})
    response = _cleanup(client, cache=True, logs=True)
    assert response.status_code in {403, 409}, response.text
    assert attempted
    assert cache.exists(), 'log write access must be checked before deleting the selected cache'
    assert cache.read_bytes() == b'keep cache'
    assert config.AUDIT_LOG.read_bytes() == b'old audit content\n'


@pytest.mark.parametrize('setting', ['DOC_DIR', 'EMBEDDING_LOCAL_PATH'])
def test_cache_cleanup_rejects_protected_data_symlink_alias(maintenance_client, tmp_path, monkeypatch, setting):
    client = maintenance_client
    sentinel = config.RAG_CACHE_DIR / 'keep.bin'
    sentinel.write_bytes(b'protected through alias')
    alias = tmp_path / 'protected-alias'
    alias.symlink_to(config.RAG_CACHE_DIR, target_is_directory=True)
    monkeypatch.setattr(config, setting, alias)
    response = _cleanup(client, cache=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_bytes() == b'protected through alias'


@pytest.mark.parametrize('blocked_operation', ['cache_remove', 'rag_reset'])
def test_repeated_cleanup_cancellation_keeps_maintenance_gate_until_worker_finishes(maintenance_client, monkeypatch, blocked_operation):
    from ai_agent_startup.core import maintenance
    from ai_agent_startup.rag import cache as rag_cache

    client = maintenance_client
    identifier = _new_session(client)
    manager = client.app.state.manager
    sentinel = config.RAG_CACHE_DIR / 'cached.bin'
    sentinel.write_bytes(b'cached')
    entered, release = threading.Event(), threading.Event()
    original_inspect = maintenance.inspect_cache
    original_reset = rag_cache.clear_runtime_caches

    def held_inspect(manager, *, remove=False):
        if remove:
            entered.set()
            assert release.wait(3)
        return original_inspect(manager, remove=remove)

    def held_reset():
        entered.set()
        assert release.wait(3)
        return original_reset()

    if blocked_operation == 'cache_remove':
        monkeypatch.setattr(maintenance, 'inspect_cache', held_inspect)
    else:
        monkeypatch.setattr(rag_cache, 'clear_runtime_caches', held_reset)

    async def run():
        task = asyncio.create_task(maintenance.cleanup(manager, cache=True))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert manager.maintenance, 'repeated cancellation must not reopen the gate while the worker can still delete files'
            with pytest.raises(ValueError):
                manager.create('cannot start before cleanup exits')
            with pytest.raises(ValueError):
                manager.submit(manager.sessions[identifier], 'cannot run before cleanup exits')
            assert not task.done()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await manager.flush()
        assert not manager.maintenance

    client.portal.call(run)


def test_regular_file_trash_rejects_selected_session_cleanup_before_cache_deletion(maintenance_client):
    client = maintenance_client
    identifier = _new_session(client)
    store = client.app.state.manager.store
    trash = store.root / '.trash'
    trash.write_bytes(b'existing user file')
    sentinel = config.RAG_CACHE_DIR / 'keep.bin'
    sentinel.write_bytes(b'keep cache')
    response = _cleanup(client, cache=True, sessions=True)
    assert response.status_code in {403, 409}, response.text
    assert sentinel.read_bytes() == b'keep cache'
    assert trash.read_bytes() == b'existing user file'
    assert store.directory(identifier).is_dir()


def test_preview_reports_one_category_error_without_hiding_other_categories(maintenance_client, monkeypatch):
    client = maintenance_client
    _new_session(client)
    config.AUDIT_LOG.write_bytes(b'available log')
    unsafe_cache = config.PROJECT_ROOT / 'src'
    unsafe_cache.mkdir()
    (unsafe_cache / 'keep.py').write_text('project source')
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', unsafe_cache)
    response = client.get('/api/maintenance')
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['cache']['error']
    assert result['logs']['files'] == 2
    assert result['logs']['bytes'] >= len(b'available log')
    assert result['sessions']['count'] == 1
    assert result['busy'] is False
    assert (unsafe_cache / 'keep.py').read_text() == 'project source'


def test_log_cleanup_clears_known_cli_diagnostic_log(maintenance_client):
    client = maintenance_client
    diagnostic = client.app.state.manager.store.root / 'cli.log'
    diagnostic.write_bytes(b'OLD_DIAGNOSTIC_CONTENT')
    response = _cleanup(client, logs=True)
    assert response.status_code == 200, response.text
    assert not diagnostic.exists() or b'OLD_DIAGNOSTIC_CONTENT' not in diagnostic.read_bytes()


def test_cleanup_rejects_numeric_consent_without_deleting_cache(maintenance_client):
    client = maintenance_client
    sentinel = config.RAG_CACHE_DIR / 'keep.bin'
    sentinel.write_bytes(b'keep cache')
    response = client.post('/api/maintenance/cleanup', json={'confirm': 1, 'cache': True})
    assert response.status_code == 422, response.text
    assert sentinel.read_bytes() == b'keep cache'
