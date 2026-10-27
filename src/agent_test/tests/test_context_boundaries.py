"""Boundary contracts for structured history scoring and retention."""

import asyncio
import copy
import json
import math
from types import SimpleNamespace
import uuid

import httpx
import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


class InspectStub:
    def __init__(self, *, timeout=.2, high_variance=False):
        self.settings = SimpleNamespace(check_interval=1, max_chunks=24,
                                        variance_threshold=.75, max_input_chars=12000,
                                        timeout=timeout, enabled=True)
        self.snapshots = []
        self.summary_calls = []
        self.high_variance = high_variance

    async def inspect(self, snapshot):
        self.snapshots.append(copy.deepcopy(snapshot))
        values = ([float(index % 2) for index in range(len(snapshot['chunks']))]
                  if self.high_variance else [0.5] * len(snapshot['chunks']))
        return {'compact': self.high_variance, 'reason': 'diagnostic', 'values': values}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summary_calls.append((kind, copy.deepcopy(messages)))
        return 'summary'


@pytest.fixture
def env(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_SUPERVISOR_ENV_FILE', tmp_path / 'supervisor.env', raising=False)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_KEEP_GROUPS', 2, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .95, raising=False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


async def _manager(store, supervisor=None):
    async def model(*args, **kwargs):
        return {'role': 'assistant', 'content': 'unused'}
    manager = SessionManager(store, model=model)
    session = manager.create()
    manager.history_context.supervisor = supervisor or InspectStub()
    return manager, session


def _conversation(count=8, *, marker=False):
    result = [{'role': 'system', 'content': 'System instructions.'}]
    for index in range(count):
        tail = f'UNIQUE_TAIL_{index:02d}' if marker else ''
        result.extend([
            {'role': 'user', 'content': f'old user {index} ' + 'same prefix ' * 45 + tail},
            {'role': 'assistant', 'content': f'old answer {index} ' + 'A' * 90},
        ])
    result.append({'role': 'user', 'content': 'latest user request'})
    return result


def test_supervisor_checks_multi_group_history_even_when_storage_coalesces_one_chunk(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 100_000, raising=False)

    async def run():
        supervisor = InspectStub()
        manager, session = await _manager(env, supervisor)
        try:
            messages = _conversation()
            history, metrics = await manager.history_context.prepare(session, messages)
            assert metrics['compact_reason'] == ''
            assert 'latest user request' in json.dumps(history, ensure_ascii=False)
            assert supervisor.snapshots, 'supervisor skipped several eligible groups coalesced into one chunk'
            assert 2 <= len(supervisor.snapshots[0]['chunks']) <= 24
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_inspection_sample_contains_distinguishing_tail_of_each_chunk(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 750, raising=False)

    async def run():
        supervisor = InspectStub()
        manager, session = await _manager(env, supervisor)
        try:
            await manager.history_context.prepare(session, _conversation(marker=True))
            assert supervisor.snapshots
            snapshot = supervisor.snapshots[0]
            assert len(snapshot['chunks']) >= 2
            samples = json.dumps(snapshot['chunks'], ensure_ascii=False)
            assert 'UNIQUE_TAIL_00' in samples, 'scoring input omitted the only distinguishing content'
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_inspection_caps_at_24_recent_complete_groups(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 100_000, raising=False)

    async def run():
        supervisor = InspectStub()
        manager, session = await _manager(env, supervisor)
        try:
            await manager.history_context.prepare(session, _conversation(30))
            assert supervisor.snapshots
            units = supervisor.snapshots[0]['chunks']
            assert len(units) == 24
            sample = json.dumps(units, ensure_ascii=False)
            assert 'old answer 28' in sample
            assert 'old user 0' not in sample
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_only_recent_protected_groups_remain_at_soft_budget_risk(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .8, raising=False)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100_000)
    messages = _conversation(1)

    async def run():
        supervisor = InspectStub()
        manager, session = await _manager(env, supervisor)
        try:
            _, baseline = await manager.history_context.prepare(session, messages)
            used = baseline['total_chars'] + baseline['reserved_chars']
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', math.floor(used / .85))
            history, metrics = await manager.history_context.prepare(session, messages)
            assert .8 <= metrics['utilization'] < 1
            assert metrics['compact_reason'] == ''
            assert metrics['archived_chunks'] == 0
            assert 'latest user request' in json.dumps(history, ensure_ascii=False)
            assert not supervisor.summary_calls
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_supervisor_scores_two_recent_protected_groups_without_archiving_them(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    messages = _conversation(1)

    async def run():
        supervisor = InspectStub()
        manager, session = await _manager(env, supervisor)
        try:
            history, metrics = await manager.history_context.prepare(session, messages)
            assert len(supervisor.snapshots) == 1
            assert len(supervisor.snapshots[0]['chunks']) == 2
            assert metrics['supervisor'] == 'evaluated'
            assert metrics['compact_reason'] == ''
            assert metrics['archived_chunks'] == 0
            assert 'latest user request' in json.dumps(history, ensure_ascii=False)
            assert not (env.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_high_variance_promotes_only_older_protected_group_and_keeps_recent_context(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    messages = _conversation(1)
    original = copy.deepcopy(messages)

    class HighVarianceInspect(InspectStub):
        async def inspect(self, snapshot):
            self.snapshots.append(copy.deepcopy(snapshot))
            return {'compact': True, 'reason': 'different values', 'values': [0.0, 1.0]}

    async def run():
        supervisor = HighVarianceInspect()
        manager, session = await _manager(env, supervisor)
        try:
            history, metrics = await manager.history_context.prepare(session, messages)
            assert len(supervisor.snapshots) == 1
            assert metrics['compact_reason'] == 'high_utilization_and_variance'
            assert metrics['value_variance'] >= supervisor.settings.variance_threshold
            assert metrics['archived_chunks'] >= 1
            assert messages == original
            assert any(item.get('role') == 'assistant' and 'old answer 0' in item.get('content', '')
                       for item in history), 'newer protected group must remain native'
            assert any(item.get('role') == 'user' and 'latest user request' in item.get('content', '')
                       for item in history)

            folder = env.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            older = [item for item in manifest['chunks']
                     if item['Time']['start'] <= 1 < item['Time']['end']]
            assert len(older) == 1
            assert all(item['Time']['end'] <= len(messages) - 1 for item in manifest['chunks'])
            page = await manager.history_context.read(session, older[0]['ID'])
            assert page['chunks']
            assert json.loads(page['chunks'][0]['RawHistory']) == original[1:older[0]['Time']['end']]
        finally:
            await manager.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize('case', ['disabled', 'not_due', 'malformed'])
def test_zero_variance_threshold_does_not_archive_without_valid_inspection(env, monkeypatch, case):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    messages = _conversation(1)

    async def run():
        supervisor = InspectStub()
        supervisor.settings.variance_threshold = 0
        if case == 'disabled':
            supervisor.settings.enabled = False
        elif case == 'not_due':
            supervisor.settings.check_interval = 2
        else:
            async def malformed(snapshot):
                supervisor.snapshots.append(copy.deepcopy(snapshot))
                return {'compact': 'not a bool', 'reason': None, 'values': [0.0, 1.0]}
            supervisor.inspect = malformed

        manager, session = await _manager(env, supervisor)
        try:
            history, metrics = await manager.history_context.prepare(session, messages)
            assert metrics['archived_chunks'] == 0
            assert metrics['compact_reason'] == ''
            assert supervisor.summary_calls == []
            assert any(item.get('role') == 'assistant' and 'old answer 0' in item.get('content', '')
                       for item in history)
            assert any(item.get('role') == 'user' and 'latest user request' in item.get('content', '')
                       for item in history)
            assert not (env.directory(session.id) / 'context' / 'manifest.json').exists()
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_reservation_included_in_soft_threshold_then_compaction_occurs(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .8, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 750, raising=False)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100_000)
    messages = _conversation(8)

    async def run():
        supervisor = InspectStub(high_variance=True)
        manager, session = await _manager(env, supervisor)
        try:
            _, baseline = await manager.history_context.prepare(session, messages)
            used = baseline['total_chars'] + baseline['reserved_chars']
            assert baseline['archived_chunks'] == 0
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', math.ceil(used / .79))
            _, low = await manager.history_context.prepare(session, messages)
            assert low['utilization'] < .8
            assert low['archived_chunks'] == 0
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', math.floor(used / .81))
            history, high = await manager.history_context.prepare(session, messages)
            assert .8 <= used / config.MODEL_INPUT_CHARS < 1
            assert high['compact_reason'] == 'high_utilization_and_variance'
            assert high['archived_chunks'] >= 1
            assert 'latest user request' in json.dumps(history, ensure_ascii=False)
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_tool_call_and_paired_result_are_archived_together_but_latest_user_is_not(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .1, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 200, raising=False)
    call = {'id': 'old-tool', 'type': 'function',
            'function': {'name': 'read_file', 'arguments': '{}'}}
    messages = [
        {'role': 'system', 'content': 'System instructions.'},
        {'role': 'user', 'content': 'old user request'},
        {'role': 'assistant', 'content': '', 'tool_calls': [call]},
        {'role': 'tool', 'tool_call_id': 'old-tool', 'content': 'historical result'},
        {'role': 'assistant', 'content': 'followup answer'},
    ]
    for index in range(4):
        messages.extend([{'role': 'user', 'content': f'next request {index}'},
                         {'role': 'assistant', 'content': f'next answer {index}'}])
    messages.append({'role': 'user', 'content': 'LATEST_UNARCHIVED_USER'})

    async def run():
        manager, session = await _manager(env, InspectStub(high_variance=True))
        try:
            history, _ = await manager.history_context.prepare(session, messages)
            manifest = json.loads((env.directory(session.id) / 'context' / 'manifest.json').read_text())
            tool_chunk = [item for item in manifest['chunks']
                          if item['Time']['start'] <= 2 < item['Time']['end']]
            assert len(tool_chunk) == 1
            chunk = tool_chunk[0]
            raw = json.loads((env.directory(session.id) / 'context' / 'chunks' /
                              (chunk['ID'] + '.json')).read_text())['RawHistory']
            parsed = json.loads(raw)
            assert any(item.get('tool_calls') for item in parsed)
            assert any(item.get('tool_call_id') == 'old-tool' for item in parsed)
            assert all(item['Time']['end'] <= len(messages) - 1 for item in manifest['chunks'])
            assert 'LATEST_UNARCHIVED_USER' in json.dumps(history, ensure_ascii=False)
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_inspector_timeout_is_reported_without_unrequested_compaction(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 750, raising=False)

    class SlowInspect(InspectStub):
        def __init__(self):
            super().__init__(timeout=.02)

        async def inspect(self, snapshot):
            self.snapshots.append(copy.deepcopy(snapshot))
            await asyncio.sleep(.1)
            return {'compact': True, 'reason': 'too late',
                    'values': [0.0] * len(snapshot['chunks'])}

    async def run():
        supervisor = SlowInspect()
        manager, session = await _manager(env, supervisor)
        try:
            _, metrics = await manager.history_context.prepare(session, _conversation(1))
            assert supervisor.snapshots
            assert metrics['supervisor'] == 'unavailable:TimeoutError'
            assert metrics['compact_reason'] == ''
            assert metrics['archived_chunks'] == 0
            assert supervisor.summary_calls == []
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_hard_budget_with_only_protected_groups_and_large_schema_checkpoints_cleanly(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .8, raising=False)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 14_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100_000)
    messages = _conversation(1)
    original = copy.deepcopy(messages)

    async def run():
        manager, session = await _manager(env)
        try:
            with pytest.raises(ContextBudgetError) as raised:
                await manager.history_context.prepare(session, messages,
                    schemas=[{'description': 'S' * 10_000}])
            metrics = raised.value.metrics
            assert metrics['compact_reason'] == 'context_insufficient'
            assert metrics['reserved_chars'] >= config.CONTEXT_RESERVE_SEARCH_CHARS
            assert messages == original
            path = env.directory(session.id) / 'context' / 'manifest.json'
            if path.exists():
                manifest = json.loads(path.read_text())
                assert all(item['Time']['end'] <= len(messages) - 1 for item in manifest['chunks'])
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_disabled_supervisor_reports_disabled_and_makes_no_inspection_request(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 750, raising=False)

    async def run():
        supervisor = InspectStub()
        supervisor.settings.enabled = False
        manager, session = await _manager(env, supervisor)
        try:
            _, metrics = await manager.history_context.prepare(session, _conversation())
            assert metrics['supervisor'] == 'disabled'
            assert supervisor.snapshots == []
            assert metrics['archived_chunks'] == 0
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_malformed_supervisor_decision_cannot_trigger_variance_compaction(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 750, raising=False)

    class MalformedInspect(InspectStub):
        async def inspect(self, snapshot):
            self.snapshots.append(copy.deepcopy(snapshot))
            return {'compact': 'not a bool', 'reason': None,
                    'values': [float(index % 2) for index in range(len(snapshot['chunks']))]}

    async def run():
        supervisor = MalformedInspect()
        manager, session = await _manager(env, supervisor)
        try:
            _, metrics = await manager.history_context.prepare(session, _conversation(1))
            assert supervisor.snapshots
            assert metrics['supervisor'].startswith('unavailable:')
            assert metrics['compact_reason'] == ''
            assert metrics['archived_chunks'] == 0
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_supervisor_provider_without_empty_tools_support_still_inspects_without_tool_authority():
    from agent_core.core.context_supervisor import SupervisorAgent, SupervisorSettings

    class ProviderCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if 'tools' in kwargs or 'tool_choice' in kwargs:
                raise ValueError('provider rejects empty tools and tool_choice')
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({'compact': False, 'reason': 'safe', 'values': [0.5]}),
                tool_calls=None))], usage=None)

    async def run():
        completions = ProviderCompletions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        settings = SupervisorSettings(enabled=True, api_key='test',
                                      base_url='https://example.org/v1', model='judge')
        supervisor = SupervisorAgent(settings=settings, client=client)
        result = await supervisor.inspect({'chunks': [{'ID': 'a' * 32}]})
        assert result == {'compact': False, 'reason': 'safe', 'values': [0.5]}
        assert len(completions.calls) == 1
        assert 'tools' not in completions.calls[0]
        assert 'tool_choice' not in completions.calls[0]

    asyncio.run(run())


def test_supervisor_rejects_provider_tool_calls_even_without_tool_request_fields():
    from agent_core.core.context_supervisor import SupervisorAgent, SupervisorSettings

    class MaliciousCompletions:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({'compact': False, 'reason': 'safe', 'values': [0.5]}),
                tool_calls=[{'id': 'unexpected'}]))], usage=None)

    async def run():
        client = SimpleNamespace(chat=SimpleNamespace(completions=MaliciousCompletions()))
        settings = SupervisorSettings(enabled=True, api_key='test',
                                      base_url='https://example.org/v1', model='judge')
        supervisor = SupervisorAgent(settings=settings, client=client)
        with pytest.raises(ValueError, match='工具'):
            await supervisor.inspect({'chunks': [{'ID': 'a' * 32}]})

    asyncio.run(run())


def test_supervisor_default_sdk_client_sends_stable_independent_session_header(monkeypatch):
    import openai
    from agent_core.core.context_supervisor import SupervisorAgent, SupervisorSettings

    observed = []

    def respond(request):
        observed.append(request.headers.get('x-opencode-session'))
        return httpx.Response(200, json={
            'id': 'completion-test', 'object': 'chat.completion', 'created': 1,
            'model': 'judge', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps({
                    'compact': False, 'reason': 'safe', 'values': [0.5]})}}],
        })

    real_client = openai.AsyncOpenAI
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    def make_client(**kwargs):
        return real_client(http_client=transport_client, **kwargs)

    monkeypatch.setattr(openai, 'AsyncOpenAI', make_client)

    async def run():
        settings = SupervisorSettings(enabled=True, api_key='test',
                                      base_url='https://example.test/v1', model='judge')
        supervisor = SupervisorAgent(settings=settings)
        try:
            for _ in range(2):
                result = await supervisor.inspect({'chunks': [{'ID': 'a' * 32}]})
                assert result['values'] == [0.5]
            assert len(observed) == 2
            assert observed[0] and observed[0] == observed[1]
            uuid.UUID(observed[0])
        finally:
            await supervisor.close()

    asyncio.run(run())


def test_inspection_request_respects_real_1024_character_limit(env, monkeypatch):
    from agent_core.core.context_supervisor import SupervisorAgent, SupervisorSettings

    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 100_000, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .01)
    observed = []

    class ProviderCompletions:
        async def create(self, **kwargs):
            system, user = kwargs['messages']
            actual_chars = len(system['content']) + len(user['content'])
            snapshot = json.loads(user['content'])
            observed.append((actual_chars, snapshot))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({'compact': False, 'reason': 'bounded',
                                    'values': [0.5] * len(snapshot['chunks'])}),
                tool_calls=None))], usage=None)

    async def run():
        async def close():
            return None

        client = SimpleNamespace(chat=SimpleNamespace(completions=ProviderCompletions()), close=close)
        settings = SupervisorSettings(enabled=True, api_key='test',
            base_url='https://example.org/v1', model='judge', max_input_chars=1024,
            check_interval=1, max_chunks=24, timeout=2)
        supervisor = SupervisorAgent(settings=settings, client=client)
        manager, session = await _manager(env, supervisor)
        try:
            _, metrics = await manager.history_context.prepare(session, _conversation(1, marker=True))
            assert metrics['supervisor'] == 'evaluated'
            assert len(observed) == 1
            actual_chars, snapshot = observed[0]
            assert actual_chars <= 1024
            assert len(snapshot['chunks']) >= 2
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_extractive_fallback_preserves_tool_name_target_and_result_fact(env, monkeypatch):
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 500, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .1, raising=False)
    target = 'reports/final-summary.md'
    tool_call = {'id': 'write-old', 'type': 'function',
                 'function': {'name': 'create_file', 'arguments': json.dumps({
                     'path': target, 'content': 'LONG_BODY_' + 'X' * 10_000})}}
    messages = [
        {'role': 'system', 'content': 'System instructions.'},
        {'role': 'user', 'content': 'make an old report'},
        {'role': 'assistant', 'content': '', 'tool_calls': [tool_call]},
        {'role': 'tool', 'tool_call_id': 'write-old',
         'content': 'WRITE_FAILED_PERMISSION_DENIED; file was not created'},
        {'role': 'assistant', 'content': 'report write failed'},
        {'role': 'user', 'content': 'second request'},
        {'role': 'assistant', 'content': 'second answer'},
        {'role': 'user', 'content': 'third request'},
        {'role': 'assistant', 'content': 'third answer'},
        {'role': 'user', 'content': 'current request'},
    ]

    class FailingSummarizer(InspectStub):
        def __init__(self):
            super().__init__(high_variance=True)

        async def summarize(self, previous_summary, messages, *, kind='session'):
            self.summary_calls.append((kind, copy.deepcopy(messages)))
            raise RuntimeError('provider rejected summary')

    async def run():
        manager, session = await _manager(env, FailingSummarizer())
        try:
            await manager.history_context.prepare(session, messages)
            folder = env.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            tool_chunks = [item for item in manifest['chunks']
                           if item['Time']['start'] <= 2 < item['Time']['end']]
            assert len(tool_chunks) == 1
            summary = tool_chunks[0]['Summary']
            assert tool_chunks[0]['summary_kind'] == 'extractive_fallback'
            assert 'create_file' in summary
            assert target in summary
            assert 'WRITE_FAILED_PERMISSION_DENIED' in summary
            assert target in manifest['session_summary']
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_hard_budget_archives_completed_post_user_tool_group_without_orphaning_results(env, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 23_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 100_000)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_RATIO', .8, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_HISTORY_CHUNK_CHARS', 1000, raising=False)
    calls = [
        {'id': 'read-a', 'type': 'function',
         'function': {'name': 'read_file', 'arguments': '{"path":"a.txt"}'}},
        {'id': 'read-b', 'type': 'function',
         'function': {'name': 'read_file', 'arguments': '{"path":"b.txt"}'}},
    ]
    messages = [
        {'role': 'system', 'content': 'System instructions.'},
        {'role': 'user', 'content': 'CURRENT_USER_NEEDS_SUMMARY'},
        {'role': 'assistant', 'content': '', 'tool_calls': calls},
        {'role': 'tool', 'tool_call_id': 'read-a', 'content': 'RESULT_A_' + 'A' * 15_000},
        {'role': 'tool', 'tool_call_id': 'read-b', 'content': 'RESULT_B_' + 'B' * 15_000},
    ]
    original = copy.deepcopy(messages)

    async def run():
        manager, session = await _manager(env)
        try:
            history, metrics = await manager.history_context.prepare(session, messages,
                schemas=[{'name': 'read_file', 'description': 'read a file'}])
            assert metrics['utilization'] <= 1
            assert 'CURRENT_USER_NEEDS_SUMMARY' in json.dumps(history, ensure_ascii=False)
            assert not any(item.get('role') == 'tool' for item in history)
            assert messages == original
            folder = env.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            assert len(manifest['chunks']) == 1
            meta = manifest['chunks'][0]
            assert (meta['Time']['start'], meta['Time']['end']) == (2, 5)
            chunk = json.loads((folder / 'chunks' / (meta['ID'] + '.json')).read_text())
            archived = json.loads(chunk['RawHistory'])
            assert archived == original[2:5]
            assert {item['tool_call_id'] for item in archived if item['role'] == 'tool'} == {'read-a', 'read-b'}
        finally:
            await manager.shutdown()

    asyncio.run(run())


def _supervisor_with_reply(content, *, finish_reason='stop', thinking_mode=None):
    from agent_core.core.context_supervisor import SupervisorAgent, SupervisorSettings

    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=None))], usage=None)

    options = {'enabled': True, 'api_key': 'test', 'base_url': 'https://example.org/v1',
               'model': 'judge'}
    if thinking_mode is not None:
        options['thinking_mode'] = thinking_mode
    settings = SupervisorSettings(**options)
    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    return SupervisorAgent(settings=settings, client=client), calls


def test_supervisor_auto_thinking_sends_no_extra_body_and_disabled_is_explicit():
    response = json.dumps({'compact': False, 'reason': 'safe', 'values': [0.5]})

    async def run():
        automatic, automatic_calls = _supervisor_with_reply(response)
        assert automatic.settings.thinking_mode == 'auto'
        await automatic.inspect({'chunks': [{'ID': 'a' * 32}]})
        assert 'extra_body' not in automatic_calls[0]

        disabled, disabled_calls = _supervisor_with_reply(response, thinking_mode='disabled')
        await disabled.inspect({'chunks': [{'ID': 'a' * 32}]})
        assert disabled_calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}

    asyncio.run(run())


@pytest.mark.parametrize('operation', ['inspect', 'summarize'])
def test_supervisor_rejects_truncated_finish_reason_even_with_valid_json(operation):
    reply = ({'compact': False, 'reason': 'safe', 'values': [0.5]} if operation == 'inspect'
             else {'summary': 'apparently complete'})

    async def run():
        supervisor, _ = _supervisor_with_reply(json.dumps(reply), finish_reason='length')
        with pytest.raises(ValueError, match='截断|length|完成'):
            if operation == 'inspect':
                await supervisor.inspect({'chunks': [{'ID': 'a' * 32}]})
            else:
                await supervisor.summarize('', [{'role': 'user', 'content': 'old fact'}])

    asyncio.run(run())


def test_supervisor_accepts_single_json_code_fence_without_accepting_prose_or_multiple_documents():
    document = json.dumps({'compact': False, 'reason': 'safe', 'values': [0.5]})

    async def run():
        fenced, _ = _supervisor_with_reply('```json\n' + document + '\n```')
        assert (await fenced.inspect({'chunks': [{'ID': 'a' * 32}]}))['values'] == [0.5]

        for content in ('preface\n```json\n' + document + '\n```',
                        '```json\n' + document + '\n```\nextra prose',
                        '```json\n' + document + '\n```\n```json\n' + document + '\n```'):
            invalid, _ = _supervisor_with_reply(content)
            with pytest.raises(ValueError):
                await invalid.inspect({'chunks': [{'ID': 'a' * 32}]})

    asyncio.run(run())
