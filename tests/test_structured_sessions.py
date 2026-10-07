"""End-to-end structured context checks through the real SessionManager loop."""

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY


class SupervisorStub:
    def __init__(self, *, values=None, fail_summary=False):
        self.values = values
        self.fail_summary = fail_summary
        self.inspected = []
        self.summarized = []
        self.settings = SimpleNamespace(check_interval=1, max_chunks=24, variance_threshold=.75,
                                        max_input_chars=12000)

    async def inspect(self, snapshot):
        self.inspected.append(copy.deepcopy(snapshot))
        if self.values == 'alternating':
            values = [float(index % 2) for index in range(len(snapshot['chunks']))]
        elif self.values == 'invalid':
            values = [float('nan')] * len(snapshot['chunks'])
        else:
            values = [0.5] * len(snapshot['chunks'])
        return {'compact': True, 'reason': 'stub', 'values': values}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summarized.append((kind, previous_summary, copy.deepcopy(messages)))
        if self.fail_summary:
            raise RuntimeError('supervisor unavailable')
        return (previous_summary + ' ' if previous_summary else '') + f'{kind} summary'


@pytest.fixture
def store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env', raising=False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 160, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 2, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .05, raising=False)
    value = SessionStore(tmp_path / 'state', sandbox_env)
    yield value
    value.close()


def _seed_history(session, count=8):
    for index in range(count):
        session.record['messages'].extend([
            {'role': 'user', 'content': f'old question {index}: ' + 'Q' * 90},
            {'role': 'assistant', 'content': f'old answer {index}: ' + 'A' * 90},
        ])


def _combined(history):
    return '\n'.join(str(item.get('content', '')) for item in history)


def test_real_loop_sends_four_sections_and_preserves_tool_pair_and_recovery(store, monkeypatch):
    async def run():
        seen = []

        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            if len(seen) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'paired-1', 'type': 'function',
                     'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'sample.txt'})}}]}
            return {'role': 'assistant', 'content': 'finished'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        _seed_history(session)
        supervisor = SupervisorStub()
        manager.history_context.supervisor = supervisor

        async def tool(owner, call):
            assert owner is session
            return 'paired result'

        monkeypatch.setattr(manager, '_tool', tool)
        manager.submit(session, 'current user request')
        await asyncio.wait_for(session.task, 3)
        assert session.record['status'] == 'idle', session.record['error']
        assert len(seen) == 2
        for heading in ('## 系统提示词', '## 当前Session总结', '## History积累', '## 用户输入'):
            assert heading in _combined(seen[0])
        assert 'current user request' in _combined(seen[0])
        assert any(message.get('role') == 'assistant' and message.get('tool_calls')
                   and message['tool_calls'][0]['id'] == 'paired-1' for message in seen[1])
        assert any(message.get('role') == 'tool' and message.get('tool_call_id') == 'paired-1'
                   and message.get('content') == 'paired result' for message in seen[1])
        assert (store.directory(session.id) / 'context' / 'manifest.json').exists()
        session_id = session.id
        await manager.shutdown()

        recovered = []

        async def restarted_model(history, **kwargs):
            recovered.append(copy.deepcopy(history))
            return {'role': 'assistant', 'content': 'resumed'}

        manager2 = SessionManager(store, model=restarted_model)
        try:
            owner = manager2.sessions[session_id]
            manager2.history_context.supervisor = SupervisorStub()
            manager2.submit(owner, 'next request after restart')
            await asyncio.wait_for(owner.task, 3)
            assert owner.record['status'] == 'idle'
            assert recovered and '## 当前Session总结' in _combined(recovered[0])
            assert 'next request after restart' in _combined(recovered[0])
        finally:
            await manager2.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('values,expected_reason', [('alternating', 'value_variance'),
                                                    ('invalid', '')])
def test_supervisor_variance_drives_compaction_but_invalid_scores_do_not(store, monkeypatch,
                                                                           values, expected_reason):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', 2.0)

    async def run():
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model)
        try:
            session = manager.create()
            _seed_history(session)
            supervisor = SupervisorStub(values=values)
            manager.history_context.supervisor = supervisor
            manager.submit(session, 'test variance')
            await asyncio.wait_for(session.task, 3)
            assert session.record['status'] == 'idle', session.record['error']
            assert supervisor.inspected
            metrics = session.record['last_run']['context']
            assert metrics['compact_reason'] == expected_reason
            assert bool(metrics['archived_chunks']) is bool(expected_reason)
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_supervisor_failure_records_explicit_local_fallback_and_still_finishes(store):
    async def run():
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': 'finished'}

        manager = SessionManager(store, model=model)
        try:
            session = manager.create()
            _seed_history(session)
            manager.history_context.supervisor = SupervisorStub(fail_summary=True)
            manager.submit(session, 'continue')
            await asyncio.wait_for(session.task, 3)
            assert session.record['status'] == 'idle'
            metrics = session.record['last_run']['context']
            assert metrics['summary_kind'] == 'extractive_fallback'
            manifest = json.loads((store.directory(session.id) / 'context' / 'manifest.json').read_text())
            assert manifest['summary_kind'] == 'extractive_fallback'
            assert '[本地摘录' in manifest['session_summary']
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_real_loop_checkpoints_before_model_when_current_input_exceeds_reserve(store):
    async def run():
        calls = []

        async def model(history, **kwargs):
            calls.append(history)
            return {'role': 'assistant', 'content': 'should not run'}

        manager = SessionManager(store, model=model)
        try:
            session = manager.create()
            manager.history_context.supervisor = SupervisorStub()
            request = 'R' * 100_000
            manager.submit(session, request)
            await asyncio.wait_for(session.task, 3)
            assert session.record['status'] == 'checkpoint'
            assert session.record['last_run']['stop_reason'] == 'context_budget'
            assert calls == []
            assert any(message.get('role') == 'user' and message.get('content') == request
                       for message in session.record['messages'])
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_history_search_via_real_tool_does_not_deadlock_single_default_worker(store):
    """The history tool's worker must not wait on work queued to itself."""
    from ai_agent_startup.tools import context_history

    previous = dict(TOOL_REGISTRY)
    importlib.reload(context_history)

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='single-default')
        try:
            session = manager.create()
            _seed_history(session)
            manager.history_context.supervisor = SupervisorStub()
            await manager.history_context.prepare(session,
                session.record['messages'] + [{'role': 'user', 'content': 'current request'}])
            # Warm the tokenizer before measuring executor scheduling.
            assert await manager.history_context.search(session, 'chunk', limit=1)
            loop = asyncio.get_running_loop()
            loop.set_default_executor(executor)
            call = {'id': 'search-1', 'function': {'name': 'history_search',
                    'arguments': json.dumps({'query': 'chunk', 'limit': 1})}}
            task = asyncio.create_task(manager._tool(session, call))
            timed_out = False
            try:
                result = await asyncio.wait_for(asyncio.shield(task), .5)
            except TimeoutError:
                timed_out = True
                # Controlled rescue for the known broken implementation, so pytest cannot hang.
                executor._max_workers = 2
                executor._adjust_thread_count()
                result = await asyncio.wait_for(asyncio.shield(task), 2)
            assert not timed_out, 'history_search blocked on its own single-worker executor'
            assert json.loads(result)
        finally:
            executor.shutdown(wait=True)
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(previous)


def test_cancelled_history_search_remains_registered_until_worker_finishes(store, monkeypatch):
    from ai_agent_startup.core import history_search

    started = threading.Event()
    release = threading.Event()

    def slow_search(*args):
        started.set()
        release.wait(timeout=3)
        return []

    monkeypatch.setattr(history_search, 'search_chunks', slow_search)

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        flush_task = None
        try:
            session = manager.create()
            _seed_history(session)
            manager.history_context.supervisor = SupervisorStub()
            await manager.history_context.prepare(session,
                session.record['messages'] + [{'role': 'user', 'content': 'current request'}])
            task = asyncio.create_task(manager.history_context.search(session, 'chunk'))
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            flush_task = asyncio.create_task(manager.flush())
            await asyncio.sleep(.05)
            assert not flush_task.done(), 'flush returned while an actual archive read was still running'
        finally:
            release.set()
            if flush_task is not None:
                await asyncio.wait_for(flush_task, 3)
            if 'task' in locals():
                await asyncio.gather(task, return_exceptions=True)
            await manager.shutdown()

    asyncio.run(run())


def test_history_tool_propagates_completed_future_timeout_without_hot_loop(store, monkeypatch):
    """A future's TimeoutError is its result, not another wait timeout."""
    from ai_agent_startup.tools import context_history

    previous = dict(TOOL_REGISTRY)
    importlib.reload(context_history)

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        try:
            session = manager.create()

            async def raise_timeout(*args, **kwargs):
                raise TimeoutError('completed search failed')

            monkeypatch.setattr(manager.history_context, 'search', raise_timeout)
            call = {'id': 'timeout-1', 'function': {'name': 'history_search',
                    'arguments': json.dumps({'query': 'needle'})}}
            task = asyncio.create_task(manager._tool(session, call))
            await asyncio.sleep(.12)
            completed = task.done()
            if not completed:
                # Rescue the broken spin loop without leaving a runaway worker behind.
                session.cancelled.set()
            outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2))[0]
            assert completed, 'completed future TimeoutError was mistaken for a polling timeout'
            assert isinstance(outcome, str) and outcome.startswith('[超时]')
        finally:
            await manager.shutdown()

    try:
        asyncio.run(run())
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(previous)


def test_cancel_during_manifest_io_never_starts_supervisor_or_publishes(store):
    started = threading.Event()
    release = threading.Event()

    def block_writer():
        started.set()
        release.wait(timeout=3)

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        try:
            session = manager.create()
            _seed_history(session)
            supervisor = SupervisorStub()
            manager.history_context.supervisor = supervisor
            writer = store.writer.submit(block_writer)
            assert await asyncio.to_thread(started.wait, 1)
            task = asyncio.create_task(manager.history_context.prepare(session,
                session.record['messages'] + [{'role': 'user', 'content': 'current request'}]))
            await asyncio.sleep(.03)
            session.cancelled.set()
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wrap_future(writer)
            await manager.flush()
            assert supervisor.inspected == []
            assert supervisor.summarized == []
            assert not (store.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            release.set()
            await manager.shutdown()

    asyncio.run(run())


def test_whole_compaction_uses_one_supervisor_deadline_and_local_fallback(store):
    class SlowSupervisor(SupervisorStub):
        def __init__(self):
            super().__init__()
            self.settings.timeout = .09
            self.calls = []

        async def summarize(self, previous_summary, messages, *, kind='session'):
            self.calls.append((time.monotonic(), kind))
            await asyncio.sleep(.05)
            return f'{kind} summary'

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        try:
            session = manager.create()
            _seed_history(session, count=10)
            supervisor = SlowSupervisor()
            manager.history_context.supervisor = supervisor
            await asyncio.wait_for(manager.history_context.prepare(session,
                session.record['messages'] + [{'role': 'user', 'content': 'current request'}]), 3)
            assert len(supervisor.calls) <= 3, 'one timeout must bound the entire compact batch'
            manifest = json.loads((store.directory(session.id) / 'context' / 'manifest.json').read_text())
            assert manifest['chunks']
            assert any(item['summary_kind'] == 'extractive_fallback' for item in manifest['chunks'])
            assert all((store.directory(session.id) / 'context' / 'chunks' / (item['ID'] + '.json')).exists()
                       for item in manifest['chunks'])
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('enabled', [False, True])
def test_named_history_knowledge_base_keeps_legacy_rag_route(tmp_path, monkeypatch, enabled):
    rag_tool = importlib.import_module('ai_agent_startup.tools.rag_search')
    directory = tmp_path / 'history-kb'
    directory.mkdir()
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', enabled, raising=False)
    monkeypatch.setattr(config, 'RAG_SOURCES', {'history': {'path': str(directory)}})
    seen = []

    def index(*, root, **kwargs):
        assert root == directory
        seen.append(('index', root))
        return 'legacy-index'

    def search(query, *, strictness, breadth, index, **kwargs):
        seen.append(('search', query, index))
        return 'legacy KB result'

    monkeypatch.setattr(rag_tool, 'get_index', index)
    monkeypatch.setattr(rag_tool, '_rag_search', search)
    monkeypatch.setattr(rag_tool, 'audit', lambda *args, **kwargs: None)
    assert rag_tool.rag_search('needle', source='history') == 'legacy KB result'
    assert seen == [('index', directory), ('search', 'needle', 'legacy-index')]


def test_structured_private_metadata_is_stripped_before_sdk_request(store, monkeypatch):
    from ai_agent_startup.core import llm

    async def run():
        manager = SessionManager(store, model=lambda *a, **k: None)
        try:
            session = manager.create()
            _seed_history(session)
            manager.history_context.supervisor = SupervisorStub()
            history, _ = await manager.history_context.prepare(session,
                session.record['messages'] + [{'role': 'user', 'content': 'current request'}])
            assert any('_context_reference' in item for item in history)
            captured = {}

            class FakeCompletions:
                async def create(self, **kwargs):
                    captured.update(kwargs)
                    return 'fake stream'

            class FakeClient:
                chat = SimpleNamespace(completions=FakeCompletions())

                def with_options(self, **kwargs):
                    return self

            monkeypatch.setattr(llm, 'selected_model', lambda: 'fake-model')
            monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: FakeClient())
            monkeypatch.setattr(llm, 'model_options', lambda: {})
            monkeypatch.setattr(llm, 'selected_schemas', lambda schemas: [])
            monkeypatch.setattr(llm, 'reserved_cost', lambda *args: None)

            async def no_ticket(*args):
                return None

            monkeypatch.setattr(llm, 'reserve_request', no_ticket)
            monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
            assert await llm._open_stream(history, session_id=session.id) == 'fake stream'
            assert captured['messages']
            assert all(not any(key.startswith('_') for key in item) for item in captured['messages'])
            assert any('_context_reference' in item for item in history)
        finally:
            await manager.shutdown()

    asyncio.run(run())
