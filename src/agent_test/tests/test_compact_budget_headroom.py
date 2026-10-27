"""Artificial engineering workloads for compaction overhead, not answer-quality evaluation."""

import asyncio
import copy
import json
import math
import time
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError
from agent_core.core.history_context import HistoryContext
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


class UnavailableSummary:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True, check_interval=1, max_chunks=24,
                                        variance_threshold=.75, max_input_chars=12000, timeout=5)
        self.summary_calls = 0

    async def inspect(self, snapshot):
        return {'compact': False, 'reason': 'equal value', 'values': [.5] * len(snapshot['chunks'])}

    async def summarize(self, previous, messages, *, kind='session'):
        self.summary_calls += 1
        # Deterministic mock latency, including the existing extraction fallback path.
        await asyncio.sleep(.001)
        raise RuntimeError('artificial unavailable summarizer')


@pytest.fixture
def budget_store(tmp_path, monkeypatch, sandbox_env):
    for name in ('ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS', 'ENABLE_AGENT_TASKS',
                 'ENABLE_COMMAND_JOBS', 'MEMORY_AUTO_EXTRACT'):
        monkeypatch.setattr(config, name, False)
    for name, value in {'MODEL_INPUT_CHARS': 64000, 'MODEL_INPUT_TOKENS': 16000,
                        'CONTEXT_HISTORY_KEEP_GROUPS': 2, 'CONTEXT_HISTORY_CHUNK_CHARS': 8000,
                        'CONTEXT_SUMMARY_CHARS': 2000, 'CONTEXT_RESERVE_USER_CHARS': 4000,
                        'CONTEXT_RESERVE_SEARCH_CHARS': 8000, 'CONTEXT_RESERVE_USER_TOKENS': 1000,
                        'CONTEXT_RESERVE_SEARCH_TOKENS': 2000, 'CONTEXT_COMPACT_RATIO': .9,
                        'CONTEXT_COMPACT_TARGET_RATIO': .75, 'CONTEXT_COMPACT_MIN_NEW_GROUPS': 4}.items():
        monkeypatch.setattr(config, name, value)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _tool_group(index, length=520):
    identifier = f'read-{index}'
    return [{'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': identifier, 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': f'data-{index}.txt'})}}]},
            {'role': 'tool', 'tool_call_id': identifier,
             'content': f'READ_FACT_{index:03d}; ' + '摘' * length}]


async def run_engineering_workload(store, service_class=HistoryContext, *, steps=48, supervisor=None):
    supervisor = supervisor or UnavailableSummary()
    manager = SessionManager(store, model=lambda *_args, **_kwargs: None)
    session = manager.create()
    service = service_class(manager, supervisor=supervisor)
    messages = [{'role': 'system', 'content': 'Keep permissions and archive provenance.'},
                {'role': 'user', 'content': 'Continue the engineering task.'}]
    for index in range(6):
        messages.extend(_tool_group(index, 1200))
    schemas = [{'type': 'function', 'function': {'name': 'read_file',
                'description': '摘' * 4200 + 's' * 16300,
                'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
    runtime = '\nEngineering state: ' + '态' * 1500
    compact_count, errors, utilization, compact_steps = 0, 0, [], []
    started = time.perf_counter()
    try:
        for index in range(steps):
            if index:
                messages.extend(_tool_group(index + 6))
            original = copy.deepcopy(messages)
            try:
                history, metrics = await service.prepare(session, messages, schemas=schemas, runtime=runtime)
                assert metrics['utilization'] <= 1
                assert metrics['reserved_chars'] == 11970
                assert metrics['reserved_tokens'] == 2992
                assert schemas[0]['function']['description'] in json.dumps(schemas, ensure_ascii=False)
                # A native result always has its native assistant request in the working window.
                native_ids = {call['id'] for message in history for call in message.get('tool_calls', [])}
                assert all(message['tool_call_id'] in native_ids for message in history if message['role'] == 'tool')
                assert 'Continue the engineering task.' in json.dumps(history, ensure_ascii=False)
                compact_count += int(metrics['compacted'])
                if metrics['compacted']:
                    compact_steps.append(index)
                utilization.append(metrics['utilization'])
            except ContextBudgetError:
                errors += 1
                compact_count = service.cache[session.id]['revision']
            assert messages == original
        manifest = service.cache[session.id]
        folder = store.directory(session.id) / 'context'
        for metadata in manifest['chunks']:
            archived = json.loads((folder / 'chunks' / (metadata['ID'] + '.json')).read_text())
            assert json.loads(archived['RawHistory']) == messages[metadata['Time']['start']:metadata['Time']['end']]
        return {'steps': steps, 'seconds': round(time.perf_counter() - started, 4),
                'summary_calls': supervisor.summary_calls, 'compactions': compact_count,
                'budget_errors': errors, 'compact_steps': compact_steps,
                'max_utilization': max(utilization, default=0),
                'summary_kind': manifest['summary_kind']}
    finally:
        await manager.shutdown()


def test_large_schema_small_results_leave_execution_headroom(budget_store):
    report = asyncio.run(run_engineering_workload(budget_store))
    print('ARTIFICIAL_COMPACT_WORKLOAD ' + json.dumps(report, sort_keys=True))
    assert report['budget_errors'] == 0
    assert report['compactions'] <= 16, report
    assert report['summary_calls'] <= 8, report


def test_fixed_overhead_failure_has_no_summary_calls_or_archive_write(budget_store):
    async def run():
        supervisor = UnavailableSummary()
        manager = SessionManager(budget_store, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = [{'role': 'system', 'content': 'Permissions remain.'},
                    {'role': 'user', 'content': 'Current request.'}, *_tool_group(0)]
        original = copy.deepcopy(messages)
        try:
            with pytest.raises(ContextBudgetError) as raised:
                await service.prepare(session, messages, schemas=[{'description': 's' * 70000}])
            assert raised.value.metrics['compact_reason'] == 'context_insufficient'
            assert raised.value.metrics['compacted'] is False
            assert supervisor.summary_calls == 0
            assert not (budget_store.directory(session.id) / 'context').exists()
            assert messages == original
        finally:
            await manager.shutdown()
    asyncio.run(run())

class SuccessfulSummary(UnavailableSummary):
    async def summarize(self, previous, messages, *, kind='session'):
        self.summary_calls += 1
        await asyncio.sleep(.001)
        return '摘' * config.CONTEXT_SUMMARY_CHARS


def test_narrow_archive_view_avoids_repeated_paid_summaries_even_when_provider_works(budget_store):
    report = asyncio.run(run_engineering_workload(budget_store, supervisor=SuccessfulSummary()))
    assert report['budget_errors'] == 0
    assert report['compactions'] <= 16, report
    assert report['summary_calls'] <= 8, report
    assert report['summary_kind'] == 'extractive_budget'


def test_existing_archive_budget_can_shrink_view_without_new_summary_or_write(budget_store, monkeypatch):
    async def run():
        supervisor = SuccessfulSummary()
        manager = SessionManager(budget_store, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = [{'role': 'system', 'content': 'Keep permissions.'},
                    {'role': 'user', 'content': 'Continue task.'}, *_tool_group(0, 20000)]
        schemas = [{'description': '摘' * 4200 + 's' * 16300}]
        runtime = '态' * 1500
        try:
            # Establish a full durable summary before later fixed overhead tightens the window.
            _, first = await service.prepare(session, messages, schemas=[], runtime='')
            assert first['compacted']
            manifest = service.cache[session.id]
            assert len(manifest['session_summary']) == config.CONTEXT_SUMMARY_CHARS
            directory = budget_store.directory(session.id) / 'context'
            before = {path.relative_to(directory): path.read_bytes()
                      for path in directory.rglob('*') if path.is_file()}
            calls = supervisor.summary_calls
            fixed = service._compose(messages, {**manifest, 'session_summary': '',
                'working_summary_chars': 0, 'working_reference_count': 0}, [], [], 1, runtime, '')
            fixed_metrics = service._metrics(fixed, schemas, messages[1], messages)
            monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', math.ceil(
                (fixed_metrics['sent_tokens'] + fixed_metrics['reserved_tokens']) / .99))
            # Reload proves the durable view caps do not depend on an in-memory cooldown.
            service = HistoryContext(manager, supervisor=supervisor)
            history, after = await service.prepare(session, messages, schemas=schemas, runtime=runtime)
            assert after['before_utilization'] > 1
            assert after['utilization'] <= 1
            assert not after['compacted']
            assert after['context_revision'] == first['context_revision']
            assert after['working_summary_chars'] < first['working_summary_chars']
            assert 'Continue task.' in json.dumps(history, ensure_ascii=False)
            assert supervisor.summary_calls == calls
            assert before == {path.relative_to(directory): path.read_bytes()
                              for path in directory.rglob('*') if path.is_file()}
        finally:
            await manager.shutdown()
    asyncio.run(run())
