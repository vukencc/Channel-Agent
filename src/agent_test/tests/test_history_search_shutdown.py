"""受控线程夹具验证历史搜索的排空与关闭。"""

import asyncio
import threading

import pytest

from agent_core import config
from agent_core.core.history_context import HistoryContext
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


@pytest.fixture
def manager_env(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, "ENABLE_STRUCTURED_CONTEXT", False)
    monkeypatch.setattr(config, "ENABLE_TASK_PLANS", False)
    monkeypatch.setattr(config, "ENABLE_AGENT_TASKS", False)
    monkeypatch.setattr(config, "ENABLE_COMMAND_JOBS", False)
    store = SessionStore(tmp_path / "state", sandbox_env)
    yield store
    store.close()


def test_shutdown_drains_cancelled_search_without_propagating_read_error(manager_env, monkeypatch):
    async def run():
        manager = SessionManager(manager_env)
        session = manager.create()
        service = HistoryContext(manager, supervisor=object())
        manager.history_context = service
        started, release = threading.Event(), threading.Event()
        flush_started = asyncio.Event()

        async def manifest(_session):
            return {"chunks": []}

        def search_chunks(*args):
            started.set()
            assert release.wait(5)
            assert session.cancelled.is_set()
            raise InterruptedError("controlled cancellation")

        monkeypatch.setattr(service, "_manifest", manifest)
        monkeypatch.setattr("agent_core.core.history_search.search_chunks", search_chunks)
        original_flush = manager.flush

        async def observed_flush():
            flush_started.set()
            await original_flush()

        monkeypatch.setattr(manager, "flush", observed_flush)
        search = asyncio.create_task(service.search(session, "query"))
        assert await asyncio.to_thread(started.wait, 5)
        executor = service.search_executor
        shutdown = asyncio.create_task(manager.shutdown())
        try:
            await asyncio.wait_for(flush_started.wait(), 5)
            session.cancelled.set()
            assert not shutdown.done()
            release.set()
            with pytest.raises(InterruptedError, match="controlled cancellation"):
                await search
            await shutdown
            assert not manager.context_operations
            assert not manager.pending_saves
            assert service.search_executor is None
            assert executor._shutdown
        finally:
            release.set()
            await asyncio.gather(search, shutdown, return_exceptions=True)
            if service.search_executor is not None:
                await service.close()

    asyncio.run(run())


def test_flush_waits_for_running_search(manager_env, monkeypatch):
    async def run():
        manager = SessionManager(manager_env)
        session = manager.create()
        service = HistoryContext(manager, supervisor=object())
        manager.history_context = service
        started, release = threading.Event(), threading.Event()

        async def manifest(_session):
            return {"chunks": []}

        def search_chunks(*args):
            started.set()
            assert release.wait(5)
            return []

        monkeypatch.setattr(service, "_manifest", manifest)
        monkeypatch.setattr("agent_core.core.history_search.search_chunks", search_chunks)
        search = asyncio.create_task(service.search(session, "query"))
        assert await asyncio.to_thread(started.wait, 5)
        flush = asyncio.create_task(manager.flush())
        try:
            await asyncio.sleep(0)
            assert not flush.done()
            release.set()
            assert await search == []
            await flush
            assert not manager.context_operations
            assert not manager.pending_saves
        finally:
            release.set()
            await asyncio.gather(search, flush, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(run())


def test_flush_propagates_pending_writer_error(manager_env):
    async def run():
        manager = SessionManager(manager_env)
        started, release = threading.Event(), threading.Event()

        def write():
            started.set()
            assert release.wait(5)
            raise OSError("controlled write failure")

        future = asyncio.get_running_loop().run_in_executor(manager.store.writer, write)
        manager.pending_saves.add(future)
        assert await asyncio.to_thread(started.wait, 5)
        flush = asyncio.create_task(manager.flush())
        try:
            await asyncio.sleep(0)
            assert not flush.done()
            release.set()
            with pytest.raises(OSError, match="controlled write failure"):
                await flush
        finally:
            release.set()
            await asyncio.gather(flush, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(run())


def test_cancelled_search_coroutine_still_drains_worker(manager_env, monkeypatch):
    async def run():
        manager = SessionManager(manager_env)
        session = manager.create()
        service = HistoryContext(manager, supervisor=object())
        manager.history_context = service
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        async def manifest(_session):
            return {'chunks': []}

        def search_chunks(*args):
            started.set()
            try:
                assert release.wait(5)
                raise InterruptedError('controlled direct cancellation')
            finally:
                finished.set()

        monkeypatch.setattr(service, '_manifest', manifest)
        monkeypatch.setattr('agent_core.core.history_search.search_chunks', search_chunks)
        search = asyncio.create_task(service.search(session, 'query'))
        assert await asyncio.to_thread(started.wait, 5)
        session.cancelled.set()
        search.cancel()
        await asyncio.sleep(0)
        flush = asyncio.create_task(manager.flush())
        try:
            await asyncio.sleep(0)
            assert not flush.done() and not finished.is_set()
            release.set()
            with pytest.raises(InterruptedError, match='controlled direct cancellation'):
                await search
            await flush
            assert finished.is_set() and not manager.context_operations
            assert not manager.pending_saves
        finally:
            release.set()
            await asyncio.gather(search, flush, return_exceptions=True)
            await manager.shutdown()
    asyncio.run(run())
