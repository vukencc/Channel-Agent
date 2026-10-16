"""Regression contracts for the structured-context compaction trigger."""

import asyncio
import copy
import json
import math
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.context import ContextBudgetError
from ai_agent_startup.core.history_context import HistoryContext, _groups
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


class SupervisorStub:
    def __init__(self, values=None, *, enabled=True, interval=1, fail=False):
        self.values = values
        self.fail = fail
        self.inspections = []
        self.summaries = []
        self.settings = SimpleNamespace(enabled=enabled, check_interval=interval,
                                        variance_threshold=.5, timeout=5,
                                        max_input_chars=12000, max_chunks=24)

    async def inspect(self, snapshot):
        self.inspections.append(copy.deepcopy(snapshot))
        if self.fail:
            raise TimeoutError('supervisor unavailable')
        values = self.values
        if values is None:
            values = [float(index % 2) for index in range(len(snapshot['chunks']))]
        return {'compact': True, 'reason': 'stub', 'values': values}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summaries.append((kind, copy.deepcopy(messages)))
        return 'archived facts'


@pytest.fixture
def context_case(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 2)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 200)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 80)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_CHARS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_CHARS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_TOKENS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_TOKENS', 0)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .9)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _messages(count=5, *, paired=False):
    messages = [{'role': 'system', 'content': 'Keep the current user instruction.'}]
    for index in range(count):
        messages.append({'role': 'user', 'content': f'earlier-{index} ' + 'u' * 180})
        if paired:
            identifier = f'call-{index}'
            messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
                {'id': identifier, 'type': 'function', 'function':
                 {'name': 'read_file', 'arguments': json.dumps({'path': f'{index}.txt'})}}]})
            messages.append({'role': 'tool', 'tool_call_id': identifier,
                             'content': f'result-{index} ' + 'r' * 180})
        else:
            messages.append({'role': 'assistant', 'content': f'answer-{index} ' + 'a' * 180})
    messages.append({'role': 'user', 'content': 'current user request'})
    return messages


def _set_utilization(monkeypatch, service, messages, target):
    """Calibrate from the actual composed request, with all reserves disabled."""
    groups = _groups(messages)
    current = len(messages) - 1
    available = [group for group in groups if group[0] != current]
    keep = min(config.CONTEXT_HISTORY_KEEP_GROUPS, len(available))
    protected = available[-keep:] if keep else []
    eligible = [group for group in available if group not in protected]
    previews = service._chunks(service._test_session, messages, eligible)
    manifest = {'session_summary': '', 'summary_kind': 'none', 'chunks': []}
    composed = service._compose(messages, manifest, previews, protected, current, '', '')
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1_000_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 1_000_000)
    baseline = service._metrics(composed, [], messages[current], messages)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', math.ceil(baseline['total_chars'] / target))
    assert target - .02 <= service._metrics(composed, [], messages[current], messages)['utilization'] <= target + .02


@pytest.mark.parametrize('mode', ['zero_variance', 'disabled', 'unavailable', 'malformed', 'not_due'])
def test_soft_pressure_without_valid_high_variance_keeps_history(context_case, monkeypatch, mode):
    async def run():
        supervisor = SupervisorStub(enabled=mode != 'disabled',
                                    interval=2 if mode == 'not_due' else 1,
                                    fail=mode == 'unavailable')
        if mode == 'zero_variance':
            supervisor.values = [0.5] * len(_groups(_messages())[:-1])
        elif mode == 'malformed':
            supervisor.values = [1.0]
        manager = SessionManager(context_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        service._test_session = session
        messages = _messages()
        try:
            _set_utilization(monkeypatch, service, messages, .93)
            _, metrics = await service.prepare(session, messages, schemas=[])
            assert .9 <= metrics['utilization'] < 1
            assert metrics['archived_chunks'] == 0
            assert metrics['compact_reason'] == ''
            assert not supervisor.summaries
            assert not (context_case.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_soft_pressure_high_variance_compacts_complete_groups(context_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(context_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        service._test_session = session
        messages = _messages(paired=True)
        original = copy.deepcopy(messages)
        try:
            _set_utilization(monkeypatch, service, messages, .93)
            _, metrics = await service.prepare(session, messages, schemas=[])
            assert metrics['compact_reason'] == 'high_utilization_and_variance'
            assert metrics['value_variance'] >= supervisor.settings.variance_threshold
            assert metrics['archived_chunks'] > 0
            manifest = json.loads((context_case.directory(session.id) / 'context' / 'manifest.json').read_text())
            for metadata in manifest['chunks']:
                record = json.loads((context_case.directory(session.id) / 'context' / 'chunks' /
                                     f"{metadata['ID']}.json").read_text())
                raw = json.loads(record['RawHistory'])
                assert raw == original[metadata['Time']['start']:metadata['Time']['end']]
                assert len([m for m in raw if m.get('tool_calls')]) == len(
                    [m for m in raw if m['role'] == 'tool'])
            assert messages == original
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_high_variance_can_promote_one_protected_group(context_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub(values=[0.0, 1.0])
        manager = SessionManager(context_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        service._test_session = session
        messages = _messages(count=1, paired=True)
        messages[1]['content'] += 'older fact ' * 300
        messages[3]['content'] += 'recent paired result ' * 100
        try:
            _set_utilization(monkeypatch, service, messages, .93)
            history, metrics = await service.prepare(session, messages, schemas=[])
            assert metrics['compact_reason'] == 'high_utilization_and_variance'
            assert metrics['archived_chunks'] == 1
            manifest = json.loads((context_case.directory(session.id) / 'context' / 'manifest.json').read_text())
            assert manifest['chunks'][0]['Time']['start'] == 1
            assert manifest['chunks'][0]['Time']['end'] == 2
            assert any(message.get('tool_calls') for message in history)
            assert any(message.get('role') == 'tool' and message.get('tool_call_id') == 'call-0'
                       for message in history)
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_hard_pressure_archives_paired_history_even_without_supervisor(context_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub(enabled=False)
        manager = SessionManager(context_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        service._test_session = session
        messages = _messages(paired=True)
        try:
            _set_utilization(monkeypatch, service, messages, 1.05)
            _, metrics = await service.prepare(session, messages, schemas=[])
            assert metrics['compact_reason'] == 'context_insufficient'
            assert metrics['archived_chunks'] > 0
            manifest = json.loads((context_case.directory(session.id) / 'context' / 'manifest.json').read_text())
            for metadata in manifest['chunks']:
                record = json.loads((context_case.directory(session.id) / 'context' / 'chunks' /
                                     f"{metadata['ID']}.json").read_text())
                raw = json.loads(record['RawHistory'])
                assert [call['id'] for message in raw for call in message.get('tool_calls', [])] == [
                    message['tool_call_id'] for message in raw if message['role'] == 'tool']
            assert not supervisor.inspections
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_hard_pressure_with_no_removable_history_raises_budget_error(context_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub(enabled=False)
        manager = SessionManager(context_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        service._test_session = session
        messages = [{'role': 'system', 'content': 'Keep rules'},
                    {'role': 'user', 'content': 'current ' + 'x' * 3000}]
        try:
            _set_utilization(monkeypatch, service, messages, 1.05)
            with pytest.raises(ContextBudgetError) as error:
                await service.prepare(session, messages, schemas=[])
            assert error.value.metrics['compact_reason'] == 'context_insufficient'
            assert not (context_case.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()
    asyncio.run(run())
