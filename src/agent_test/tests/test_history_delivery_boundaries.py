"""受控离线回归：真实历史/文件工具产物；不调用实际模型或重放真实会话。"""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from agent_core import config
from agent_core.core.context import ContextBudgetError
from agent_core.core.history_context import HistoryContext
from agent_core.core.llm import ModelResponseError
from agent_core.core.session_service import session_service_context
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.rag.index import RetrievalIndex
from agent_core.rag.tool import rag_search
from agent_core.tools import TOOL_REGISTRY, all_schemas
from agent_core.tools.context_history import history_read


class OfflineSupervisor:
    """只做本地摘录的服务替身，不代表真实模型摘要质量。"""

    def __init__(self):
        self.settings = SimpleNamespace(enabled=False, timeout=2, max_input_chars=12000)
        self.summaries = []

    async def summarize(self, previous_summary, messages, *, kind='session'):
        self.summaries.append(kind)
        return ('needle ' + ' '.join(m.get('content', '') for m in messages))[:900]


@pytest.fixture
def delivery_store(tmp_path, monkeypatch, sandbox_env):
    settings = {
        'ENABLE_STRUCTURED_CONTEXT': True, 'WORKSPACE_MEMORY_ENABLED': False,
        'ENABLE_TASK_PLANS': False, 'ENABLE_AGENT_TASKS': False,
        'ENABLE_COMMAND_JOBS': False, 'MEMORY_AUTO_EXTRACT': False,
        'RAG_ASSESS': False, 'CONTEXT_SUMMARY': False,
        'MODEL_INPUT_CHARS': 100000, 'MODEL_INPUT_TOKENS': 100000,
        'CONTEXT_HISTORY_CHUNK_CHARS': 1000, 'CONTEXT_HISTORY_KEEP_GROUPS': 0,
        'CONTEXT_SUMMARY_CHARS': 1000, 'CONTEXT_COMPACT_RATIO': .8,
        'CONTEXT_COMPACT_TARGET_RATIO': .75, 'TOOL_MAX_OUTPUT': 50000,
        'CONTEXT_RESERVE_USER_CHARS': 0, 'CONTEXT_RESERVE_SEARCH_CHARS': 0,
        'CONTEXT_RESERVE_USER_TOKENS': 0, 'CONTEXT_RESERVE_SEARCH_TOKENS': 0,
        'CONTEXT_SUPERVISOR_ENV_FILE': tmp_path / 'absent-supervisor.env',
        'RAG_CACHE_DIR': tmp_path / 'rag-cache', 'MODEL_RECOVERY_LIMIT': 1,
    }
    for key, value in settings.items():
        monkeypatch.setattr(config, key, value)
    # 功能在导入后显式开启；同步真实注册表，模拟正常启用时的 schema 快照。
    from agent_core.core import sessions as sessions_module
    monkeypatch.setattr(sessions_module, 'TOOL_SCHEMAS', all_schemas())
    registry = dict(TOOL_REGISTRY)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(registry)


def _schema(name: str) -> dict:
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


READERS = [_schema(n) for n in ('history_search', 'history_read', 'rag_search')]


def _batch(identifier: str, name: str, payload: str) -> list[dict]:
    return [
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': identifier, 'type': 'function',
             'function': {'name': name, 'arguments': '{"query":"needle"}'}}]},
        {'role': 'tool', 'tool_call_id': identifier, 'content': payload,
         '_provenance': 'executed'},
    ]


def _files(store: SessionStore, session) -> dict[str, bytes]:
    folder = store.directory(session.id)
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob('*')
            if p.is_file() and (p.name == 'messages.jsonl' or 'context' in p.parts)}


async def _owner(store):
    async def forbidden_model(*args, **kwargs):
        raise AssertionError('此处禁止调用模型')
    manager = SessionManager(store, model=forbidden_model)
    manager.history_context = HistoryContext(manager, supervisor=OfflineSupervisor())
    session = manager.create(prompt='保留原文与工具配对')
    return manager, session


async def _search_producer(manager, session):
    """真实发布私有分块，再将真实 core 搜索产物序列化为旧大页。"""
    service = manager.history_context
    messages = [session.record['messages'][0]]
    for i in range(12):
        messages.extend([
            {'role': 'user', 'content': f'needle source {i} ' + 'evidence ' * 130},
            {'role': 'assistant', 'content': f'needle answer {i} ' + 'reference ' * 130},
        ])
    messages.append({'role': 'user', 'content': '核验本轮未读证据'})
    session.record['messages'] = messages
    session.record['last_run'] = {'turn_id': 'controlled-turn'}
    manifest = await service._manifest(session)
    # 模拟已存在的真实归档及其收窄工作视图，原文由真实 publisher 发布。
    chunks = service._chunks(session, messages, [(i, i + 1) for i in range(1, len(messages) - 1)])
    await service._compact(session, {**manifest, 'working_reference_count': 0}, chunks)
    await manager.save(session)
    # 旧大页的真实 core 搜索产物；新工具另有1500小页上限，不能伪造大页。
    payload = json.dumps(await service.search(session, 'needle', limit=20), ensure_ascii=False)
    assert len(json.loads(payload)) == 20 and len(payload) > 15000
    assert service.cache[session.id]['working_reference_count'] == 0
    return payload


@pytest.mark.parametrize('separator', ['', 'failed', 'partial'])
def test_undelivered_search_batches_checkpoint_before_any_archive(
        delivery_store, monkeypatch, separator):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            search = await _search_producer(manager, session)
            # 小 RAG 由真实空索引路径生成；没有 embedding/reranker/线上请求。
            rag = rag_search('needle', index=RetrievalIndex([]))
            assert len(rag) < 1000 and '知识库为空' in rag
            messages = session.record['messages']
            messages.extend(_batch('search-a', 'history_search', search))
            if separator:
                messages.append({'role': 'assistant', 'content': separator + ' incomplete response'})
            messages.extend(_batch('search-b', 'history_search', search))
            messages.extend(_batch('small-rag', 'rag_search', rag))
            await manager.save(session)
            before = _files(delivery_store, session)
            original = copy.deepcopy(messages)
            calls = list(manager.history_context.supervisor.summaries)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            with pytest.raises(ContextBudgetError):
                await manager.history_context.prepare(session, messages, schemas=READERS)
            assert _files(delivery_store, session) == before
            assert manager.history_context.supervisor.summaries == calls
            assert session.record['messages'] == original
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('submitted', ['exact', 'missing', 'body', 'call_id', 'reference'])
def test_mark_delivered_requires_actual_complete_exact_batch(delivery_store, monkeypatch, submitted):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            batch = _batch('actual', 'history_search', payload)
            session.record['messages'].extend(batch)
            actual = [copy.deepcopy(session.record['messages'][0]),
                      copy.deepcopy(session.record['messages'][25]), *copy.deepcopy(batch)]
            if submitted == 'missing':
                actual.pop()
            elif submitted == 'body':
                actual[-1]['content'] += ' changed'
            elif submitted == 'call_id':
                actual[-2]['tool_calls'][0]['id'] = actual[-1]['tool_call_id'] = 'different'
            elif submitted == 'reference':
                actual = actual[:2] + [{'role': 'user', 'content': '归档引用存在', '_context_reference': True}]
            try:
                manager.history_context.mark_delivered(session, actual)
            except ValueError:
                assert submitted == 'missing'
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            if submitted == 'exact':
                history, _ = await manager.history_context.prepare(session, session.record['messages'], schemas=READERS)
                assert not any(m.get('tool_call_id') == 'actual' for m in history)
            else:
                with pytest.raises(ContextBudgetError):
                    await manager.history_context.prepare(session, session.record['messages'], schemas=READERS)
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('boundary', ['turn_id', 'new_user', 'session', 'forget'])
def test_delivery_ack_isolated_by_turn_session_and_forget(delivery_store, monkeypatch, boundary):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            batch = _batch('isolated', 'history_search', payload)
            session.record['messages'].extend(batch)
            manager.history_context.mark_delivered(session, session.record['messages'])
            if boundary == 'turn_id':
                session.record['last_run']['turn_id'] = 'different-turn'
            elif boundary == 'new_user':
                # 原位置相同但真实请求内容改变，也不能沿用确认。
                session.record['messages'][25]['content'] = '另一条真实用户请求'
            elif boundary == 'session':
                other = manager.create(prompt='另一会话')
                other.record['messages'] = copy.deepcopy(session.record['messages'])
                other.record['last_run'] = copy.deepcopy(session.record['last_run'])
                session = other
            else:
                manager.history_context.forget(session.id)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            with pytest.raises(ContextBudgetError):
                await manager.history_context.prepare(session, session.record['messages'], schemas=READERS)
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('outcome', ['success', 'failure', 'partial', 'timeout', 'cancel', 'recover'])
def test_real_session_loop_acknowledges_only_successful_model_delivery(
        delivery_store, monkeypatch, outcome):
    async def run():
        seen = []
        acks = []
        manager = None
        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            if len(seen) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'read-evidence', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"evidence.txt"}'}}]}
            # 故障发生在真实工具结果提交之后，先确认失败前从未 ACK 该结果。
            assert not any(any(m.get('tool_call_id') == 'read-evidence' for m in h) for h in acks)
            if outcome == 'recover' and len(seen) == 3:
                return {'role': 'assistant', 'content': '离线恢复成功'}
            if outcome == 'success':
                return {'role': 'assistant', 'content': '离线成功'}
            if outcome == 'cancel':
                manager.cancel(session)
                raise asyncio.CancelledError
            if outcome == 'partial':
                kwargs['emit']('content', '仅部分输出，不是成功回执')
            if outcome == 'timeout':
                raise TimeoutError('controlled model timeout')
            raise ModelResponseError('controlled incomplete model response')
        manager = SessionManager(delivery_store, model=model)
        service = manager.history_context
        service.supervisor = OfflineSupervisor()
        original = service.mark_delivered
        def mark(owner, history):
            acks.append(copy.deepcopy(history))
            original(owner, history)
        monkeypatch.setattr(service, 'mark_delivered', mark)
        try:
            session = manager.create(prompt='受控工具读取')
            session.record['tool_names'] = ['read_file', 'history_read']
            (manager.workspace(session) / 'evidence.txt').write_text('真实文件工具证据🙂', encoding='utf-8')
            manager.submit(session, '读取证据')
            await asyncio.wait_for(session.task, 5)
            assert seen, session.record.get('error')
            tool_results = [m for m in session.record['messages'] if m.get('tool_call_id') == 'read-evidence']
            assert len(tool_results) == 1 and tool_results[0]['content'] == '真实文件工具证据🙂'
            delivered = [h for h in acks if any(m.get('tool_call_id') == 'read-evidence' for m in h)]
            assert len(delivered) == (1 if outcome in ('success', 'recover') else 0)
            assert session.record['status'] == ('idle' if outcome in ('success', 'recover') else 'cancelled' if outcome == 'cancel' else 'error')
            assert len(seen) == (2 if outcome in ('success', 'cancel') else 3)
            if outcome == 'partial':
                assert any(m.get('content') == '仅部分输出，不是成功回执' for m in session.record['messages'])
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_real_history_read_small_pages_are_complete_contiguous_owned_and_immutable(
        delivery_store, monkeypatch):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            await _search_producer(manager, session)
            meta = manager.history_context.cache[session.id]['chunks'][0]
            chunk = json.loads((delivery_store.directory(session.id) / 'context' / 'chunks' / (meta['ID'] + '.json')).read_text(encoding='utf-8'))
            expected = chunk['RawHistory']
            before = _files(delivery_store, session)
            monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 1500)
            offset = 0
            parts = []
            with session_service_context(manager, session.id, asyncio.get_running_loop()):
                while True:
                    payload = await asyncio.to_thread(history_read, meta['ID'], offset=offset, limit=6000)
                    assert len(payload) <= 1500
                    page = json.loads(payload)
                    assert page['total_chars'] == len(expected)
                    assert page['chunks'][0]['chunk_offset'] == offset
                    text = ''.join(c['RawHistory'] for c in page['chunks'])
                    assert text and expected[offset:offset + len(text)] == text
                    parts.append(text)
                    if page['next_offset'] is None:
                        break
                    assert page['next_offset'] == offset + len(text)
                    offset = page['next_offset']
            assert len(parts) > 1 and ''.join(parts) == expected
            assert _files(delivery_store, session) == before
            outsider = manager.create(prompt='无权读另一会话原文')
            with pytest.raises(PermissionError):
                await manager.history_context.read(outsider, meta['ID'], output_limit=1500)
            session.cancelled.set()
            with pytest.raises(asyncio.CancelledError):
                await manager.history_context.read(session, meta['ID'], output_limit=1500)
            assert _files(delivery_store, session) == before
        finally:
            await manager.shutdown()
    asyncio.run(run())
@pytest.mark.parametrize('fits', [True, False])
def test_legacy_manifest_coverage_never_counts_as_first_raw_delivery(delivery_store, monkeypatch, fits):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            batch = _batch('legacy-covered', 'history_search', payload)
            start = len(session.record['messages'])
            session.record['messages'].extend(batch)
            service = manager.history_context
            # 真实发布旧版本可能产生的覆盖块；覆盖和引用不能代替成功模型响应。
            manifest = await service._manifest(session)
            await service._compact(session, manifest,
                                   service._chunks(session, session.record['messages'], [(start, start + 2)]))
            await manager.save(session)
            before = _files(delivery_store, session)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 100000 if fits else 9000)
            if fits:
                history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
                assert [m for m in history if m.get('tool_call_id') == 'legacy-covered'] == [batch[1]]
                # prepare 成功也不能将下一次尚未真正提交的同一结果视为已读。
                monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
                with pytest.raises(ContextBudgetError):
                    await service.prepare(session, session.record['messages'], schemas=READERS)
            else:
                with pytest.raises(ContextBudgetError):
                    await service.prepare(session, session.record['messages'], schemas=READERS)
            assert _files(delivery_store, session) == before
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_success_ack_releases_only_the_batch_present_in_actual_window(delivery_store, monkeypatch):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            first = _batch('first', 'history_search', payload)
            second = _batch('second', 'history_search', payload)
            session.record['messages'].extend(first + second)
            await manager.save(session)
            raw_before = (delivery_store.directory(session.id) / 'messages.jsonl').read_bytes()
            service = manager.history_context
            service.mark_delivered(session, [session.record['messages'][0], *first])
            before = _files(delivery_store, session)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            with pytest.raises(ContextBudgetError):
                await service.prepare(session, session.record['messages'], schemas=READERS)
            assert _files(delivery_store, session) == before
            service.mark_delivered(session, [session.record['messages'][0], *second])
            history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            assert not any(m.get('tool_call_id') in {'first', 'second'} for m in history)
            assert (delivery_store.directory(session.id) / 'messages.jsonl').read_bytes() == raw_before
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('repair', ['success', 'failure', 'partial', 'cancel'])
def test_real_handoff_repair_ack_uses_only_successful_actual_repair_window(
        delivery_store, monkeypatch, repair):
    async def run():
        histories = []
        acks = []
        manager = None
        async def model(history, **kwargs):
            histories.append(copy.deepcopy(history))
            if len(histories) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'repair-evidence', 'type': 'function', 'function': {
                        'name': 'read_file', 'arguments': '{"path":"repair.txt"}'}}]}
            if len(histories) == 2:
                return {'role': 'assistant', 'content': '受控无效交接，不是JSON'}
            assert len(histories) == 3
            if repair == 'success':
                return {'role': 'assistant', 'content': '{"state":"ok"}'}
            if repair == 'cancel':
                manager.cancel(session)
                raise asyncio.CancelledError
            if repair == 'partial':
                kwargs['emit']('content', '未完成的修复输出')
            raise ModelResponseError('controlled repair failure')
        manager = SessionManager(delivery_store, model=model)
        service = manager.history_context
        service.supervisor = OfflineSupervisor()
        original = service.mark_delivered
        def mark(owner, history):
            acks.append(copy.deepcopy(history))
            original(owner, history)
        monkeypatch.setattr(service, 'mark_delivered', mark)
        try:
            session = manager.create(prompt='受控交接修复')
            session.record['tool_names'] = ['read_file', 'history_read']
            (manager.workspace(session) / 'repair.txt').write_text('真实修复依据', encoding='utf-8')
            manager.submit(session, '读取并交接', handoff_contract={
                'fields': {'state': {'type': 'string', 'enum': ['ok']}}})
            await asyncio.wait_for(session.task, 5)
            assert len(histories) == 3
            assert acks == histories[:3 if repair == 'success' else 2]
            assert any(m.get('tool_call_id') == 'repair-evidence' and m['content'] == '真实修复依据'
                       for m in histories[2])
            assert session.record['status'] == ('idle' if repair == 'success' else 'cancelled' if repair == 'cancel' else 'error')
        finally:
            await manager.shutdown()
    asyncio.run(run())
@pytest.mark.parametrize('older_window', ['complete', 'partial', 'forged', 'unmarked'])
def test_repeated_identical_call_occurrences_ack_only_the_actual_prepared_position(
        delivery_store, monkeypatch, older_window):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            service = manager.history_context
            batch = _batch('reused-id', 'history_search', payload)
            session.record['messages'].extend(copy.deepcopy(batch))
            old_history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            service.mark_delivered(session, old_history)
            session.record['messages'].extend(copy.deepcopy(batch))
            # 此请求只含较旧已确认的 occurrence；相同 call ID/参数/正文不能证明新 occurrence 已发送。
            stale = old_history
            if older_window == 'partial':
                stale = [m for m in old_history if m.get('role') != 'tool']
            elif older_window == 'forged':
                stale = copy.deepcopy(old_history)
                for m in stale:
                    if m.get('tool_calls'):
                        m['_history_delivery'] = {'start': len(session.record['messages']) - 2,
                                                  'end': len(session.record['messages']),
                                                  'token': m.get('_history_delivery', {}).get('token', 'forged')}
            elif older_window == 'unmarked':
                stale = copy.deepcopy(old_history)
                for m in stale:
                    m.pop('_history_delivery', None)
            try:
                service.mark_delivered(session, stale)
            except ValueError:
                assert older_window == 'partial'
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            with pytest.raises(ContextBudgetError):
                await service.prepare(session, session.record['messages'], schemas=READERS)
            # old 已确认且离开原生消息窗口后，只有实际 prepare 的 new occurrence 可以解除保护。
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 100000)
            new_history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            results = [m for m in new_history if m.get('tool_call_id') == 'reused-id']
            assert len(results) == 1 and results[0]['content'] == payload
            service.mark_delivered(session, new_history)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            assert not any(m.get('tool_call_id') == 'reused-id' for m in history)
        finally:
            await manager.shutdown()
    asyncio.run(run())

def test_actual_window_marker_from_prior_turn_cannot_ack_same_raw_batch(delivery_store, monkeypatch):
    async def run():
        manager, session = await _owner(delivery_store)
        try:
            payload = await _search_producer(manager, session)
            batch = _batch('same-payload-next-turn', 'history_search', payload)
            session.record['messages'].extend(batch)
            service = manager.history_context
            old_history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            session.record['last_run']['turn_id'] = 'a-new-real-turn'
            service.mark_delivered(session, old_history)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            with pytest.raises(ContextBudgetError):
                await service.prepare(session, session.record['messages'], schemas=READERS)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 100000)
            actual_history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            service.mark_delivered(session, actual_history)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 9000)
            history, _ = await service.prepare(session, session.record['messages'], schemas=READERS)
            assert not any(m.get('tool_call_id') == 'same-payload-next-turn' for m in history)
            assert not any('_history_delivery' in m for m in session.record['messages'])
        finally:
            await manager.shutdown()
    asyncio.run(run())
