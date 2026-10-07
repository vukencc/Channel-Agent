"""Contract tests for opt-in, persistent structured conversation context."""

import asyncio
import copy
import json
import os
import re

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


class SupervisorStub:
    def __init__(self, *, compact=True):
        self.compact = compact
        self.inspections = []
        self.summaries = []

    async def inspect(self, snapshot):
        self.inspections.append(copy.deepcopy(snapshot))
        return {'compact': self.compact, 'reason': 'test decision', 'values': [1.0] * len(snapshot.get('chunks', []))}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summaries.append((kind, previous_summary, copy.deepcopy(messages)))
        if kind == 'chunk':
            return 'independent chunk summary'
        return (previous_summary + ' | ' if previous_summary else '') + 'session summary'


@pytest.fixture
def context_env(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env', raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 160, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 2, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 2000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_CHARS', 4000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_CHARS', 8000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_TOKENS', 1000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_TOKENS', 2000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', 0.05, raising=False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _messages(groups=6):
    result = [{'role': 'system', 'content': 'System rules remain authoritative.'}]
    for index in range(groups):
        result.extend((
            {'role': 'user', 'content': f'question-{index} ' + 'q' * 90},
            {'role': 'assistant', 'content': f'answer-{index} ' + 'a' * 90},
        ))
    result.append({'role': 'user', 'content': 'current request: final question'})
    return result


def _service(manager, supervisor=None):
    from ai_agent_startup.core.history_context import HistoryContext
    return HistoryContext(manager, supervisor=supervisor or SupervisorStub())


async def _manager(store):
    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'unused'}
    manager = SessionManager(store, model=model)
    return manager, manager.create()


def test_prepare_four_sections_preserves_current_tool_protocol_and_input(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            messages = _messages(3)
            messages[-1:-1] = [
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call-1', 'type': 'function', 'function': {'name': 'file_read', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'paired result'},
            ]
            original = copy.deepcopy(messages)
            history, metrics = await _service(manager, SupervisorStub(compact=False)).prepare(
                session, messages, schemas=[], runtime='runtime details', memory='memory details')
            combined = '\n'.join(str(message.get('content', '')) for message in history)
            for heading in ('## 系统提示词', '## 当前Session总结', '## History积累', '## 用户输入'):
                assert heading in combined
            assert any(message.get('role') == 'assistant' and message.get('tool_calls') for message in history)
            assert any(message.get('role') == 'tool' and message.get('tool_call_id') == 'call-1'
                       for message in history)
            assert any(message.get('role') == 'user' and 'current request' in message.get('content', '')
                       for message in history)
            assert messages == original
            assert isinstance(metrics, dict)
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_compaction_writes_private_manifest_and_complete_immutable_chunks(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            messages = _messages()
            original = copy.deepcopy(messages)
            durable_before = copy.deepcopy(session.record['messages'])
            await _service(manager).prepare(session, messages)
            folder = context_env.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            assert manifest['revision'] >= 1
            assert manifest['chunks']
            for meta in manifest['chunks']:
                chunk_id = meta.get('ID', meta.get('id'))
                assert re.fullmatch(r'[a-f0-9]{32}', chunk_id)
                chunk = json.loads((folder / 'chunks' / f'{chunk_id}.json').read_text())
                assert chunk['ID'] == chunk_id
                assert set(chunk['Time']) >= {'start', 'end', 'created_at'}
                assert chunk['Summary']
                assert isinstance(chunk['RawHistory'], str)
                assert json.loads(chunk['RawHistory'])
            assert messages == original
            assert session.record['messages'] == durable_before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_recovery_is_idempotent_and_append_advances_summary(context_env):
    async def run():
        manager, session = await _manager(context_env)
        messages = _messages()
        try:
            first = SupervisorStub()
            await _service(manager, first).prepare(session, messages)
            path = context_env.directory(session.id) / 'context' / 'manifest.json'
            before = path.read_bytes()
            session_id = session.id
        finally:
            await manager.shutdown()
        restored = SessionManager(context_env, model=lambda *a, **k: None)
        try:
            session = restored.sessions[session_id]
            second = SupervisorStub()
            service = _service(restored, second)
            await service.prepare(session, messages)
            assert path.read_bytes() == before
            assert not second.summaries
            appended = messages + [
                {'role': 'assistant', 'content': 'late answer ' + 'x' * 120},
                {'role': 'user', 'content': 'next request'},
            ]
            await service.prepare(session, appended)
            assert json.loads(path.read_text())['revision'] > json.loads(before)['revision']
            assert any(kind == 'session' and previous for kind, previous, _ in second.summaries)
        finally:
            await restored.shutdown()
    asyncio.run(run())


def test_nonappend_prefix_tamper_rejected_without_manifest_change(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            service = _service(manager)
            messages = _messages()
            await service.prepare(session, messages)
            path = context_env.directory(session.id) / 'context' / 'manifest.json'
            before = path.read_bytes()
            tampered = copy.deepcopy(messages)
            tampered[1]['content'] = 'rewritten old user text'
            with pytest.raises((ValueError, PermissionError)):
                await service.prepare(session, tampered)
            assert path.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_search_is_summary_only_and_read_is_bounded(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            service = _service(manager)
            await service.prepare(session, _messages())
            results = await service.search(session, 'chunk', limit=5, method='bm25')
            assert results
            assert all('RawHistory' not in item and 'raw_history' not in item for item in results)
            chunk_id = results[0].get('ID', results[0].get('id'))
            excerpt = await service.read(session, chunk_id, offset=5, limit=23)
            assert excerpt['total_chars'] > 23
            assert excerpt['next_offset'] > 5
            assert len(excerpt['chunks']) >= 1
            raw = excerpt['chunks'][0]['RawHistory']
            assert isinstance(raw, str) and len(raw) <= 23
            with pytest.raises((ValueError, PermissionError)):
                await service.read(session, '0' * 32)
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_read_page_under_metadata_sized_output_limit_must_advance_or_reject(context_env, monkeypatch):
    async def run():
        manager, session = await _manager(context_env)
        try:
            service = _service(manager)
            await service.prepare(session, _messages())
            manifest = json.loads((context_env.directory(session.id) / 'context' / 'manifest.json').read_text())
            chunk_id = manifest['chunks'][0]['ID']
            one = await service.read(session, chunk_id, offset=0, limit=1)
            assert one['total_chars'] > 100
            assert len(one['chunks'][0]['RawHistory']) == 1
            empty_page = copy.deepcopy(one)
            empty_page['chunks'][0]['RawHistory'] = ''
            empty_page['next_offset'] = 0
            quota = len(json.dumps(empty_page, ensure_ascii=False))
            monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', quota)
            try:
                page = await service.read(session, chunk_id, offset=0, limit=100)
            except ValueError:
                return
            consumed = sum(len(item['RawHistory']) for item in page['chunks'])
            assert consumed >= 1
            assert page['next_offset'] is not None and page['next_offset'] > 0
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_private_context_symlink_is_rejected(context_env, tmp_path):
    async def run():
        manager, session = await _manager(context_env)
        try:
            foreign = tmp_path / 'foreign'
            foreign.mkdir()
            folder = context_env.directory(session.id) / 'context'
            folder.parent.mkdir(parents=True, exist_ok=True)
            folder.symlink_to(foreign, target_is_directory=True)
            with pytest.raises((ValueError, PermissionError, OSError)):
                await _service(manager).prepare(session, _messages())
            assert list(foreign.iterdir()) == []
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_hardlinked_private_audit_file_cannot_modify_foreign_file(context_env, tmp_path):
    async def run():
        manager, session = await _manager(context_env)
        try:
            foreign = tmp_path / 'foreign-audit.txt'
            foreign.write_text('foreign original', encoding='utf-8')
            directory = context_env.directory(session.id)
            directory.mkdir(parents=True, exist_ok=True)
            os.link(foreign, directory / 'audit.jsonl')
            with pytest.raises((ValueError, PermissionError, OSError)):
                await _service(manager).prepare(session, _messages())
            assert foreign.read_text(encoding='utf-8') == 'foreign original'
            assert not (directory / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_lifecycle_gate_rejects_compaction_without_private_write(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            manager.maintenance = True
            with pytest.raises((ValueError, PermissionError)):
                await _service(manager).prepare(session, _messages())
            assert not (context_env.directory(session.id) / 'context').exists()
        finally:
            manager.maintenance = False
            await manager.shutdown()
    asyncio.run(run())


def test_chunk_summaries_are_independent_and_session_summary_merges_once(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            supervisor = SupervisorStub()
            service = _service(manager, supervisor)
            messages = _messages(12)
            await service.prepare(session, messages)
            chunk_calls = [(previous, items) for kind, previous, items in supervisor.summaries if kind == 'chunk']
            session_calls = [(previous, items) for kind, previous, items in supervisor.summaries if kind == 'session']
            assert len(chunk_calls) >= 2
            assert all(previous == '' for previous, _ in chunk_calls)
            assert len(session_calls) == 1
            assert session_calls[0][0] == ''
            # An identical request must not summarize any already indexed content twice.
            before = len(supervisor.summaries)
            await service.prepare(session, messages)
            assert len(supervisor.summaries) == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_session_summary_receives_all_new_raw_history_not_only_chunk_summaries(context_env):
    async def run():
        manager, session = await _manager(context_env)
        try:
            supervisor = SupervisorStub()
            await _service(manager, supervisor).prepare(session, _messages(8))
            session_inputs = [items for kind, _, items in supervisor.summaries if kind == 'session']
            assert len(session_inputs) == 1
            serialized = json.dumps(session_inputs[0], ensure_ascii=False)
            assert 'question-0' in serialized
            assert 'answer-0' in serialized
            assert 'independent chunk summary' not in serialized
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_failed_manifest_publish_keeps_previous_revision(context_env, monkeypatch):
    async def run():
        manager, session = await _manager(context_env)
        try:
            service = _service(manager)
            messages = _messages(6)
            await service.prepare(session, messages)
            path = context_env.directory(session.id) / 'context' / 'manifest.json'
            before = path.read_bytes()
            original_write = context_env.atomic_write

            def fail_manifest(target, text):
                if str(target).endswith('/context/manifest.json'):
                    raise OSError('injected manifest failure')
                return original_write(target, text)

            monkeypatch.setattr(context_env, 'atomic_write', fail_manifest)
            appended = messages + [
                {'role': 'assistant', 'content': 'new answer ' + 'x' * 300},
                {'role': 'user', 'content': 'new request'},
            ]
            with pytest.raises(OSError):
                await service.prepare(session, appended)
            assert path.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_other_session_cannot_read_private_chunk(context_env):
    async def run():
        manager, owner = await _manager(context_env)
        try:
            service = _service(manager)
            await service.prepare(owner, _messages())
            manifest = json.loads((context_env.directory(owner.id) / 'context' / 'manifest.json').read_text())
            item = manifest['chunks'][0]
            chunk_id = item.get('ID', item.get('id'))
            other = manager.create()
            with pytest.raises((ValueError, PermissionError)):
                await service.read(other, chunk_id)
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_oversized_current_user_raises_budget_error_without_dropping_record(context_env):
    from ai_agent_startup.core.context import ContextBudgetError

    async def run():
        manager, session = await _manager(context_env)
        try:
            messages = [{'role': 'system', 'content': 'policy'},
                        {'role': 'user', 'content': 'C' * 100_000}]
            original = copy.deepcopy(messages)
            with pytest.raises(ContextBudgetError):
                await _service(manager).prepare(session, messages, schemas=[])
            assert messages == original
            assert session.record['messages'][0]['role'] == 'system'
            folder = context_env.directory(session.id) / 'context'
            assert not (folder / 'manifest.json').exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_failed_summary_with_full_previous_summary_still_records_new_fact(context_env):
    unique_fact = 'NEWLY_OBSERVED_FACT_7f3b2c'

    class SwitchingSupervisor(SupervisorStub):
        def __init__(self):
            super().__init__()
            self.fail = False

        async def summarize(self, previous_summary, messages, *, kind='session'):
            if self.fail:
                raise RuntimeError('supervisor unavailable')
            return 'P' * 2000 if kind == 'session' else 'chunk summary'

    async def run():
        manager, session = await _manager(context_env)
        try:
            supervisor = SwitchingSupervisor()
            service = _service(manager, supervisor)
            first = _messages(6)
            await service.prepare(session, first)
            path = context_env.directory(session.id) / 'context' / 'manifest.json'
            before = json.loads(path.read_text())
            assert len(before['session_summary']) == 2000
            supervisor.fail = True
            appended = first + [{'role': 'assistant', 'content': 'previous current answer'}]
            for index in range(5):
                appended.extend([
                    {'role': 'user', 'content': unique_fact + f' new question {index}'},
                    {'role': 'assistant', 'content': 'observed new answer ' + 'A' * 90},
                ])
            appended.append({'role': 'user', 'content': 'next current request'})
            await service.prepare(session, appended)
            after = json.loads(path.read_text())
            assert after['revision'] > before['revision']
            assert after['summary_kind'] == 'extractive_fallback'
            assert unique_fact in after['session_summary']
        finally:
            await manager.shutdown()

    asyncio.run(run())
