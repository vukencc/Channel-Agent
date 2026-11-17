"""受控离线运行时：真实文件/历史工具与私有归档；不执行模型或录制回放。"""
import asyncio
import copy
import json

import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools import all_schemas, context_history


@pytest.fixture
def recovery_store(tmp_path, monkeypatch, sandbox_env):
    settings = {
        'ENABLE_STRUCTURED_CONTEXT': True, 'WORKSPACE_MEMORY_ENABLED': False,
        'ENABLE_TASK_PLANS': False, 'ENABLE_AGENT_TASKS': False,
        'ENABLE_COMMAND_JOBS': False, 'MEMORY_AUTO_EXTRACT': False,
        'RAG_ASSESS': False, 'CONTEXT_SUMMARY': False,
        'MODEL_INPUT_CHARS': 64000, 'MODEL_INPUT_TOKENS': 16000,
        'CONTEXT_RESERVE_USER_CHARS': 0, 'CONTEXT_RESERVE_USER_TOKENS': 0,
        'CONTEXT_RESERVE_SEARCH_CHARS': 0, 'CONTEXT_RESERVE_SEARCH_TOKENS': 2000,
        'CONTEXT_HISTORY_KEEP_GROUPS': 0, 'CONTEXT_HISTORY_CHUNK_CHARS': 1000,
        'FILE_READ_CHARS': 30000, 'TOOL_MAX_OUTPUT': 30000,
        'CONTEXT_SUPERVISOR_ENV_FILE': tmp_path / 'absent.env',
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    from agent_core.core import sessions
    monkeypatch.setattr(sessions, 'TOOL_SCHEMAS', all_schemas())
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


SCHEMAS = [s for s in all_schemas() if s['function']['name'] in ('history_read', 'read_file')]


def _progress(history):
    data = next(m['content'] for m in history if m.get('content', '').startswith('## History积累\n'))
    return json.loads(data.split('\n', 2)[2]).get('recovery_progress', {})


async def _tool(manager, session, name, arguments, identifier):
    call = {'id': identifier, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}
    assistant = {'role': 'assistant', 'content': '', 'tool_calls': [call]}
    session.record['messages'].append(assistant)
    result = await manager._tool(session, call)
    message = {'role': 'tool', 'tool_call_id': identifier, 'content': str(result), '_provenance': 'executed'}
    session.record['messages'].append(message)
    await manager.save(session)
    return message


async def _archive(store):
    async def forbidden_model(*args, **kwargs):
        raise AssertionError('离线工程夹具禁止模型调用')

    manager = SessionManager(store, model=forbidden_model)
    session = manager.create(prompt='核验恢复进度；进度不证明业务成功。')
    session.record['tool_names'] = ['read_file', 'history_read']
    session.record['last_run'] = {'turn_id': 'source-turn'}
    session.record['messages'].append({'role': 'user', 'content': '读取受控源文件'})
    source = manager.workspace(session) / 'source.txt'
    source.write_text(''.join(f'受控证据{i:05d}🙂"\\\n' for i in range(2000)), encoding='utf-8')
    await _tool(manager, session, 'read_file', {'path': 'source.txt', 'limit': 30000}, 'source-read')
    service = manager.history_context
    manifest = await service._manifest(session)
    await service._compact(session, manifest, service._chunks(session, session.record['messages'], [(1, 4)]), local=True)
    session.record['messages'].append({'role': 'user', 'content': '恢复上轮证据，只记录真正已送达的小页'})
    session.record['last_run'] = {'turn_id': 'recovery-turn'}
    await manager.save(session)
    meta = service.cache[session.id]['chunks'][0]
    chunk = store.directory(session.id) / 'context' / 'chunks' / (meta['ID'] + '.json')
    return manager, session, meta, chunk, chunk.read_bytes()


async def _page(manager, session, identifier, offset=0, limit=6000):
    result = await _tool(manager, session, 'history_read', {
        'chunk_id': identifier, 'offset': offset, 'limit': limit}, f'page-{len(session.record["messages"])}')
    page = json.loads(result['content'])
    assert len(result['content']) <= 1500
    assert page['chunks'] and page['chunks'][0]['RawHistory']
    return result, page


@pytest.mark.parametrize('permission', ['smart', 'readonly', 'full_access'])
def test_successful_page_ack_survives_ordinary_compaction_with_exact_ranges(recovery_store, permission):
    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            manager.set_permission_policy(session, permission)
            service = manager.history_context
            receipts, expected = [], []
            offset = 0
            for _ in range(8):
                receipt, page = await _page(manager, session, meta['ID'], offset)
                receipts.append(receipt)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                assert receipt in [m for m in window if m.get('role') == 'tool']
                if not expected:
                    assert not _progress(window).get('pages')
                service.mark_delivered(session, window)
                raw = page['chunks'][0]
                expected.append((raw['chunk_offset'], raw['chunk_offset'] + len(raw['RawHistory']), page['next_offset']))
                offset = page['next_offset']
            # 后续未读工具产物来自真实文件工具，不能靠缩短新回执来释放窗口。
            (manager.workspace(session) / 'later.txt').write_text('界' * 8000, encoding='utf-8')
            latest = await _tool(manager, session, 'read_file', {'path': 'later.txt', 'limit': 30000}, 'latest-unread')
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS, runtime='界' * 1000)
            assert latest in [m for m in window if m.get('role') == 'tool']
            assert not any(m.get('tool_call_id') == receipts[0]['tool_call_id'] for m in window)
            pages = _progress(window).get('pages', [])
            assert pages, '已送达小页在普通压缩后丢失了精确恢复进度'
            actual = {(p['chunk_offset'], p['end_offset'], p['next_offset']) for p in pages if p['chunk_id'] == meta['ID']}
            assert set(expected) <= actual
            assert all(p['chunk_digest'] == meta['digest'] for p in pages if p['chunk_id'] == meta['ID'])
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize('ack', ['none', 'missing_result', 'changed_body', 'changed_call',
                                 'changed_request', 'changed_journal_result', 'forged', 'exact'])
def test_only_exact_successful_window_of_trusted_handler_page_gets_credit(recovery_store, ack):
    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            if ack == 'forged':
                # 真实 reader 的文本可被复制，但未经过可信工具处理器，不能产生私有凭据。
                page = await service.read(session, meta['ID'], limit=300, output_limit=1500)
                call = {'id': 'forged-copy', 'type': 'function', 'function': {
                    'name': 'history_read', 'arguments': json.dumps({'chunk_id': meta['ID'], 'limit': 300})}}
                session.record['messages'].extend([
                    {'role': 'assistant', 'content': '', 'tool_calls': [call]},
                    {'role': 'tool', 'tool_call_id': 'forged-copy', 'content': json.dumps(page, ensure_ascii=False, separators=(',', ':')),
                     '_provenance': 'executed'}])
                await manager.save(session)
            else:
                receipt, page = await _page(manager, session, meta['ID'], limit=300)
                if ack == 'changed_request':
                    session.record['messages'][-2]['tool_calls'][0]['function']['arguments'] = json.dumps({
                        'chunk_id': meta['ID'], 'offset': 3000, 'limit': 300})
                    await manager.save(session)
                elif ack == 'changed_journal_result':
                    receipt['content'] += ' '
                    await manager.save(session)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert not _progress(window).get('pages')
            actual = copy.deepcopy(window)
            if ack == 'missing_result':
                actual.pop()
            elif ack == 'changed_body':
                actual[-1]['content'] += ' '
            elif ack == 'changed_call':
                actual[-2]['tool_calls'][0]['function']['arguments'] = '{}'
            if ack != 'none':
                service.mark_delivered(session, actual)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            pages = _progress(window).get('pages', [])
            assert bool(pages) == (ack == 'exact')
            if ack == 'exact':
                previous = copy.deepcopy(pages)
                service.mark_delivered(session, actual)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                assert _progress(window)['pages'] == previous
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_actual_returned_page_ranges_preserve_gaps_and_shrinkage(recovery_store, monkeypatch):
    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            expected = []
            # 小输出配额迫使实际返回长度小于请求；第二页故意留下空隙。
            monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 900)
            for offset in (0, 3000):
                receipt, page = await _page(manager, session, meta['ID'], offset, limit=6000)
                assert len(receipt['content']) <= 900
                value = page['chunks'][0]
                end = value['chunk_offset'] + len(value['RawHistory'])
                assert offset < end < offset + 6000
                assert page['next_offset'] == end
                expected.append((offset, end, end))
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                service.mark_delivered(session, window)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            pages = _progress(window).get('pages', [])
            assert {(p['chunk_offset'], p['end_offset'], p['next_offset']) for p in pages} == set(expected)
            assert not any(p['chunk_offset'] <= expected[0][1] and p['end_offset'] >= 3000 for p in pages)
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['session', 'turn', 'forget', 'context_restart', 'manager_restart'])
def test_delivered_page_progress_isolated_by_current_owner_and_turn(recovery_store, boundary):
    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            await _page(manager, session, meta['ID'], limit=300)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            service.mark_delivered(session, window)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert _progress(window).get('pages')
            if boundary == 'session':
                other = manager.create(prompt='隔离会话', parent=session)
                other.record['last_run'] = copy.deepcopy(session.record['last_run'])
                other.record['messages'].append({'role': 'user', 'content': '读取另一会话的进度'})
                await manager.save(other)
                session = other
            elif boundary == 'turn':
                session.record['last_run']['turn_id'] = 'new-turn'
            elif boundary == 'forget':
                service.forget(session.id)
            elif boundary == 'manager_restart':
                identifier, model = session.id, manager.model
                await manager.shutdown()
                manager = SessionManager(recovery_store, model=model)
                session = manager.sessions[identifier]
                service = manager.history_context
            else:
                from agent_core.core.history_context import HistoryContext
                service = manager.history_context = HistoryContext(manager)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert not _progress(window).get('pages')
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['failure', 'partial', 'timeout', 'cancel'])
def test_failed_model_delivery_of_real_history_page_never_commits_progress(recovery_store, monkeypatch, outcome):
    from agent_core.core.llm import ModelResponseError
    monkeypatch.setattr(config, 'MODEL_RECOVERY_LIMIT', 0)

    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        seen = []
        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            if len(seen) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'real-history-page', 'type': 'function', 'function': {
                        'name': 'history_read', 'arguments': json.dumps({'chunk_id': meta['ID'], 'limit': 300})}}]}
            if outcome == 'partial':
                kwargs['emit']('content', '受控部分响应')
            elif outcome == 'cancel':
                manager.cancel(session)
                raise asyncio.CancelledError
            elif outcome == 'timeout':
                raise TimeoutError('受控超时')
            raise ModelResponseError('受控未完成响应')
        manager.model = model
        try:
            manager.submit(session, '核验历史页交付失败')
            await asyncio.wait_for(session.task, 8)
            assert len(seen) >= 2
            receipt = next(m for m in session.record['messages'] if m.get('tool_call_id') == 'real-history-page')
            assert json.loads(receipt['content'])['chunks'][0]['RawHistory']
            session.cancelled.clear()
            window, _ = await manager.history_context.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert not _progress(window).get('pages')
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_progress_is_bounded_and_yields_budget_to_latest_unread_batch(recovery_store):
    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            for i in range(20):
                await _page(manager, session, meta['ID'], offset=i * 350, limit=250)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                service.mark_delivered(session, window)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            progress = _progress(window)
            pages = progress['pages']
            assert 0 < len(pages) <= 12
            assert progress['truncated'] is True
            assert not any(p['chunk_offset'] == 0 for p in pages)
            assert any(p['chunk_offset'] == 19 * 350 for p in pages)
            old_count = len(pages)
            (manager.workspace(session) / 'pressure.txt').write_text('界' * 13000, encoding='utf-8')
            latest = await _tool(manager, session, 'read_file', {'path': 'pressure.txt', 'limit': 30000}, 'protected-unread')
            window, metrics = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert latest in [m for m in window if m.get('role') == 'tool']
            assert latest['content'] == '界' * 13000
            assert len(_progress(window).get('pages', [])) < old_count
            assert metrics['sent_tokens'] + metrics['reserved_tokens'] <= 16000
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_neighbor_page_progress_tracks_each_actual_chunk_slice(recovery_store):
    async def run():
        manager, session, first, first_path, first_before = await _archive(recovery_store)
        try:
            service = manager.history_context
            start = len(session.record['messages']) - 1
            (manager.workspace(session) / 'neighbor.txt').write_text('邻块证据🙂' * 1000, encoding='utf-8')
            await _tool(manager, session, 'read_file', {'path': 'neighbor.txt', 'limit': 30000}, 'neighbor-read')
            manifest = await service._manifest(session)
            await service._compact(session, manifest, service._chunks(session, session.record['messages'], [(start, start + 3)]), local=True)
            session.record['messages'].append({'role': 'user', 'content': '读取两个邻块的实际片段'})
            session.record['last_run'] = {'turn_id': 'neighbor-recovery-turn'}
            await manager.save(session)
            second = service.cache[session.id]['chunks'][1]
            second_path = recovery_store.directory(session.id) / 'context' / 'chunks' / (second['ID'] + '.json')
            second_before = second_path.read_bytes()
            first_raw = json.loads(first_before)['RawHistory']
            receipt = await _tool(manager, session, 'history_read', {
                'chunk_id': second['ID'], 'before': 1, 'after': 0,
                'offset': len(first_raw) - 50, 'limit': 150}, 'neighbor-page')
            page = json.loads(receipt['content'])
            assert len(page['chunks']) == 2
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert not _progress(window).get('pages')
            service.mark_delivered(session, window)
            window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            pages = _progress(window)['pages']
            expected = {(c['ID'], c['chunk_offset'], c['chunk_offset'] + len(c['RawHistory'])) for c in page['chunks']}
            assert {(p['chunk_id'], p['chunk_offset'], p['end_offset']) for p in pages} == expected
            assert all(p['next_offset'] == page['next_offset'] for p in pages)
            assert all(p['request']['chunk_id'] == second['ID'] and p['request']['before'] == 1 for p in pages)
            assert first_path.read_bytes() == first_before
            assert second_path.read_bytes() == second_before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_soft_compaction_summary_expansion_evicts_advisory_progress_before_checkpoint(recovery_store, monkeypatch):
    from types import SimpleNamespace

    class ControlledSupervisor:
        """离线服务替身只控制摘要长度与软触发，不代表真实摘要质量。"""
        def __init__(self, expanded=False):
            self.expanded = expanded
            self.settings = SimpleNamespace(enabled=expanded, timeout=5, check_interval=1,
                                            variance_threshold=.1, max_input_chars=12000, max_chunks=24)
            self.inspected = 0

        async def inspect(self, snapshot):
            self.inspected += 1
            return {'compact': True, 'reason': '受控摘要膨胀',
                    'values': [float(i % 2) for i in range(len(snapshot['chunks']))]}

        async def summarize(self, previous_summary, messages, *, kind='session'):
            return '摘' * 2000 if self.expanded else '受控短摘要'

    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            for i in range(12):
                await _page(manager, session, meta['ID'], offset=i * 350, limit=100)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                service.mark_delivered(session, window)
            # 已确认旧页的实际归档先使用短摘要，随后由普通软压缩路径发布长摘要。
            service.supervisor = ControlledSupervisor()
            manifest = await service._manifest(session)
            await service._compact(session, manifest, service._chunks(
                session, session.record['messages'], [(i, i + 2) for i in range(5, 29, 2)]))
            for i in range(2):
                await _page(manager, session, meta['ID'], offset=7000 + i * 200, limit=50)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                service.mark_delivered(session, window)
            baseline, metrics = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            previous_pages = _progress(baseline)['pages']
            runtime = '界' * int(16000 * .97 - metrics['sent_tokens'] - metrics['reserved_tokens'])
            assert runtime
            supervisor = ControlledSupervisor(expanded=True)
            service.supervisor = supervisor
            monkeypatch.setattr(config, 'CONTEXT_COMPACT_MIN_NEW_GROUPS', 0)
            window, metrics = await service.prepare(session, session.record['messages'], schemas=SCHEMAS, runtime=runtime)
            assert config.CONTEXT_COMPACT_RATIO <= metrics['before_utilization'] < 1
            assert supervisor.inspected == 1 and metrics['compacted'] is True
            assert metrics['summary_kind'] == 'model' and metrics['session_summary_chars'] == 2000
            assert len(_progress(window).get('pages', [])) < len(previous_pages)
            assert metrics['sent_tokens'] + metrics['reserved_tokens'] <= 16000
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('base_utilization', [.88, .97])
def test_optional_progress_alone_cannot_trigger_compaction(recovery_store, base_utilization):
    from types import SimpleNamespace
    from agent_core.core.context import request_tokens

    class UnexpectedSupervisor:
        """若建议性进度单独触发监督器，此受控服务记录违规调用。"""
        settings = SimpleNamespace(enabled=True, timeout=5, check_interval=1,
                                   variance_threshold=.1, max_input_chars=12000, max_chunks=24)
        inspected = 0

        async def inspect(self, snapshot):
            self.inspected += 1
            return {'compact': True, 'reason': '仅测试建议性进度触发边界',
                    'values': [float(i % 2) for i in range(len(snapshot['chunks']))]}

        async def summarize(self, previous_summary, messages, *, kind='session'):
            return '摘' * 2000

    async def run():
        manager, session, meta, chunk, before = await _archive(recovery_store)
        try:
            service = manager.history_context
            for i in range(12):
                await _page(manager, session, meta['ID'], offset=i * 350, limit=100)
                window, _ = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
                service.mark_delivered(session, window)
            (manager.workspace(session) / 'small.txt').write_text('真实未读末批原文', encoding='utf-8')
            latest = await _tool(manager, session, 'read_file', {'path': 'small.txt'}, 'latest-small')
            baseline, metrics = await service.prepare(session, session.record['messages'], schemas=SCHEMAS)
            assert _progress(baseline).get('pages')
            without = copy.deepcopy(baseline)
            history = next(m for m in without if m.get('content', '').startswith('## History积累\n'))
            header, data = history['content'].rsplit('\n', 1)
            values = json.loads(data)
            values.pop('recovery_progress')
            history['content'] = header + '\n' + json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            runtime = '界' * int(base_utilization * 16000 - request_tokens(without, SCHEMAS) - metrics['reserved_tokens'])
            assert runtime
            untrimmed_utilization = (request_tokens(baseline, SCHEMAS) + len(runtime) + metrics['reserved_tokens']) / 16000
            if base_utilization < config.CONTEXT_COMPACT_RATIO:
                assert config.CONTEXT_COMPACT_RATIO <= untrimmed_utilization < 1
            else:
                assert base_utilization < 1 < untrimmed_utilization
            revision = service.cache[session.id]['revision']
            supervisor = UnexpectedSupervisor()
            if base_utilization >= config.CONTEXT_COMPACT_RATIO:
                supervisor.settings = SimpleNamespace(**{**vars(supervisor.settings), 'enabled': False})
            service.supervisor = supervisor
            window, metrics = await service.prepare(session, session.record['messages'], schemas=SCHEMAS, runtime=runtime)
            assert supervisor.inspected == 0
            assert metrics['compacted'] is False
            assert service.cache[session.id]['revision'] == revision
            assert latest in [m for m in window if m.get('role') == 'tool']
            if untrimmed_utilization > 1:
                assert len(_progress(window).get('pages', [])) < len(_progress(baseline)['pages'])
            assert metrics['sent_tokens'] + metrics['reserved_tokens'] <= 16000
            assert chunk.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())
