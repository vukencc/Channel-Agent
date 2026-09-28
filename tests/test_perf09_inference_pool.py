import asyncio
import threading

from pydantic import BaseModel

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.base import Tool
from ai_agent_startup.tools.sandbox import sandbox_root


class EmptyArgs(BaseModel):
    pass


def call(name, identifier='call'):
    return {'id': identifier, 'function': {'name': name, 'arguments': '{}'}}


def test_rag_pool_does_not_take_file_slots_and_preserves_context(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_INFERENCE_CONCURRENCY', 1, raising=False)
    monkeypatch.setattr(config, 'TOOL_CONCURRENCY', 1)
    started, release = threading.Event(), threading.Event()
    threads = []
    def inference():
        threads.append(threading.current_thread().name)
        started.set()
        assert release.wait(3)
        return str(sandbox_root())
    monkeypatch.setitem(TOOL_REGISTRY, 'rag_search', Tool('rag_search', EmptyArgs, inference, concurrency='read'))
    monkeypatch.setitem(TOOL_REGISTRY, 'probe', Tool('probe', EmptyArgs, lambda: '可用', concurrency='read'))
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        session = manager.create()
        task = asyncio.create_task(manager._tool(session, call('rag_search')))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert await asyncio.wait_for(manager._tool(session, call('probe')), .5) == '可用'
        finally:
            release.set()
            result = await task
            await manager.shutdown()
            store.close()
        assert result == str(store.workspace_path(session.id))
        assert threads[0].startswith('rag-inference')
    asyncio.run(run())


def test_timed_out_rag_keeps_inference_slot_until_worker_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RAG_INFERENCE_CONCURRENCY', 1, raising=False)
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    count = 0
    def inference():
        nonlocal count
        count += 1
        if count == 1:
            started.set()
            assert release.wait(3)
        else:
            second_started.set()
        return '结束'
    monkeypatch.setitem(TOOL_REGISTRY, 'rag_search', Tool('rag_search', EmptyArgs, inference, timeout_s=.05, concurrency='read'))
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
        manager = SessionManager(store)
        session = manager.create()
        try:
            result = await manager._tool(session, call('rag_search', 'first'))
            assert started.is_set() and '[超时]' in result
            second = asyncio.create_task(manager._tool(session, call('rag_search', 'second')))
            await asyncio.sleep(.1)
            assert not second_started.is_set()
        finally:
            release.set()
            if 'second' in locals():
                await second
            await manager.shutdown()
            store.close()
    asyncio.run(run())
