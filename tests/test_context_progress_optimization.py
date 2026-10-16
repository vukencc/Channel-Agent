"""Regression contracts for bounded context work and actual execution progress."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.execution_guidance import repeated_read_notice
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


class SupervisorStub:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True, check_interval=1, max_chunks=24,
                                        variance_threshold=.75, max_input_chars=12000, timeout=20)
        self.inspections = []
        self.summaries = []

    async def inspect(self, snapshot):
        self.inspections.append(copy.deepcopy(snapshot))
        values = [0.0, 1.0] * len(snapshot['chunks'])
        return {'compact': True, 'reason': 'different values',
                'values': values[:len(snapshot['chunks'])]}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summaries.append((kind, previous_summary, copy.deepcopy(messages)))
        return 'summary of ' + kind


@pytest.fixture
def context_store(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 2)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 300)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env')
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _messages(count=7, *, tool=False):
    messages = [{'role': 'system', 'content': 'System instructions'}]
    for index in range(count):
        messages.append({'role': 'user', 'content': f'question-{index} ' + 'q' * 70})
        if tool and index == 0:
            messages.append({'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'read-0', 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': '{"path":"sample.txt"}'}}]})
            messages.append({'role': 'tool', 'tool_call_id': 'read-0',
                             'content': 'TOOL_PAYLOAD_SENTINEL_' + 'z' * 300})
        messages.append({'role': 'assistant', 'content': f'answer-{index} ' + 'a' * 70})
    messages.append({'role': 'user', 'content': 'current request'})
    return messages


async def _manager(store, supervisor):
    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'unused'}
    manager = SessionManager(store, model=model)
    manager.history_context.supervisor = supervisor
    return manager, manager.create()


def test_low_utilization_skips_supervisor_and_compaction_despite_value_variance(context_store, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .95)

    async def run():
        supervisor = SupervisorStub()
        manager, session = await _manager(context_store, supervisor)
        try:
            _, metrics = await manager.history_context.prepare(session, _messages(3))
            assert metrics['utilization'] < config.CONTEXT_COMPACT_RATIO
            assert metrics['supervisor'] == 'below_threshold'
            assert metrics['archived_chunks'] == 0
            assert not supervisor.inspections
            assert not supervisor.summaries
            assert not (context_store.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_high_utilization_compacts_complete_tool_pair_and_keeps_raw_history(context_store, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .1)

    async def run():
        supervisor = SupervisorStub()
        manager, session = await _manager(context_store, supervisor)
        messages = _messages(tool=True)
        original = copy.deepcopy(messages)
        try:
            _, metrics = await manager.history_context.prepare(session, messages)
            assert metrics['supervisor'] == 'evaluated'
            assert supervisor.inspections
            assert metrics['archived_chunks'] > 0
            folder = context_store.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            archived = [json.loads((folder / 'chunks' / (item['ID'] + '.json')).read_text())
                        for item in manifest['chunks']]
            pair = [item for item in archived if 'read-0' in item['RawHistory']]
            assert len(pair) == 1
            raw = json.loads(pair[0]['RawHistory'])
            assert any(item.get('tool_call_id') == 'read-0' for item in raw)
            assert any(any(call['id'] == 'read-0' for call in item.get('tool_calls', [])) for item in raw)
            assert 'TOOL_PAYLOAD_SENTINEL_' in pair[0]['RawHistory']
            assert messages == original
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_session_summary_uses_bounded_chunk_metadata_and_previous_manifest_summary(context_store, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .1)
    # 本用例验证连续两次摘要合并；关闭新增历史节流，保留原有验收断言。
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_MIN_NEW_GROUPS', 0)

    async def run():
        supervisor = SupervisorStub()
        manager, session = await _manager(context_store, supervisor)
        try:
            first = _messages(tool=True)
            await manager.history_context.prepare(session, first)
            session_calls = [(previous, payload) for kind, previous, payload in supervisor.summaries
                             if kind == 'session']
            assert len(session_calls) == 1
            serialized = json.dumps(session_calls[0][1], ensure_ascii=False)
            assert 'TOOL_PAYLOAD_SENTINEL_' not in serialized
            assert 'question-0' not in serialized
            assert len(serialized) < 4000
            assert all({'ID', 'Time', 'Summary', 'summary_kind'} <= set(item)
                       for item in session_calls[0][1])
            assert all('RawHistory' not in item for item in session_calls[0][1])
            appended = first + [{'role': 'assistant', 'content': 'reply'},
                                {'role': 'user', 'content': 'followup'},
                                {'role': 'assistant', 'content': 'more reply'},
                                {'role': 'user', 'content': 'latest request'}]
            await manager.history_context.prepare(session, appended)
            session_calls = [(previous, payload) for kind, previous, payload in supervisor.summaries
                             if kind == 'session']
            assert len(session_calls) >= 2
            assert session_calls[-1][0] == 'summary of session'
        finally:
            await manager.shutdown()

    asyncio.run(run())


def _progress_record(names, *, sources=None):
    messages = [{'role': 'system', 'content': 'rules'}]
    tools = []
    for index, name in enumerate(names):
        call_id = f'call-{index}'
        event_id = f'event-{index}'
        arguments = {'path': 'sample.txt', 'offset': index * 10}
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]},
            {'role': 'tool', 'tool_call_id': call_id, '_event_id': event_id,
             'content': f'unique page {index}'},
        ])
        tools.append({'name': name, 'event_id': event_id,
                      'source': (sources or ['executed'] * len(names))[index]})
    return {'messages': messages, 'last_run': {'turn_id': 'current', 'tools': tools}}


def test_distinct_executed_reads_warn_after_six_calls_and_progress_clears_stall():
    record = _progress_record(['read_file', 'list_files'] * 3)
    assert repeated_read_notice(record)
    assert not repeated_read_notice(_progress_record(['read_file', 'list_files'] * 2 + ['read_file']))
    for progress in ('create_file', 'run_command'):
        resumed = _progress_record(['read_file', 'list_files'] * 3 + [progress])
        assert not repeated_read_notice(resumed)


def test_denied_failed_and_cached_calls_do_not_count_as_read_progress():
    names = ['read_file', 'list_files'] * 3
    sources = ['executed'] * 5 + ['not_executed']
    assert not repeated_read_notice(_progress_record(names, sources=sources))
    for source in ('not_executed', 'execution_error', 'cached'):
        record = _progress_record(names + ['create_file'], sources=['executed'] * 6 + [source])
        assert repeated_read_notice(record)


@pytest.mark.parametrize('status,still_stalled', [('in_progress', True), ('completed', False)])
def test_only_completed_plan_update_clears_read_stall(status, still_stalled):
    record = _progress_record(['read_file', 'list_files'] * 3 + ['plan_update'])
    call = record['messages'][-2]['tool_calls'][0]
    call['function']['arguments'] = json.dumps({'plan_id': 'plan-1', 'task_key': 'inspect',
                                                 'status': status, 'expected_revision': 2})
    assert bool(repeated_read_notice(record)) is still_stalled


def test_hard_budget_batches_protected_tool_groups_into_one_session_summary(context_store, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 23_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100_000)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .8)
    # Leave room for the complete model summary while the raw tool results exceed the window.
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 128)
    messages = [{'role': 'system', 'content': 'System instructions'},
                {'role': 'user', 'content': 'CURRENT_USER_INPUT'}]
    for index in range(2):
        call_id = f'large-read-{index}'
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': f'file-{index}.txt'})}}]},
            {'role': 'tool', 'tool_call_id': call_id,
             'content': f'RESULT_{index}_' + str(index) * 12_000},
        ])
    original = copy.deepcopy(messages)

    async def run():
        supervisor = SupervisorStub()
        manager, session = await _manager(context_store, supervisor)
        try:
            history, metrics = await manager.history_context.prepare(session, messages)
            assert metrics['utilization'] <= 1
            assert 'CURRENT_USER_INPUT' in json.dumps(history, ensure_ascii=False)
            assert messages == original
            assert len([kind for kind, _, _ in supervisor.summaries if kind == 'session']) == 1
            folder = context_store.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            raw = [json.loads(json.loads((folder / 'chunks' / (item['ID'] + '.json')).read_text())['RawHistory'])
                   for item in manifest['chunks']]
            assert {message['tool_call_id'] for chunk in raw for message in chunk
                    if message.get('role') == 'tool'} == {'large-read-0', 'large-read-1'}
            assert all(any(any(call['id'] == message['tool_call_id']
                               for call in candidate.get('tool_calls', [])) for candidate in chunk)
                           for chunk in raw for message in chunk if message.get('role') == 'tool')
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_no_plan_real_read_records_executed_provenance(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        calls = 0

        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'read-1', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"sample.txt"}'}}]}
            return {'role': 'assistant', 'content': 'done'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        (manager.workspace(session) / 'sample.txt').write_text('sample content')
        try:
            manager.submit(session, 'Read the sample')
            await session.task
            tool = session.record['last_run']['tools'][0]
            result = next(item for item in session.record['messages']
                          if item.get('tool_call_id') == 'read-1')
            assert tool['source'] == 'executed'
            assert result['_event_id'] == tool['event_id']
            assert result['_provenance'] == 'executed'
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_private_provenance_does_not_consume_model_context_budget(monkeypatch):
    from ai_agent_startup.core.context import build_model_history
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 100000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100000)
    plain = [{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'work'},
             {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
             {'role': 'tool', 'tool_call_id': 'c', 'content': 'result'}]
    decorated = copy.deepcopy(plain)
    decorated[-1].update(_event_id='a' * 32, _provenance='executed')
    _, baseline = build_model_history(plain)
    _, actual = build_model_history(decorated)
    assert actual['sent_tokens'] == baseline['sent_tokens']
    assert actual['sent_chars'] == baseline['sent_chars']
    assert decorated[-1]['_provenance'] == 'executed'


def test_work_progress_receipts_survive_compaction_and_do_not_claim_completion():
    from ai_agent_startup.core.execution_guidance import work_progress_context
    record = _progress_record(['create_file', 'append_file', 'read_file'])
    record['messages'][1]['tool_calls'][0]['function']['arguments'] = '{"path":"other.txt"}'
    text = work_progress_context(record)
    assert 'create_file' in text and 'append_file' in text
    assert '工具结果' in text
    assert '不代表验收完成' in text
    assert work_progress_context(_progress_record(['create_file'], sources=['not_executed'])) == ''


def test_work_progress_keeps_latest_file_offset_across_user_turns():
    from ai_agent_startup.core.execution_guidance import work_progress_context
    record = _progress_record(['create_file', 'append_file'])
    for message in record['messages']:
        if message.get('role') == 'tool':
            message['_provenance'] = 'executed'
    record['last_run'] = {'tools': []}
    text = work_progress_context(record)
    assert 'append_file' in text and 'unique page 1' in text
    assert 'unique page 0' not in text
    assert len(text) < 2000



@pytest.mark.parametrize('context_limit', [2400, 1200, 1000, 900, 800, 700, 600, 500, 400, 256])
def test_plan_summary_keeps_work_evidence_and_explicit_resume_action(tmp_path, monkeypatch, sandbox_env, context_limit):
    monkeypatch.setattr(config, 'TASK_PLAN_CONTEXT_CHARS', context_limit)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        calls = 0
        async def model(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 7:
                name = 'create_file' if calls == 1 else 'read_file'
                arguments = {'path': 'artifact.txt', **({'content': 'actual artifact'} if calls == 1 else {})}
                return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': str(calls), 'function': {
                    'name': name, 'arguments': json.dumps(arguments)}}]}
            return {'role': 'assistant', 'content': 'not claiming acceptance'}
        manager = SessionManager(store, model=model, permission_override='full_access')
        session = manager.create()
        try:
            plan = await manager.task_plans.create(session, 'Build artifact', [{'key':'write', 'title':'write',
                'acceptance':'artifact exists and tests pass'}])
            await manager.task_plans.update(session, plan['plan_id'], 'write', 'in_progress', expected_revision=plan['revision'])
            manager.submit(session, 'Create artifact, then read it')
            await session.task
            session.record['status'] = 'checkpoint'
            await manager.task_plans.finish_run(session)
            summary = await manager.task_plans.summary(session)
            data = json.loads(summary[summary.index('{'):])
            evidence = next(item['id'] for item in manager.task_plans.cache[session.id]['evidence']
                            if item['name'] == 'create_file')
            assert evidence in data.get('evidence_ids', []) or any(item['id'] == evidence for item in data.get('work_evidence', []))
            if 'next_action' not in data:
                assert evidence in data['evidence_ids']
                assert data['next_task'] == 'write'
                action = {'tool': 'plan_update', 'arguments': {'plan_id': data['plan_id'],
                    'expected_revision': data['revision'], 'task_key': data['next_task'], 'status': 'in_progress'}}
            else:
                assert data['work_evidence'][0]['name'] == 'create_file'
                assert data['work_evidence'][0]['source'] == 'executed'
                action = data['next_action']
                assert action['tool'] == 'plan_update'
                assert action['arguments']['task_key'] == 'write'
                assert action['arguments']['status'] == 'in_progress'
                assert action['arguments']['expected_revision'] == data['revision']
                assert action['arguments']['plan_id'] == data['plan_id']
            assert manager.task_plans.cache[session.id]['tasks'][0]['status'] == 'interrupted'
            resumed = await manager.task_plans.update(session, **action['arguments'])
            assert resumed['tasks'][0]['status'] == 'in_progress'
            assert len(summary) <= config.TASK_PLAN_CONTEXT_CHARS
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())

@pytest.mark.parametrize('initial_utilization', [.99, 1.1])
@pytest.mark.parametrize('summary_character', ['摘', '\x00'])
def test_compaction_budget_estimate_includes_new_refs_and_long_summary(context_store, monkeypatch, initial_utilization, summary_character):
    from ai_agent_startup.core.history_context import _groups
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .9)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 1000)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_CHARS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_CHARS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_SEARCH_TOKENS', 0)
    monkeypatch.setattr(config, 'CONTEXT_RESERVE_USER_TOKENS', 0)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100000)
    class LongSummary(SupervisorStub):
        async def summarize(self, previous_summary, messages, *, kind='session'):
            self.summaries.append((kind, previous_summary, copy.deepcopy(messages)))
            return summary_character * config.CONTEXT_SUMMARY_CHARS
    async def run():
        supervisor = LongSummary()
        supervisor.settings.max_input_chars = 100000
        manager, session = await _manager(context_store, supervisor)
        messages = _messages(4)
        for message in messages[1:-3]:
            message['content'] = 'short'
        for index in (-3, -2):
            messages[index]['content'] += 'x' * 8000
        original = copy.deepcopy(messages)
        try:
            service = manager.history_context
            manifest = await service._manifest(session)
            groups = _groups(messages)
            available = [group for group in groups if group[0] != len(messages) - 1]
            protected = available[-2:]
            previews = service._chunks(session, messages, available[:-2])
            initial = service._compose(messages, manifest, previews, protected, len(messages) - 1, '', '')
            size = service._metrics(initial, [], messages[-1], messages)['total_chars']
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', int(size / initial_utilization))
            history, metrics = await service.prepare(session, messages, schemas=[])
            assert metrics['utilization'] <= 1
            if summary_character == '\x00':
                assert metrics['summary_kind'] == 'extractive_fallback'
                assert '\x00' not in json.loads((context_store.directory(session.id) / 'context' / 'manifest.json').read_text())['session_summary']
            assert messages == original
            assert 'current request' in json.dumps(history)
            assert len([entry for entry in supervisor.summaries if entry[0] == 'session']) == 1
            assert metrics['archived_chunks'] > len(previews)
        finally:
            await manager.shutdown()
    asyncio.run(run())
