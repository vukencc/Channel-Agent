"""受控离线历史证据定位；真实私有发布/搜索/读取，不调用模型或重放客户记录。"""

import asyncio
import copy
import json
import threading
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.history_context import HistoryContext, _digest
from agent_core.core.session_service import session_service_context
from agent_core.core.storage import SessionStore
from agent_core.tools import context_history


RAG_CONTENT = 'RAG_SENTINEL 真实受控证据 中文🙂 "引号" \\换行\n第二行\t尾部'


def _call(identifier='same', name='rag_search', reasoning=''):
    return {'role': 'assistant', 'content': '', 'reasoning_content': reasoning,
            'tool_calls': [{'id': identifier, 'type': 'function',
                            'function': {'name': name, 'arguments': '{"query":"needle"}'}}]}


def _result(content=RAG_CONTENT, identifier='same'):
    return {'role': 'tool', 'tool_call_id': identifier, 'content': content}


def _wire(messages: list[dict], layout='compact', duplicate_content=False):
    """在生成真实 JSON 时记录期望字符区间，不用事后 find 或重序列化推断位置。"""
    ascii_only = layout in ('compact', 'reversed_escaped')
    whitespace = '' if layout == 'compact' else '\n \t '
    pieces, length, spans = [], 0, {}
    def emit(value):
        nonlocal length
        pieces.append(value)
        length += len(value)
    emit('[' + whitespace)
    for index, message in enumerate(messages):
        if index:
            emit(',' + whitespace)
        emit('{' + whitespace)
        items = list(message.items())
        if layout == 'reversed_escaped':
            items.reverse()
        if duplicate_content and message.get('role') == 'tool':
            items.append(('content', 'ambiguous duplicate content'))
        for position, (key, value) in enumerate(items):
            if position:
                emit(',' + whitespace)
            emit(json.dumps(key) + whitespace + ':' + whitespace)
            token = json.dumps(value, ensure_ascii=ascii_only)
            if key == 'content' and message.get('role') == 'tool':
                spans[index] = (length, length + len(token))
            emit(token)
        emit(whitespace + '}')
    emit(whitespace + ']')
    return ''.join(pieces), spans


class Archive:
    def __init__(self, store):
        self.store = store
        self.session = SimpleNamespace(id='a' * 32, cancelled=threading.Event(), deleting=False,
                                       forking=False, record={'messages': []})
        self.reader_enabled = True
        self.manager = SimpleNamespace(store=store, sessions={self.session.id: self.session},
                                       closing=False, maintenance=False, pending_saves=set())
        self.manager.session_tools = lambda owner: [{'name': 'history_search'}] + (
            [{'name': 'history_read'}] if self.reader_enabled else [])
        self.service = HistoryContext(self.manager, supervisor=object())
        self.manager.history_context = self.service
        self.records = []
        self.start = 9

    def add(self, messages, *, summary='needle 原始证据', layout='compact', duplicate_content=False):
        raw, spans = _wire(messages, layout, duplicate_content)
        parsed = json.loads(raw)
        identifier = f'{len(self.records) + 1:032x}'
        record = {'ID': identifier, 'Time': {'start': self.start, 'end': self.start + len(messages),
                  'created_at': '2026-11-16T00:00:00+00:00'}, 'Summary': summary,
                  'RawHistory': raw, 'digest': _digest(parsed), 'session_id': self.session.id,
                  'summary_kind': 'extractive_budget'}
        self.start += len(messages) + 1
        self.records.append(record)
        manifest = {'version': 1, 'owner_id': self.session.id, 'revision': len(self.records),
                    'session_summary': '', 'summary_kind': 'none', 'updated_at': '',
                    'chunks': [{k: v for k, v in row.items() if k not in {'RawHistory', 'session_id'}}
                               for row in self.records]}
        self.service._publish(self.session, [record], manifest)
        self.service.cache.pop(self.session.id, None)
        return record, spans

    def bytes(self):
        directory = self.store.directory(self.session.id) / 'context'
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    async def tool_read(self, arguments):
        with session_service_context(self.manager, self.session.id, asyncio.get_running_loop()):
            return await asyncio.to_thread(context_history.history_read, **arguments)

    async def tool_search(self, **kwargs):
        with session_service_context(self.manager, self.session.id, asyncio.get_running_loop()):
            return await asyncio.to_thread(context_history.history_search, 'needle', **kwargs)


@pytest.fixture
def archive(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 12000)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', tmp_path / 'rag-cache')
    store = SessionStore(tmp_path / 'state', sandbox_env)
    value = Archive(store)
    yield value
    asyncio.run(value.service.close())
    store.close()


def test_first_locator_page_reaches_rag_after_long_reasoning(archive):
    async def run():
        record, _ = archive.add([_call(reasoning='推理前缀' * 12000), _result()])
        before = archive.bytes()
        hits = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert hits and hits[0]['ID'] == record['ID']
        locations = hits[0].get('tool_results', [])
        if locations:
            shown_payload = await archive.tool_search(tool_name='rag_search')
            assert len(shown_payload) <= 1500
            shown = json.loads(shown_payload)
            assert shown[0]['tool_results'][0] == locations[0]
            locations = shown[0]['tool_results']
        # 旧接口没有 locator，只能从0读取；真实第一页会陷在 reasoning，不能触达工具证据。
        arguments = locations[0]['read_args'] if locations else {
            'chunk_id': record['ID'], 'before': 0, 'after': 0, 'offset': 0, 'limit': 6000}
        payload = await archive.tool_read(arguments)
        page = json.loads(payload)
        assert len(payload) <= 1500
        assert 'RAG_SENTINEL' in ''.join(chunk['RawHistory'] for chunk in page['chunks'])
        assert archive.bytes() == before
    asyncio.run(run())
@pytest.mark.parametrize('layout', ['pretty_cjk', 'reversed_escaped', 'whitespace'])
def test_locator_matches_original_token_span_and_global_message_index(archive, layout):
    async def run():
        first = _call(reasoning='长推理🙂' * 1000)
        first['content'] = RAG_CONTENT  # 与后面的工具正文相同，不能用第一次正文 find 冒充位置。
        record, spans = archive.add([first, _result()], layout=layout)
        before = archive.bytes()
        rows = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        locations = rows[0]['tool_results']
        assert len(locations) == 1
        location = locations[0]
        assert set(location) == {'tool_name', 'call_id', 'message_index', 'read_args', 'end_offset'}
        assert location['tool_name'] == 'rag_search' and location['call_id'] == 'same'
        assert location['message_index'] == record['Time']['start'] + 1
        start, end = spans[1]
        assert location['read_args'] == {'chunk_id': record['ID'], 'before': 0, 'after': 0,
                                         'offset': start, 'limit': min(6000, end - start)}
        assert location['end_offset'] == end
        assert json.loads(record['RawHistory'][start:end]) == RAG_CONTENT
        assert archive.bytes() == before
    asyncio.run(run())


def test_all_actual_tool_pages_reconstruct_only_the_original_content_token(archive):
    async def run():
        body = RAG_CONTENT + ' 中文🙂"\\\n' * 400
        record, spans = archive.add([_call(reasoning='old reasoning' * 2000), _result(body),
                                     _call('later', 'read_file'), _result('不得越界读取的后续正文', 'later')],
                                    layout='reversed_escaped')
        before = archive.bytes()
        rows = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        location = rows[0]['tool_results'][0]
        arguments = dict(location['read_args'])
        start, end = spans[1]
        offset, parts = arguments['offset'], []
        while offset < end:
            arguments['offset'] = offset
            arguments['limit'] = min(6000, end - offset)
            payload = await archive.tool_read(arguments)
            assert len(payload) <= 1500
            page = json.loads(payload)
            assert page['total_chars'] == len(record['RawHistory'])
            assert [chunk['ID'] for chunk in page['chunks']] == [record['ID']]
            assert page['chunks'][0]['chunk_offset'] == offset
            part = ''.join(chunk['RawHistory'] for chunk in page['chunks'])
            assert part and part == record['RawHistory'][offset:offset + len(part)]
            assert offset + len(part) <= end
            parts.append(part)
            next_offset = page['next_offset']
            assert next_offset == offset + len(part)
            assert offset < next_offset <= end
            offset = next_offset
        assert len(parts) > 1
        assert ''.join(parts) == record['RawHistory'][start:end]
        assert json.loads(''.join(parts)) == body
        assert location['end_offset'] == end
        assert archive.bytes() == before
    asyncio.run(run())


def test_reused_call_id_is_resolved_within_each_actual_batch(archive):
    async def run():
        messages = [_call('same', 'read_file'), _result('文件正文', 'same'),
                    _call('same', 'rag_search'), _result(RAG_CONTENT, 'same'),
                    _call('same', 'history_read'), _result('派生归档正文', 'same')]
        record, spans = archive.add(messages)
        rows = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        locations = rows[0]['tool_results']
        assert len(locations) == 1
        assert locations[0]['message_index'] == record['Time']['start'] + 3
        assert locations[0]['read_args']['offset'] == spans[3][0]
        assert locations[0]['end_offset'] == spans[3][1]
        assert locations[0]['tool_name'] == 'rag_search'
    asyncio.run(run())


@pytest.mark.parametrize('derived_tool', ['history_read', 'history_search', 'read_file'])
def test_nested_fake_rag_or_copied_history_result_is_not_top_level_evidence(archive, derived_tool):
    async def run():
        copied = json.dumps([_call('fake', 'rag_search'), _result(identifier='fake')], ensure_ascii=False)
        archive.add([_call('derived', derived_tool), _result(copied, 'derived')], summary='needle ' * 30)
        actual, _ = archive.add([_call('real'), _result(identifier='real')], summary='needle actual')
        rows = await archive.service.search(archive.session, 'needle', limit=20, tool_name='rag_search')
        assert [row['ID'] for row in rows] == [actual['ID']]
        assert [loc['call_id'] for loc in rows[0]['tool_results']] == ['real']
    asyncio.run(run())


def test_tool_filter_runs_before_rank_limit_so_twenty_derived_hits_cannot_hide_rag(archive, monkeypatch):
    async def run():
        from agent_core.core import history_search
        for i in range(20):
            archive.add([_call(f'derived-{i}', 'history_read'), _result('复制的检索摘要', f'derived-{i}')],
                        summary='needle ' * 40)
        target, _ = archive.add([_call('real'), _result(identifier='real')], summary='needle actual')
        old_semantics = await archive.service.search(archive.session, 'needle', limit=20)
        assert len(old_semantics) == 20 and target['ID'] not in {row['ID'] for row in old_semantics}
        ranked = []
        original = history_search.search_chunks
        def observed(chunks, *args, **kwargs):
            ranked.append([chunk['ID'] for chunk in chunks])
            return original(chunks, *args, **kwargs)
        monkeypatch.setattr(history_search, 'search_chunks', observed)
        rows = await archive.service.search(archive.session, 'needle', limit=1, tool_name='rag_search')
        assert ranked == [[target['ID']]]
        assert [row['ID'] for row in rows] == [target['ID']]
    asyncio.run(run())


def test_default_none_search_retains_summary_only_semantics_without_raw_io(archive, monkeypatch):
    async def run():
        record, _ = archive.add([_call(), _result()])
        def forbidden(*args, **kwargs):
            raise AssertionError('默认摘要搜索不能新增原文读取')
        monkeypatch.setattr(archive.service, '_read_records', forbidden)
        rows = await archive.service.search(archive.session, 'needle')
        assert [row['ID'] for row in rows] == [record['ID']]
        assert all('tool_results' not in row and 'RawHistory' not in row for row in rows)
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['digest', 'cross_session', 'missing_result', 'wrong_call',
                                   'duplicate_keys', 'duplicate_id_within_batch'])
def test_corrupt_or_ambiguous_archive_fails_closed_instead_of_silently_filtering(archive, fault):
    async def run():
        messages = [_call(), _result()]
        if fault == 'missing_result':
            messages.pop()
        elif fault == 'wrong_call':
            messages[-1]['tool_call_id'] = 'unmatched'
        elif fault == 'duplicate_id_within_batch':
            messages[0]['tool_calls'].append(copy.deepcopy(messages[0]['tool_calls'][0]))
        record, _ = archive.add(messages, duplicate_content=fault == 'duplicate_keys')
        if fault in ('digest', 'cross_session'):
            tampered = copy.deepcopy(record)
            if fault == 'digest':
                tampered['RawHistory'] = tampered['RawHistory'].replace('RAG_SENTINEL', 'ALTERED_BODY')
            else:
                tampered['session_id'] = 'c' * 32
            path = archive.store.directory(archive.session.id) / 'context' / 'chunks' / (record['ID'] + '.json')
            archive.store.atomic_write(path, json.dumps(tampered, ensure_ascii=False))
        before = archive.bytes()
        with pytest.raises((ValueError, PermissionError)):
            await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert archive.bytes() == before
    asyncio.run(run())


def test_cancelled_session_cannot_search_or_return_a_locator(archive):
    async def run():
        archive.add([_call(), _result()])
        before = archive.bytes()
        archive.session.cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert archive.bytes() == before
    asyncio.run(run())


def test_many_locators_are_omitted_as_whole_objects_with_truthful_count(archive):
    async def run():
        messages = []
        for i in range(12):
            messages.extend([_call(f'rag-{i}'), _result(RAG_CONTENT, f'rag-{i}')])
        archive.add(messages)
        rows = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert len(rows[0]['tool_results']) == 12
        original = copy.deepcopy(rows)
        payload = context_history._search_json(rows)
        assert len(payload) <= 1500
        selected = json.loads(payload)
        kept = selected[0]['tool_results']
        assert kept, '1500字符小页必须至少保留一个能装入的完整定位对象'
        assert selected[0]['tool_results_omitted_count'] == 12 - len(kept) > 0
        assert kept == original[0]['tool_results'][:len(kept)]
        assert all(set(loc) == set(original[0]['tool_results'][0]) for loc in kept)
        assert rows == original
    asyncio.run(run())


def test_locator_metadata_that_cannot_fit_fails_without_partial_json(archive, monkeypatch):
    async def run():
        archive.add([_call(), _result()])
        rows = await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        original = copy.deepcopy(rows)
        monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 40)
        with pytest.raises(ValueError):
            context_history._search_json(rows)
        assert rows == original
    asyncio.run(run())


def test_search_without_read_capability_does_not_advertise_callable_read_args(archive):
    async def run():
        archive.add([_call(), _result()])
        archive.reader_enabled = False
        payload = await archive.tool_search(tool_name='rag_search')
        assert len(payload) <= 1500
        rows = json.loads(payload)
        assert rows and rows[0]['tool_results']
        assert all('read_args' not in loc for row in rows for loc in row['tool_results'])
        hint = ' '.join(row.get('read_hint', '') for row in rows)
        assert 'history_read' in hint and any(word in hint for word in ('不可用', '未启用', '不能', '不可调用'))
    asyncio.run(run())


def test_none_tool_filter_does_not_break_old_search_stub_signature(monkeypatch):
    async def run():
        seen = []
        class LegacyHistory:
            async def search(self, session, query, limit=5, method='bm25'):
                seen.append((session.id, query, limit, method))
                return [{'ID': 'b' * 32, 'Summary': '原摘要', 'score': 1.0}]
        owner = SimpleNamespace(id='owner')
        manager = SimpleNamespace(history_context=LegacyHistory(), sessions={'owner': owner}, closing=False)
        monkeypatch.setattr(context_history, 'audit', lambda *a, **kw: None)
        with session_service_context(manager, 'owner', asyncio.get_running_loop()):
            payload = await asyncio.to_thread(context_history.history_search, 'needle', tool_name=None)
        assert json.loads(payload)[0]['ID'] == 'b' * 32
        assert seen == [('owner', 'needle', 5, 'bm25')]
    asyncio.run(run())


def test_tool_name_schema_rejects_ambiguous_identifiers():
    assert context_history.HistorySearchArgs(query='needle', tool_name='rag_search').tool_name == 'rag_search'
    for value in ('rag search', '../rag', '', 'a' * 65, 3, True):
        with pytest.raises(ValueError):
            context_history.HistorySearchArgs(query='needle', tool_name=value)


def test_archive_io_timeout_propagates_without_returning_partial_hits(archive, monkeypatch):
    async def run():
        archive.add([_call(), _result()])
        before = archive.bytes()
        def timed_out(*args, **kwargs):
            raise TimeoutError('受控原文读取期限')
        monkeypatch.setattr(archive.service, '_read_records', timed_out)
        with pytest.raises(TimeoutError):
            await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert archive.bytes() == before
    asyncio.run(run())


def test_call_pair_cannot_be_stitched_across_two_archive_chunks(archive):
    async def run():
        archive.add([_call()])
        archive.add([_result()])
        before = archive.bytes()
        with pytest.raises((ValueError, PermissionError)):
            await archive.service.search(archive.session, 'needle', tool_name='rag_search')
        assert archive.bytes() == before
    asyncio.run(run())

def test_real_session_manager_filtered_search_respects_no_reader_capability(
        tmp_path, monkeypatch, sandbox_env):
    from agent_core.core import sessions as sessions_module
    from agent_core.core.sessions import SessionManager
    from agent_core.core.tool_settings import bounded_schemas
    from agent_core.tools import all_schemas

    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', True)
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    # conftest 在导入后切换功能；此处仅刷新真实已注册 schema 快照，不替换能力查询或模型。
    monkeypatch.setattr(sessions_module, 'TOOL_SCHEMAS', all_schemas())

    async def run():
        store = SessionStore(tmp_path / 'real-state', sandbox_env)
        manager = SessionManager(store)
        try:
            session = manager.create(prompt='真实能力接线的受控历史夹具')
            manager.set_tool_names(session, ['history_search'])
            service = manager.history_context
            journal = [session.record['messages'][0], {'role': 'user', 'content': '受控归档输入'},
                       _call(), _result()]
            session.record['messages'] = copy.deepcopy(journal)
            manifest = await service._manifest(session)
            await service._compact(session, manifest, service._chunks(session, journal, [(2, 4)]), local=True)
            await manager.save(session)
            directory = store.directory(session.id)
            def original_bytes():
                return {str(path.relative_to(directory)): path.read_bytes()
                        for path in directory.rglob('*') if path.is_file()
                        and (path.name == 'messages.jsonl' or 'context' in path.parts)}
            before = original_bytes()
            selected_before = copy.deepcopy(session.record['tool_names'])
            tools_before = manager.session_tools(session)
            schemas_before = bounded_schemas(manager._session_schemas(session),
                                               manager._effective_session_tools(session))
            assert [item['name'] for item in tools_before] == ['history_search']
            assert [item['function']['name'] for item in schemas_before] == ['history_search']
            # 只派发获准的 history_search；真实 _tool 注入 caller_service/工具上下文与选用策略。
            payload = await manager._tool(session, {
                'id': 'allowed-search', 'type': 'function', 'function': {
                    'name': 'history_search', 'arguments': json.dumps({
                        'query': 'RAG_SENTINEL', 'tool_name': 'rag_search'})}}, evidence={})
            assert len(payload) <= 1500
            rows = json.loads(payload)
            assert rows and rows[0]['tool_results']
            assert all('read_args' not in locator for row in rows for locator in row['tool_results'])
            hint = ' '.join(row.get('read_hint', '') for row in rows)
            assert 'history_read' in hint
            assert any(word in hint for word in ('未启用', '不可用', '不能', '不可调用'))
            assert manager.session_tools(session) == tools_before
            assert session.record['tool_names'] == selected_before == ['history_search']
            assert bounded_schemas(manager._session_schemas(session),
                                   manager._effective_session_tools(session)) == schemas_before
            assert session.record['messages'] == journal
            assert original_bytes() == before
        finally:
            await manager.shutdown()
            store.close()
    asyncio.run(run())