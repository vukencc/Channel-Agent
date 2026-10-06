"""会话删除仅归档选定记录，保留工作区和其他会话。"""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_delete_session_moves_state_preserves_workspace_and_does_not_restore_on_restart(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            target, other = manager.create('delete'), manager.create('keep')
            await manager.flush()
            workspace = store.workspace(target.id)
            (workspace / 'user.txt').write_text('keep user content')
            store.remember(target.id, 'session memory')
            archived = await manager.delete_session(target)
            assert archived == store.root / '.trash' / target.id
            assert json.loads((archived / 'session.json').read_text())['id'] == target.id
            assert (archived / 'memory.md').read_text()
            assert not store.directory(target.id).exists()
            assert (workspace / 'user.txt').read_text() == 'keep user content'
            assert other.id in manager.sessions and target.id not in manager.sessions
            with pytest.raises(ValueError):
                manager.submit(target, 'must not recreate')
            with pytest.raises(ValueError):
                manager.save(target)
            restored = SessionManager(store)
            assert set(restored.sessions) == {other.id}
            await restored.shutdown()
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_delete_rejects_busy_related_queued_tasks_and_active_tool_workers(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            parent, child = manager.create(), manager.create()
            child.record['delegated_from'] = parent.id
            child.forking = True
            with pytest.raises(ValueError):
                await manager.delete_session(parent)
            with pytest.raises(ValueError):
                await manager.delete_session(child)
            child.forking = False
            from ai_agent_startup.core.agent_tasks import AgentTasks
            manager.agent_tasks = AgentTasks(manager, require_budget_config=False)
            await manager.flush()
            identifier = await manager.agent_tasks.start(parent, 'queued work', [], 1, delay_seconds=60)
            with pytest.raises(ValueError):
                await manager.delete_session(parent)
            manager.agent_tasks.cancel(parent.id, identifier)
            await asyncio.gather(manager.agent_tasks.tasks[identifier], return_exceptions=True)
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_delete_waits_for_pending_save_before_archiving(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        release, entered = threading.Event(), threading.Event()
        try:
            target = manager.create()
            await manager.flush()
            def hold_writer():
                entered.set()
                release.wait(2)
            blocker = asyncio.get_running_loop().run_in_executor(store.writer, hold_writer)
            await asyncio.to_thread(entered.wait, 1)
            target.record['messages'].append({'role': 'user', 'content': 'saved before deletion'})
            manager.save(target)
            deletion = asyncio.create_task(manager.delete_session(target))
            await asyncio.sleep(.02)
            assert not deletion.done()
            release.set()
            await blocker
            archived = await deletion
            record = store.read_record(archived / 'session.json')
            assert record['messages'][-1]['content'] == 'saved before deletion'
            assert not store.directory(target.id).exists()
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_archive_refuses_symlink_trash_and_preserves_legacy_workspace(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        target = store.create('legacy', 'system')
        legacy = store.directory(target['id']) / 'workspace'
        legacy.mkdir()
        (legacy / 'file').write_text('keep')
        with pytest.raises(ValueError):
            store.delete_session(target['id'])
        assert (legacy / 'file').read_text() == 'keep'
        external = tmp_path / 'external'
        external.mkdir()
        (store.root / '.trash').symlink_to(external, target_is_directory=True)
        with pytest.raises(ValueError):
            store.delete_session(target['id'])
        assert store.directory(target['id']).exists()
        assert not list(external.iterdir())
    finally:
        store.close()


def test_deletion_rechecks_related_activity_after_waiting_for_saves(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        release = threading.Event()
        try:
            parent, child = manager.create(), manager.create()
            child.record['delegated_from'] = parent.id
            await manager.flush()
            blocker = asyncio.get_running_loop().run_in_executor(store.writer, release.wait, 2)
            manager.save(parent)
            deletion = asyncio.create_task(manager.delete_session(parent))
            await asyncio.sleep(.02)
            child.forking = True
            release.set()
            await blocker
            with pytest.raises(ValueError):
                await deletion
            assert parent.id in manager.sessions and store.directory(parent.id).exists()
            child.forking = False
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_delete_rejects_timed_out_read_tool_still_working(tmp_path, monkeypatch):
    from pydantic import BaseModel
    from ai_agent_startup import config
    from ai_agent_startup.tools.base import TOOL_REGISTRY, Tool

    class EmptyArgs(BaseModel):
        pass

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        release = threading.Event()
        monkeypatch.setattr(config, 'TOOL_TIMEOUT', .02)
        monkeypatch.setitem(TOOL_REGISTRY, 'deletion_probe', Tool('deletion_probe', EmptyArgs,
            lambda: release.wait(2), concurrency='read'))
        try:
            target = manager.create()
            await manager.flush()
            result = await manager._tool(target, {'id': 'probe', 'function': {
                'name': 'deletion_probe', 'arguments': '{}'}})
            assert '超时' in result
            with pytest.raises(ValueError):
                await manager.delete_session(target)
            release.set()
            await asyncio.gather(*list(target.tool_workers))
            await asyncio.sleep(0)
            assert await manager.delete_session(target)
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_delete_rejects_workspace_directory_overlapping_session_state(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'state')
    try:
        record = store.create('overlap', 'system')
        workspace = store.workspace(record['id'])
        (workspace / 'user.txt').write_text('keep')
        with pytest.raises(ValueError):
            store.delete_session(record['id'])
        assert (workspace / 'user.txt').read_text() == 'keep'
    finally:
        store.close()


def test_related_background_command_blocks_session_deletion(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            parent, child = manager.create(), manager.create()
            child.record['delegated_from'] = parent.id
            await manager.flush()
            queue = SimpleNamespace(records={'job': {'owner': parent.id, 'status': 'running'}},
                                    lock=threading.RLock(), futures={})
            manager.command_jobs = queue
            with pytest.raises(ValueError):
                await manager.delete_session(child)
            assert store.directory(child.id).exists()
        finally:
            manager.command_jobs = None
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_completed_background_command_still_auditing_blocks_deletion(tmp_path):
    import concurrent.futures

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            target = manager.create()
            await manager.flush()
            finalizing = concurrent.futures.Future()
            queue = SimpleNamespace(records={'job': {'owner': target.id, 'status': 'completed'}},
                                    lock=threading.RLock(), futures={'job': finalizing})
            manager.command_jobs = queue
            with pytest.raises(ValueError):
                await manager.delete_session(target)
        finally:
            manager.command_jobs = None
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_delete_waits_for_memory_candidate_atomic_write(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        entered, release = threading.Event(), threading.Event()
        original = store.atomic_write

        def held_write(path, body):
            if path.name == 'memory-candidates.json':
                entered.set()
                assert release.wait(3)
            return original(path, body)

        async def complete(prompt, **kwargs):
            return '["saved candidate"]'

        monkeypatch.setattr(store, 'atomic_write', held_write)
        monkeypatch.setattr(manager, '_complete', complete)
        deletion = None
        try:
            target = manager.create()
            await manager.flush()
            manager._start_memory_candidates(target, 'query', 'answer')
            assert await asyncio.to_thread(entered.wait, 2)
            deletion = asyncio.create_task(manager.delete_session(target))
            await asyncio.sleep(.04)
            waited_for_writer = not deletion.done()
            release.set()
            archive = await asyncio.wait_for(deletion, 3)
            assert waited_for_writer, '删除必须等原子写线程结束，不能只取消其 await'
            assert (archive / 'memory-candidates.json').read_text() == '["saved candidate"]'
            assert not store.directory(target.id).exists()
        finally:
            release.set()
            if deletion:
                await asyncio.gather(deletion, return_exceptions=True)
            await manager.shutdown()
            store.close()

    asyncio.run(run())
