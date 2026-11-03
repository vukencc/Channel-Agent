import asyncio
import copy
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


def test_archived_batch_restored_without_duplicate_and_history_read_keeps_old_behavior(tmp_path, sandbox_env):
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store)
        manager.history_context.supervisor = Supervisor()
        session = manager.create()
        journal = messages()
        try:
            old, _ = await manager.history_context.prepare(session, journal, schemas=schemas('history_read'))
            assert not any(m.get('tool_call_id') == '0' for m in old)
            before = copy.deepcopy(await manager.history_context._manifest(session))
            restored, _ = await manager.history_context.prepare(session, journal, schemas=[])
            assert [m.get('tool_call_id') for m in restored if m['role'] == 'tool'] == ['0', '1']
            after = await manager.history_context._manifest(session)
            assert after['chunks'] == before['chunks'] and after['revision'] == before['revision']
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())
