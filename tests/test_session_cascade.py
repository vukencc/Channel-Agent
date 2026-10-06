"""User deletion archives delegated descendants while preserving independent forks."""
import asyncio
import concurrent.futures
import json
import threading
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def _tree(manager):
    root = manager.create('root')
    child = manager.create('child', parent=root, workspace_mode='shared')
    descendant = manager.create('older nested descendant')
    # Persisted/imported hierarchies may contain deeper relations than new delegation allows.
    descendant.record['delegated_from'] = child.id
    manager.save(descendant)
    unrelated = manager.create('unrelated')
    branch = manager.create('independent manual branch')
    branch.record['branch'] = {'parent_id': root.id, 'through': 0}
    manager.save(branch)
    return root, child, descendant, unrelated, branch


def test_cascade_archives_all_delegated_descendants_and_preserves_independent_sessions(store):
    async def run():
        manager = SessionManager(store)
        try:
            root, child, descendant, unrelated, branch = _tree(manager)
            workspace = manager.workspace(root)
            (workspace / 'keep.txt').write_text('project files')
            await manager.flush()
            archived = await manager.delete_session(root, cascade=True)
            assert archived == store.root / '.trash' / root.id
            assert set(manager.sessions) == {unrelated.id, branch.id}
            for value in (root, child, descendant):
                assert not store.directory(value.id).exists()
                path = store.root / '.trash' / value.id / 'session.json'
                assert json.loads(path.read_text())['id'] == value.id
            assert (workspace / 'keep.txt').read_text() == 'project files'
            restored = SessionManager(store)
            try:
                assert set(restored.sessions) == {unrelated.id, branch.id}
            finally:
                await restored.shutdown()
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('activity', ['forking', 'tool', 'command_audit'])
def test_busy_descendant_blocks_entire_cascade_without_archiving_any_record(store, activity):
    async def run():
        manager = SessionManager(store)
        root, child, descendant, unrelated, branch = _tree(manager)
        pending = None
        try:
            await manager.flush()
            if activity == 'forking':
                descendant.forking = True
            elif activity == 'tool':
                pending = concurrent.futures.Future()
                descendant.tool_workers.add(pending)
            else:
                pending = concurrent.futures.Future()
                manager.command_jobs = SimpleNamespace(
                    records={'job': {'owner': descendant.id, 'status': 'completed'}},
                    futures={'job': pending}, lock=threading.RLock())
            with pytest.raises(ValueError):
                await manager.delete_session(root, cascade=True)
            assert set(manager.sessions) == {root.id, child.id, descendant.id, unrelated.id, branch.id}
            assert all(store.directory(value.id).exists() for value in manager.sessions.values())
            assert not (store.root / '.trash').exists()
        finally:
            descendant.forking = False
            descendant.tool_workers.clear()
            manager.command_jobs = None
            if pending is not None:
                pending.cancel()
            await manager.shutdown()
    asyncio.run(run())


def test_cascade_waits_for_descendant_save_before_archiving(store):
    async def run():
        manager = SessionManager(store)
        root, child, descendant, unrelated, branch = _tree(manager)
        entered, release = threading.Event(), threading.Event()
        deletion = None
        try:
            await manager.flush()
            def held_writer():
                entered.set()
                release.wait(3)
            blocker = asyncio.get_running_loop().run_in_executor(store.writer, held_writer)
            assert await asyncio.to_thread(entered.wait, 2)
            descendant.record['messages'].append({'role': 'user', 'content': 'save descendant before archive'})
            manager.save(descendant)
            deletion = asyncio.create_task(manager.delete_session(root, cascade=True))
            await asyncio.sleep(.03)
            assert not deletion.done()
            release.set()
            await blocker
            await asyncio.wait_for(deletion, 3)
            record = store.read_record(store.root / '.trash' / descendant.id / 'session.json')
            assert record['messages'][-1]['content'] == 'save descendant before archive'
            assert set(manager.sessions) == {unrelated.id, branch.id}
        finally:
            release.set()
            if deletion is not None:
                await asyncio.gather(deletion, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(run())


def test_web_user_delete_returns_all_archived_descendant_ids(store):
    pytest.importorskip('fastapi')
    from fastapi.testclient import TestClient
    from ai_agent_startup.web.app import create_app

    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': 'done'}

    app = create_app(store=store, token='cascade-test', model=model)
    with TestClient(app, base_url='http://localhost',
                    headers={'Authorization': 'Bearer cascade-test'}) as client:
        manager = client.app.state.manager
        root, child, descendant, unrelated, branch = _tree(manager)
        response = client.request('DELETE', f'/api/sessions/{root.id}', json={'confirm': True})
        assert response.status_code == 200, response.text
        assert set(response.json()['deleted_ids']) == {root.id, child.id, descendant.id}
        assert set(manager.sessions) == {unrelated.id, branch.id}
        assert all((store.root / '.trash' / value.id / 'session.json').is_file()
                   for value in (root, child, descendant))


def test_descendant_archive_conflict_rejects_cascade_before_moving_any_record(store):
    async def run():
        manager = SessionManager(store)
        try:
            root, child, descendant, unrelated, branch = _tree(manager)
            await manager.flush()
            conflict = store.root / '.trash' / descendant.id
            conflict.mkdir(parents=True)
            (conflict / 'keep.txt').write_text('older archive')
            with pytest.raises(ValueError):
                await manager.delete_session(root, cascade=True)
            assert set(manager.sessions) == {root.id, child.id, descendant.id, unrelated.id, branch.id}
            assert all(store.directory(value.id).exists() for value in manager.sessions.values())
            assert (conflict / 'keep.txt').read_text() == 'older archive'
            assert not (store.root / '.trash' / root.id).exists()
            assert not (store.root / '.trash' / child.id).exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_cascade_drains_descendant_memory_write_before_archiving(store, monkeypatch):
    async def run():
        manager = SessionManager(store)
        root, child, descendant, unrelated, branch = _tree(manager)
        entered, release = threading.Event(), threading.Event()
        original = store.atomic_write
        deletion = None

        def held_write(path, body):
            if path == store.directory(descendant.id) / 'memory-candidates.json':
                entered.set()
                assert release.wait(3)
            return original(path, body)

        async def complete(prompt, **kwargs):
            return '["candidate before archive"]'

        monkeypatch.setattr(store, 'atomic_write', held_write)
        monkeypatch.setattr(manager, '_complete', complete)
        try:
            await manager.flush()
            manager._start_memory_candidates(descendant, 'query', 'answer')
            assert await asyncio.to_thread(entered.wait, 2)
            deletion = asyncio.create_task(manager.delete_session(root, cascade=True))
            await asyncio.sleep(.03)
            assert not deletion.done()
            release.set()
            await asyncio.wait_for(deletion, 3)
            path = store.root / '.trash' / descendant.id / 'memory-candidates.json'
            assert path.read_text() == '["candidate before archive"]'
            assert not store.directory(descendant.id).exists()
        finally:
            release.set()
            if deletion is not None:
                await asyncio.gather(deletion, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(run())
