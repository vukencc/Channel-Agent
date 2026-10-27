"""Regressions for Compact work under a tight context budget."""

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.history_context import HistoryContext
from agent_core.core.llm import TOOL_SCHEMAS
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.tool_settings import bounded_schemas, model_tools, selected_schemas


class CountingSupervisor:
    def __init__(self):
        self.settings = SimpleNamespace(enabled=True, check_interval=1, max_chunks=24,
                                        variance_threshold=.75, max_input_chars=12000, timeout=5)
        self.summaries = []

    async def inspect(self, snapshot):
        return {'compact': False, 'reason': 'same value', 'values': [.5] * len(snapshot['chunks'])}

    async def summarize(self, previous, messages, *, kind='session'):
        self.summaries.append(kind)
        return 'model summary'


@pytest.fixture
def overhead_store(tmp_path, monkeypatch, sandbox_env):
    for name in ('ENABLE_STRUCTURED_CONTEXT', 'ENABLE_TASK_PLANS', 'ENABLE_AGENT_TASKS',
                 'ENABLE_COMMAND_JOBS', 'MEMORY_AUTO_EXTRACT'):
        monkeypatch.setattr(config, name, False)
    settings = {'MODEL_INPUT_CHARS': 64000, 'MODEL_INPUT_TOKENS': 16000,
                'CONTEXT_HISTORY_KEEP_GROUPS': 2, 'CONTEXT_HISTORY_CHUNK_CHARS': 8000,
                'CONTEXT_SUMMARY_CHARS': 2000, 'CONTEXT_RESERVE_USER_CHARS': 4000,
                'CONTEXT_RESERVE_SEARCH_CHARS': 8000, 'CONTEXT_RESERVE_USER_TOKENS': 1000,
                'CONTEXT_RESERVE_SEARCH_TOKENS': 2000, 'CONTEXT_COMPACT_RATIO': .9,
                'CONTEXT_COMPACT_TARGET_RATIO': .75, 'CONTEXT_COMPACT_MIN_NEW_GROUPS': 4}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _large_tool_history():
    messages = [{'role': 'system', 'content': 'Keep permissions and archive provenance.'},
                {'role': 'user', 'content': 'Continue the engineering task.'}]
    for index in range(6):
        call_id = f'read-{index}'
        messages.extend([
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': call_id, 'type': 'function', 'function': {
                    'name': 'read_file', 'arguments': json.dumps({'path': f'data-{index}.txt'})}}]},
            {'role': 'tool', 'tool_call_id': call_id,
             'content': f'READ_FACT_{index:03d}; ' + '摘' * 3500},
        ])
    return messages


@pytest.mark.parametrize('previous_kind', ['extractive_fallback', 'none'])
def test_hard_budget_large_batch_uses_local_summary_and_preserves_archive(
        overhead_store, previous_kind):
    async def run():
        supervisor = CountingSupervisor()
        manager = SessionManager(overhead_store, model=lambda *_args, **_kwargs: None)
        session = manager.create()
        service = HistoryContext(manager, supervisor=supervisor)
        messages = _large_tool_history()
        original = copy.deepcopy(messages)
        schemas = [{'type': 'function', 'function': {'name': 'read_file',
                    'description': '摘' * 4200 + 's' * 16300,
                    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}}}]
        runtime = '\nEngineering state: ' + '态' * 1500
        try:
            manifest = await service._manifest(session)
            if previous_kind == 'extractive_fallback':
                manifest['summary_kind'] = previous_kind
                manifest['session_summary'] = '[本地摘录；之前模型超时；原文可检索]'
            previews = service._chunks(session, messages, [(2, 4), (4, 6), (6, 8), (8, 10)])
            assert sum(len(json.dumps(chunk['messages'], ensure_ascii=False)) for chunk in previews) > 8000
            _, metrics = await service.prepare(session, messages, schemas=schemas, runtime=runtime)
            assert metrics['before_utilization'] >= 1
            assert metrics['compacted']
            if previous_kind == 'none':
                assert metrics['working_summary_chars'] < config.CONTEXT_SUMMARY_CHARS / 2
            assert metrics['summary_kind'] == 'extractive_budget'
            assert supervisor.summaries == []
            assert messages == original

            folder = overhead_store.directory(session.id) / 'context'
            durable = json.loads((folder / 'manifest.json').read_text())
            assert durable['summary_kind'] == 'extractive_budget'
            assert durable['revision'] == 1
            assert durable['chunks']
            for metadata in durable['chunks']:
                chunk = json.loads((folder / 'chunks' / (metadata['ID'] + '.json')).read_text())
                raw = json.loads(chunk['RawHistory'])
                assert raw == original[metadata['Time']['start']:metadata['Time']['end']]
                assert chunk['digest'] == hashlib.sha256(chunk['RawHistory'].encode()).hexdigest()
                native = {call['id'] for message in raw for call in message.get('tool_calls', [])}
                assert all(message['tool_call_id'] in native for message in raw
                           if message['role'] == 'tool')
        finally:
            await manager.shutdown()

    asyncio.run(run())


def test_schema_titles_are_removed_only_from_schema_nodes():
    literal = {'title': 'literal value', 'nested': {'title': 'still literal'}}
    schema = {'type': 'function', 'function': {'name': 'example', 'description': 'Keep this.',
        'parameters': {'type': 'object', 'title': 'Root annotation',
            'properties': {
                'title': {'type': 'string', 'title': 'Property annotation', 'default': 'User title'},
                'payload': {'type': 'object', 'title': 'Payload annotation',
                    'default': copy.deepcopy(literal), 'const': copy.deepcopy(literal),
                    'enum': [copy.deepcopy(literal)], 'examples': [copy.deepcopy(literal)],
                    'properties': {'nested': {'type': 'string', 'title': 'Nested annotation'}}}},
            'required': ['title'], '$defs': {'item': {'type': 'string', 'title': 'Definition annotation'}},
            'allOf': [{'$ref': '#/$defs/item', 'title': 'Branch annotation'}]}}}
    original = copy.deepcopy(schema)
    trimmed = bounded_schemas([schema], None)[0]
    parameters = trimmed['function']['parameters']
    assert 'title' not in parameters
    assert 'title' in parameters['properties']
    assert 'title' not in parameters['properties']['title']
    assert 'title' not in parameters['properties']['payload']
    assert 'title' not in parameters['properties']['payload']['properties']['nested']
    assert 'title' not in parameters['$defs']['item']
    assert 'title' not in parameters['allOf'][0]
    assert parameters['properties']['payload']['default'] == literal
    assert parameters['properties']['payload']['const'] == literal
    assert parameters['properties']['payload']['enum'] == [literal]
    assert parameters['properties']['payload']['examples'] == [literal]
    assert parameters['properties']['title']['default'] == 'User title'
    assert parameters['required'] == ['title']
    assert parameters['allOf'][0]['$ref'] == '#/$defs/item'
    assert trimmed['function']['name'] == 'example'
    assert trimmed['function']['description'] == 'Keep this.'
    assert schema == original


def test_registered_tool_schemas_shrink_without_mutating_registry():
    original = copy.deepcopy(TOOL_SCHEMAS)
    before = len(json.dumps(TOOL_SCHEMAS, ensure_ascii=False))
    before_titles = json.dumps(TOOL_SCHEMAS, ensure_ascii=False).count('"title":')
    bounded = bounded_schemas(TOOL_SCHEMAS, None)
    with model_tools(None):
        selected = selected_schemas(TOOL_SCHEMAS)
    names = [row['function']['name'] for row in TOOL_SCHEMAS]
    # 自动目录仅移除已有替代接口的兼容名称，其余顺序与注册表一致。
    replacements = {'delegate': 'create_session', 'task_status': 'wait_agent_task',
                    'job_status': 'inspect_command_job', 'job_logs': 'inspect_command_job'}
    expected = [name for name in names if replacements.get(name) not in names]
    assert [row['function']['name'] for row in bounded] == expected
    assert [row['function']['name'] for row in selected] == expected
    assert bounded == selected
    assert len(json.dumps(bounded, ensure_ascii=False)) < before
    assert before_titles > 0
    assert json.dumps(bounded, ensure_ascii=False).count('"title":') < before_titles
    assert TOOL_SCHEMAS == original
