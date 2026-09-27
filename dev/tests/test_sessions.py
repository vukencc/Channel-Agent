import asyncio
import json
import threading

import pytest

import config
from core.sessions import SessionManager
from core.storage import SessionStore
from tools import TOOL_REGISTRY
from tools.base import Tool
from pydantic import BaseModel


@pytest.fixture
def store(tmp_path):
    value = SessionStore(tmp_path / 'state', workspace_root=tmp_path / 'crud_tests')
    yield value
    value.close()


def test_storage_restore_memory_export_and_lock(store):
    record = store.create('测试', 'system')
    record['messages'].append({'role': 'user', 'content': 'hello'})
    store.save(record)
    store.remember(record['id'], '偏好中文')
    assert store.load_all()[0]['messages'][-1]['content'] == 'hello'
    assert '偏好中文' in store.memory(record['id'])
    exported = json.loads(store.export(record, 'json').read_text())
    assert exported['messages'] == record['messages']
    assert '偏好中文' in exported['memory']
    assert 'hello' in store.export(record).read_text()
    with pytest.raises(RuntimeError, match='已有 CLI'):
        SessionStore(store.root)
    with pytest.raises(ValueError):
        store.directory('../escape')


def test_interrupted_tool_not_replayed_and_corrupt_file_preserved(store):
    record = store.create('recover', 'system')
    record.update(status='running')
    record['messages'].append({'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'a', 'function': {'name': 'create_file', 'arguments': '{}'}}]})
    store.save(record)
    recovered = store.load_all()[0]
    assert recovered['status'] == 'interrupted'
    assert recovered['messages'][-1]['tool_call_id'] == 'a'
    assert '结果未知' in recovered['messages'][-1]['content']
    path = store.directory(record['id']) / 'session.json'
    path.write_text('broken')
    assert store.load_all() == []
    assert path.read_text() == 'broken'
    assert store.errors


def test_concurrent_agents_independent_context_and_memory(store, monkeypatch):
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    async def run():
        started = set()
        both = asyncio.Event()
        async def model(history, *, session_id, emit):
            started.add(session_id)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
            assert ('记忆甲' in history[0]['content']) == (history[-1]['content'] == '甲')
            emit('content', history[-1]['content'])
            return {'role': 'assistant', 'content': history[-1]['content']}
        manager = SessionManager(store, model=model)
        a, b = manager.create('a'), manager.create('b')
        store.remember(a.id, '记忆甲')
        manager.submit(a, '甲')
        manager.submit(b, '乙')
        with pytest.raises(ValueError, match='正在运行'):
            manager.submit(a, '重复')
        await asyncio.gather(a.task, b.task)
        assert a.record['messages'][-1]['content'] == '甲'
        assert b.record['messages'][-1]['content'] == '乙'
        assert len(started) == 2
    asyncio.run(run())


def test_threaded_crud_confirmations_and_workspaces_are_isolated(store, monkeypatch):
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    async def run():
        async def model(history, *, session_id, emit):
            if history[-1]['role'] == 'tool':
                return {'role': 'assistant', 'content': history[-1]['content']}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'write', 'function': {'name': 'create_file', 'arguments': json.dumps({
                    'path': 'same.txt', 'content': history[-1]['content']})}}]}
        manager = SessionManager(store, model=model)
        a, b = manager.create(), manager.create()
        manager.submit(a, 'first')
        manager.submit(b, 'second')
        async def wait_confirmations():
            while not (a.confirmation and b.confirmation):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_confirmations(), 2)
        manager.decide(a, True)
        manager.decide(b, False)
        await asyncio.gather(a.task, b.task)
        assert (store.workspace_path(a.id) / 'same.txt').read_text() == 'first'
        assert not (store.workspace_path(b.id) / 'same.txt').exists()
        assert '[已取消]' in b.record['messages'][-1]['content']
        assert not (store.workspace_path(a.id) / 'session.json').exists()
    asyncio.run(run())


def test_cancel_pending_confirmation_and_restart_context(store, monkeypatch):
    async def run():
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'write', 'function': {'name': 'create_file', 'arguments': '{"path":"no.txt"}'}}]}
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, 'write')
        async def waiting():
            while not session.confirmation:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(waiting(), 2)
        manager.cancel(session)
        await session.task
        assert session.record['status'] == 'cancelled'
        assert not (store.workspace_path(session.id) / 'no.txt').exists()
        recovered = SessionManager(store)
        assert recovered.sessions[session.id].record['messages'] == session.record['messages']
    asyncio.run(run())


def test_blocking_tools_run_on_distinct_threads(store, monkeypatch):
    barrier = threading.Barrier(2, timeout=2)
    threads = set()
    class Args(BaseModel):
        pass
    def worker():
        threads.add(threading.get_ident())
        barrier.wait()
        return 'done'
    monkeypatch.setitem(TOOL_REGISTRY, 'thread_probe', Tool('thread_probe', Args, worker))
    async def run():
        async def model(history, **kwargs):
            if history[-1]['role'] == 'tool':
                return {'role': 'assistant', 'content': 'done'}
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'probe', 'function': {'name': 'thread_probe', 'arguments': '{}'}}]}
        manager = SessionManager(store, model=model)
        a, b = manager.create(), manager.create()
        manager.submit(a, 'a'); manager.submit(b, 'b')
        await asyncio.gather(a.task, b.task)
        assert a.record['status'] == b.record['status'] == 'idle'
        assert len(threads) == 2
        assert threading.get_ident() not in threads
    asyncio.run(run())


def test_cancel_queued_session_does_not_cancel_another(store, monkeypatch):
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 1)
    async def run():
        release = asyncio.Event()
        started = asyncio.Event()
        seen = []
        async def model(history, *, session_id, emit):
            seen.append(session_id)
            started.set()
            await release.wait()
            return {'role': 'assistant', 'content': 'ok'}
        manager = SessionManager(store, model=model)
        a, b = manager.create(), manager.create()
        manager.submit(a, 'a')
        await started.wait()
        manager.submit(b, 'b')
        manager.cancel(b)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(a.task, b.task, return_exceptions=True)
        assert a.record['status'] == 'idle'
        assert b.record['status'] == 'cancelled'
        assert seen == [a.id]
    asyncio.run(run())


def test_stream_failure_preserves_partial_and_allows_next_turn(store):
    async def run():
        async def model(history, *, session_id, emit):
            emit('content', 'partial')
            raise RuntimeError('offline')
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, 'hello')
        await session.task
        assert session.record['status'] == 'error'
        assert session.record['messages'][-1]['content'] == 'partial'
        assert store.load_all()[0]['messages'][-1]['content'] == 'partial'
    asyncio.run(run())


def test_background_assessment_does_not_block_next_turn_or_overwrite_it(store, monkeypatch):
    from core import sessions
    monkeypatch.setattr(config, 'RAG_ASSESS', True)
    async def run():
        assessing = asyncio.Event()
        async def slow_assess(*args):
            assessing.set()
            await asyncio.sleep(10)
            return 'stale assessment'
        monkeypatch.setattr(sessions, 'assess_rag', slow_assess)
        async def model(history, **kwargs):
            if history[-1]['content'] == 'search':
                return {'role':'assistant','content':'','tool_calls':[{'id':'r','function':{'name':'rag_search','arguments':'{}'}}]}
            return {'role':'assistant','content':'answer'}
        manager = SessionManager(store, model=model)
        async def tool(*args):
            return 'retrieval context'
        monkeypatch.setattr(manager, '_tool', tool)
        s = manager.create()
        manager.submit(s, 'search')
        await asyncio.wait_for(s.task, 1)
        await asyncio.wait_for(assessing.wait(), 1)
        assert not s.busy
        manager.submit(s, 'continue')
        await asyncio.wait_for(s.task, 1)
        assert s.record['messages'][-1]['content'] == 'answer'
        assert 'assessment' not in s.record
        await manager.shutdown()
    asyncio.run(run())


def test_assessment_timeout_is_nonfatal_and_persisted(store, monkeypatch):
    from core import sessions
    monkeypatch.setattr(config, 'ASSESS_TIMEOUT', .02)
    async def run():
        async def stalled(*args):
            await asyncio.sleep(10)
        monkeypatch.setattr(sessions, 'assess_rag', stalled)
        manager = SessionManager(store)
        session = manager.create()
        manager._start_assessment(session, 'query', ['context'], 'answer')
        await asyncio.wait_for(session.assessment_task, 1)
        assert session.record['status'] == 'idle'
        assert 'TimeoutError' in session.record['assessment']
        assert store.load_all()[0]['assessment'] == session.record['assessment']
        await manager.shutdown()
    asyncio.run(run())
