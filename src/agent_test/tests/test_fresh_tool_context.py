import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def schemas(*names):
    return [{'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}} for name in names]


def messages(size=3000):
    calls = [{'id': str(i), 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}} for i in range(2)]
    return [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'old'},
            {'role': 'assistant', 'content': 'x' * 20000}, {'role': 'user', 'content': 'current'},
            {'role': 'assistant', 'content': '', 'tool_calls': calls},
            *[{'role': 'tool', 'tool_call_id': str(i), 'content': str(i) * size} for i in range(2)]]


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 16000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100000)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 128)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_TARGET_RATIO', .1)
    for name in ('CONTEXT_RESERVE_SEARCH_CHARS', 'CONTEXT_RESERVE_USER_CHARS',
                 'CONTEXT_RESERVE_SEARCH_TOKENS', 'CONTEXT_RESERVE_USER_TOKENS'):
        monkeypatch.setattr(config, name, 0)


class Supervisor:
    settings = SimpleNamespace(enabled=False)
    def __init__(self):
        self.calls = 0
    async def summarize(self, *args, **kwargs):
        self.calls += 1
        return 'summary'


@pytest.mark.parametrize('names', [None, (), ('history_search',), ('rag_search',)])
def test_fresh_batch_survives_hard_compaction_and_retry(tmp_path, sandbox_env, names):
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store)
        manager.history_context.supervisor = Supervisor()
        session = manager.create()
        journal = messages()
        try:
            supplied = None if names is None else schemas(*names)
            history, metrics = await manager.history_context.prepare(session, journal, schemas=supplied)
            assert metrics['archived_chunks'] > 0 and metrics['utilization'] <= 1
            assert metrics['utilization'] > metrics['target_utilization']
            assert [m.get('tool_call_id') for m in history if m['role'] == 'tool'] == ['0', '1']
            assert any(len(m.get('tool_calls', [])) == 2 for m in history)
            retry, _ = await manager.history_context.prepare(session, journal, schemas=supplied)
            assert [m.get('tool_call_id') for m in retry if m['role'] == 'tool'] == ['0', '1']
            # 仅模拟此前 retry 窗口已得到成功响应；准备和 partial 本身均不构成交付。
            # 此前提为受控离线 fixture，不是真实模型执行或性能证据。
            session.record['messages'] = journal
            manager.history_context.mark_delivered(session, retry)
            journal.append({'role': 'assistant', 'content': 'seen'})
            later, _ = await manager.history_context.prepare(session, journal, schemas=supplied)
            assert not any(m.get('tool_call_id') == '0' for m in later)
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_oversized_fresh_batch_fails_before_summary_or_publish(tmp_path, sandbox_env):
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store)
        supervisor = Supervisor()
        manager.history_context.supervisor = supervisor
        session = manager.create()
        try:
            before = copy.deepcopy(await manager.history_context._manifest(session))
            with pytest.raises(ContextBudgetError, match='最新完整工具批次无法装入窗口'):
                await manager.history_context.prepare(session, messages(12000), schemas=[])
            assert supervisor.calls == 0
            assert await manager.history_context._manifest(session) == before
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())


def test_legacy_archived_unread_batch_restored_with_or_without_reader_without_duplicate(tmp_path, sandbox_env):
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store)
        manager.history_context.supervisor = Supervisor()
        session = manager.create()
        journal = messages()
        original = copy.deepcopy(journal)
        try:
            service = manager.history_context
            session.record['messages'] = copy.deepcopy(journal)
            # 真实发布旧版本可能产生的归档，不用新 prepare 重演静默丢失未读批次的旧 bug。
            manifest = await service._manifest(session)
            chunks = service._chunks(session, journal, [(1, 2), (2, 3), (4, 7)])
            await service._compact(session, manifest, chunks, local=True)
            await manager.save(session)
            before = copy.deepcopy(await service._manifest(session))
            directory = store.directory(session.id) / 'context'
            durable_before = {p.relative_to(directory): p.read_bytes()
                              for p in directory.rglob('*') if p.is_file()}
            raw_before = (store.directory(session.id) / 'messages.jsonl').read_bytes()
            archived = [json.loads(json.loads((directory / 'chunks' / (item['ID'] + '.json')).read_text(
                encoding='utf-8'))['RawHistory']) for item in before['chunks']]
            assert [m['tool_call_id'] for chunk in archived for m in chunk if m['role'] == 'tool'] == ['0', '1']
            for supplied in (schemas('history_read'), []):
                restored, metrics = await service.prepare(session, journal, schemas=supplied)
                assert metrics['utilization'] <= 1
                assert [m.get('tool_call_id') for m in restored if m['role'] == 'tool'] == ['0', '1']
                assert [m for m in restored if m['role'] == 'tool'] == journal[5:7]
                assert sum(len(m.get('tool_calls', [])) == 2 for m in restored) == 1
                after = await service._manifest(session)
                assert after['chunks'] == before['chunks'] and after['revision'] == before['revision']
                assert journal == original and session.record['messages'] == original
                assert durable_before == {p.relative_to(directory): p.read_bytes()
                                          for p in directory.rglob('*') if p.is_file()}
                assert (store.directory(session.id) / 'messages.jsonl').read_bytes() == raw_before
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
