"""Regression contracts for spacing context compactions across growing turns."""

import asyncio
import copy
import json
import math
from types import SimpleNamespace

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.history_context import HistoryContext, _groups
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


class SupervisorStub:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True, check_interval=1,
                                        variance_threshold=.5, timeout=5,
                                        max_input_chars=12000, max_chunks=24)
        self.inspections = []
        self.summaries = []

    async def inspect(self, snapshot):
        self.inspections.append(copy.deepcopy(snapshot))
        return {'compact': True, 'reason': 'different values',
                'values': [float(index % 2) for index in range(len(snapshot['chunks']))]}

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summaries.append((kind, copy.deepcopy(messages)))
        return 'archived facts'


@pytest.fixture
def compact_case(tmp_path, monkeypatch, sandbox_env):
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
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_TARGET_RATIO', .75, raising=False)
    monkeypatch.setattr(config, 'CONTEXT_COMPACT_MIN_NEW_GROUPS', 4, raising=False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _messages(count=8):
    messages = [{'role': 'system', 'content': 'Keep the current user instruction.'}]
    for index in range(count):
        messages.append({'role': 'user', 'content': f'earlier-{index} ' + 'u' * 180})
        messages.append({'role': 'assistant', 'content': f'answer-{index} ' + 'a' * 180})
    messages.append({'role': 'user', 'content': 'current user request'})
    return messages


def _set_utilization(monkeypatch, service, session, messages, target, *, schemas=None):
    """Choose a char limit from the request that prepare is about to compose."""
    manifest = service.cache.get(session.id) or service._load(session)
    groups = _groups(messages)
    current = len(messages) - 1
    covered = [(item['Time']['start'], item['Time']['end']) for item in manifest['chunks']]
    available = [group for group in groups if group[0] != current and not any(
        start <= group[0] and group[1] <= end for start, end in covered)]
    keep = min(config.CONTEXT_HISTORY_KEEP_GROUPS, len(available))
    protected = available[-keep:] if keep else []
    eligible = [group for group in available if group not in protected]
    previews = service._chunks(session, messages, eligible)
    composed = service._compose(messages, manifest, previews, protected, current, '', '')
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1_000_000)
    monkeypatch.setattr(config, 'MODEL_INPUT_TOKENS', 1_000_000)
    schemas = schemas or []
    baseline = service._metrics(composed, schemas, messages[current], messages)
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', math.ceil(baseline['total_chars'] / target))
    actual = service._metrics(composed, schemas, messages[current], messages)['utilization']
    assert target - .02 <= actual <= target + .02


def _next_turn(messages, *, answer='done', request='next request'):
    return [*messages, {'role': 'assistant', 'content': answer},
            {'role': 'user', 'content': request}]


def test_soft_compaction_reaches_lower_watermark_when_budget_allows(compact_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(compact_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages(count=2)
        messages[3]['content'] += 'important older detail ' * 30
        try:
            monkeypatch.setattr(config, 'CONTEXT_SUMMARY_CHARS', 32)
            _set_utilization(monkeypatch, service, session, messages, .93)
            _, metrics = await service.prepare(session, messages, schemas=[])
            assert metrics['compacted']
            assert metrics['compact_reason'] == 'high_utilization_and_variance'
            assert metrics['utilization'] <= config.CONTEXT_COMPACT_TARGET_RATIO
            assert len([kind for kind, _ in supervisor.summaries if kind == 'session']) == 1
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_soft_cooldown_persists_after_service_reload_and_allows_growth(compact_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(compact_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages()
        try:
            _set_utilization(monkeypatch, service, session, messages, .93)
            _, first = await service.prepare(session, messages, schemas=[])
            assert first['compacted']
            manifest_path = compact_case.directory(session.id) / 'context' / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            assert manifest['last_compact_message_count'] == len(messages)
            service = HistoryContext(manager, supervisor=supervisor)
            small_growth = _next_turn(messages)
            _set_utilization(monkeypatch, service, session, small_growth, .93)
            inspected = len(supervisor.inspections)
            summarized = len(supervisor.summaries)
            _, cooled = await service.prepare(session, small_growth, schemas=[])
            assert .9 <= cooled['utilization'] < 1
            assert not cooled['compacted']
            assert cooled['supervisor'] == 'cooldown'
            assert len(supervisor.inspections) == inspected
            assert len(supervisor.summaries) == summarized
            assert json.loads(manifest_path.read_text())['revision'] == manifest['revision']
            # 最新用户输入不计入可归档历史；再增长两轮才达到四个完整新增组。
            enough_growth = _next_turn(_next_turn(small_growth))
            _set_utilization(monkeypatch, service, session, enough_growth, .93)
            await service.prepare(session, enough_growth, schemas=[])
            assert len(supervisor.inspections) > inspected
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_hard_budget_bypasses_cooldown_without_inspection_and_preserves_tool_pair(compact_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(compact_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages()
        try:
            _set_utilization(monkeypatch, service, session, messages, .93)
            _, first = await service.prepare(session, messages, schemas=[])
            assert first['compacted']
            call_id = 'large-read'
            grown = [*messages,
                     {'role': 'assistant', 'content': '', 'tool_calls': [{
                         'id': call_id, 'type': 'function', 'function': {
                             'name': 'read_file', 'arguments': '{"path":"sample.txt"}'}}]},
                     {'role': 'tool', 'tool_call_id': call_id,
                      'content': 'large result ' + 'x' * 4000},
                     {'role': 'user', 'content': 'latest request'}]
            original = copy.deepcopy(grown)
            _set_utilization(monkeypatch, service, session, grown, 1.05)
            inspected = len(supervisor.inspections)
            _, after = await service.prepare(session, grown, schemas=[])
            assert after['compacted']
            assert after['compact_reason'] == 'context_insufficient'
            assert after['utilization'] <= 1
            assert len(supervisor.inspections) == inspected
            assert grown == original
            folder = compact_case.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            archived = [json.loads((folder / 'chunks' / f"{chunk['ID']}.json").read_text())
                        for chunk in manifest['chunks']]
            paired = [json.loads(chunk['RawHistory']) for chunk in archived if call_id in chunk['RawHistory']]
            assert len(paired) == 1
            assert any(message.get('tool_call_id') == call_id for message in paired[0])
            assert any(any(call['id'] == call_id for call in message.get('tool_calls', []))
                       for message in paired[0])
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_unreachable_target_keeps_request_under_hard_budget(compact_case, monkeypatch):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(compact_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages(count=2)
        messages[0]['content'] += ' FIXED_SYSTEM' * 350
        messages[-1]['content'] += ' CURRENT_INPUT_SENTINEL' * 60
        original = copy.deepcopy(messages)
        schemas = [{'type': 'function', 'function': {
            'name': 'noop', 'description': 'FIXED_SCHEMA' * 100,
            'parameters': {'type': 'object', 'properties': {}}}}]
        try:
            _set_utilization(monkeypatch, service, session, messages, .93, schemas=schemas)
            empty = service._compose(messages, service._load(session), [], [], len(messages) - 1, '', '')
            minimum = service._metrics(empty, schemas, messages[-1], messages)['utilization']
            assert config.CONTEXT_COMPACT_TARGET_RATIO < minimum < 1
            history, metrics = await service.prepare(session, messages, schemas=schemas)
            assert metrics['compacted']
            assert config.CONTEXT_COMPACT_TARGET_RATIO < metrics['utilization'] <= 1
            assert not metrics['compact_target_reached']
            assert messages == original
            assert 'CURRENT_INPUT_SENTINEL' in history[-1]['content']
            assert len([kind for kind, _ in supervisor.summaries if kind == 'session']) == 1
            folder = compact_case.directory(session.id) / 'context'
            manifest = json.loads((folder / 'manifest.json').read_text())
            archived = [json.loads((folder / 'chunks' / f"{chunk['ID']}.json").read_text())
                        for chunk in manifest['chunks']]
            assert all(json.loads(chunk['RawHistory']) == original[chunk['Time']['start']:chunk['Time']['end']]
                       for chunk in archived)
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('field,value', [
    ('last_compact_message_count', -1),
    ('last_compact_message_count', True),
    ('last_compact_message_count', '1'),
    ('last_compact_utilization', float('inf')),
])
def test_invalid_compaction_manifest_fails_closed_without_partial_write(compact_case, monkeypatch, field, value):
    async def run():
        supervisor = SupervisorStub()
        manager = SessionManager(compact_case, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _messages()
        try:
            _set_utilization(monkeypatch, service, session, messages, .93)
            _, first = await service.prepare(session, messages, schemas=[])
            assert first['compacted']
            directory = compact_case.directory(session.id)
            manifest_path = directory / 'context' / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest[field] = value
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
            before = {path.relative_to(directory): path.read_bytes()
                      for path in directory.rglob('*') if path.is_file()}
            inspected = len(supervisor.inspections)
            summarized = len(supervisor.summaries)
            reloaded = HistoryContext(manager, supervisor=supervisor)
            with pytest.raises(ValueError):
                await reloaded.prepare(session, _next_turn(messages), schemas=[])
            after = {path.relative_to(directory): path.read_bytes()
                     for path in directory.rglob('*') if path.is_file()}
            assert after == before
            assert len(supervisor.inspections) == inspected
            assert len(supervisor.summaries) == summarized
        finally:
            await manager.shutdown()
    asyncio.run(run())
