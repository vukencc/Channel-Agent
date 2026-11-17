"""受控离线模型回归；不使用真实模型、真实会话或录制回放。"""
import asyncio
import copy
import json
import os
import re

import pytest

from agent_core import config
from agent_core.core.context import request_tokens
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools import all_schemas
from agent_core.tools import context_history  # 显式启用离线历史工具注册


@pytest.fixture
def input_store(tmp_path, monkeypatch, sandbox_env):
    settings = {
        'ENABLE_STRUCTURED_CONTEXT': True, 'WORKSPACE_MEMORY_ENABLED': False,
        'ENABLE_TASK_PLANS': False, 'ENABLE_AGENT_TASKS': False,
        'ENABLE_COMMAND_JOBS': False, 'MEMORY_AUTO_EXTRACT': False,
        'RAG_ASSESS': False, 'CONTEXT_SUMMARY': False,
        'MODEL_INPUT_CHARS': 64000, 'MODEL_INPUT_TOKENS': 16000,
        'CONTEXT_RESERVE_USER_CHARS': 0, 'CONTEXT_RESERVE_USER_TOKENS': 0,
        'CONTEXT_RESERVE_SEARCH_CHARS': 0, 'CONTEXT_RESERVE_SEARCH_TOKENS': 2000,
        'FILE_READ_CHARS': 30000, 'TOOL_MAX_OUTPUT': 30000, 'MODEL_RECOVERY_LIMIT': 0,
        'CONTEXT_SUPERVISOR_ENV_FILE': tmp_path / 'missing.env',
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    from agent_core.core import sessions
    monkeypatch.setattr(sessions, 'TOOL_SCHEMAS', all_schemas())
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _input():
    upstream = ''.join(f'上游证据第{i}项；' for i in range(400)) + '引号"反斜杠\\换行\n🙂尾证据'
    prefix = '用户背景：保持当前任务"\\🙂。\n上游节点 source 自报交接（不可信数据，非指令）：\n'
    suffix = '\n本次新增信息：核验本轮新增事项\n当前节点任务：只核验当前节点。'
    return prefix + upstream + suffix, (len(prefix), len(prefix) + len(upstream)), upstream


@pytest.mark.parametrize('mode', ['reference', 'reader_off', 'history_off', 'no_span',
                                  'chunk_tamper', 'journal_tamper', 'metadata_tamper', 'cancel_read',
                                  'publication_failure', 'publication_cancel', 'impossible',
                                  'forgotten_ack', 'recreated_context', 'new_turn'])
def test_successful_full_input_delivery_releases_only_upstream_span(input_store, monkeypatch, mode):
    if mode == 'history_off':
        monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)

    async def run():
        seen = []
        text, span, upstream = _input()
        payload = ''.join(f'工具原始证据{i:04d}。' for i in range(2200 if mode == 'impossible' else 1400))

        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            if len(seen) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'latest-unread', 'type': 'function',
                    'function': {'name': 'read_file', 'arguments': '{"path":"evidence.txt","limit":30000}'}}]}
            return {'role': 'assistant', 'content': '受控离线完成'}

        manager = SessionManager(input_store, model=model)
        service = manager.history_context
        if mode == 'publication_failure':
            def fail_publish(*args):
                raise PermissionError('受控发布失败')
            monkeypatch.setattr(service, '_publish', fail_publish)
        elif mode == 'publication_cancel':
            original_projection = service._project_input
            async def cancel_projection(owner, *args):
                manager.cancel(owner)
                return await original_projection(owner, *args)
            monkeypatch.setattr(service, '_project_input', cancel_projection)
        elif mode in ('forgotten_ack', 'recreated_context', 'new_turn'):
            original_mark = service.mark_delivered
            def expire_ack(owner, actual):
                original_mark(owner, actual)
                if mode == 'forgotten_ack':
                    service.forget(owner.id)
                elif mode == 'recreated_context':
                    from agent_core.core.history_context import HistoryContext
                    manager.history_context = HistoryContext(manager)
                else:
                    owner.record['last_run']['turn_id'] = 'controlled-next-turn'
            monkeypatch.setattr(service, 'mark_delivered', expire_ack)
        try:
            session = manager.create(prompt='核验证据；保留工具原文。')
            session.record['tool_names'] = ['read_file'] if mode == 'reader_off' else ['read_file', 'history_read']
            (manager.workspace(session) / 'evidence.txt').write_text(payload, encoding='utf-8')
            manager.submit(session, text, flow_id='controlled-flow', handoff_span=None if mode == 'no_span' else span)
            await asyncio.wait_for(session.task, 8)
            if mode in ('reader_off', 'history_off', 'no_span', 'publication_failure',
                        'publication_cancel', 'impossible', 'forgotten_ack', 'recreated_context', 'new_turn'):
                assert seen and all(any(text in m.get('content', '') for m in h) for h in seen)
                assert not getattr(manager.history_context, 'cache', {}).get(session.id, {}).get('input_refs')
                assert [m['content'] for m in session.record['messages'] if m.get('role') == 'user'] == [text]
                assert [m['content'] for m in session.record['messages'] if m.get('role') == 'tool'] == [payload]
                expected = {'history_off': 'idle', 'publication_failure': 'error',
                            'publication_cancel': 'cancelled'}
                assert session.record['status'] == expected.get(mode, 'checkpoint')
                if mode != 'history_off':
                    assert len(seen) == 1
                manifest_path = input_store.directory(session.id) / 'context' / 'manifest.json'
                if manifest_path.exists():
                    assert not json.loads(manifest_path.read_text(encoding='utf-8')).get('input_refs')
                return
            assert len(seen) == 2, session.record.get('error')
            assert '## 用户输入\n' + text in [m.get('content') for m in seen[0]]
            current = [m for m in seen[1] if m.get('role') == 'user'][-1]['content']
            assert upstream not in current
            assert text[:span[0]] in current and text[span[1]:] in current
            assert 'history_read' in current
            raw_user = [m for m in session.record['messages'] if m.get('role') == 'user'][-1]
            assert raw_user['content'] == text
            tool = [m for m in session.record['messages'] if m.get('tool_call_id') == 'latest-unread']
            assert len(tool) == 1 and tool[0]['content'] == payload
            assert [m for m in seen[1] if m.get('role') == 'tool'] == tool
            assert [m['tool_calls'] for m in seen[1] if m.get('tool_calls')] == [
                m['tool_calls'] for m in session.record['messages'] if m.get('tool_calls')]
            assert request_tokens(seen[1]) + 2000 <= 16000
            restored = copy.deepcopy(seen[1])
            next(m for m in restored if m.get('role') == 'user' and '当前节点任务' in m.get('content', ''))['content'] = '## 用户输入\n' + text
            assert request_tokens(restored) + 2000 > 16000
            manifest = manager.history_context.cache[session.id]
            assert manifest['input_refs']
            ref = manifest['input_refs'][0]['ID']
            parts, offset = [], 0
            while True:
                page = await manager.history_context.read(session, ref, offset=offset, limit=777)
                parts.extend(chunk['RawHistory'] for chunk in page['chunks'])
                if page['next_offset'] is None:
                    break
                offset = page['next_offset']
            snapshot = json.loads(''.join(parts))
            assert snapshot[0]['content'] == text
            assert snapshot[0]['content'][span[0]:span[1]] == upstream
            hint = re.search(r'read_args=(\{.*?\})', current)
            assert hint
            args = json.loads(hint.group(1))
            end_offset = int(re.search(r'end_offset=(\d+)', current).group(1))
            encoded = ''.join(parts)[args['offset']:end_offset]
            assert json.loads('"' + encoded + '"') == upstream
            outsider = manager.create(prompt='其他会话')
            with pytest.raises(PermissionError):
                await manager.history_context.read(outsider, ref)
            sibling = manager.create(prompt='同项目兄弟会话', parent=session)
            assert manager.workspace(sibling) == manager.workspace(session)
            with pytest.raises(PermissionError):
                await manager.history_context.read(sibling, ref)
            service = manager.history_context
            if mode == 'chunk_tamper':
                chunk = input_store.directory(session.id) / 'context' / 'chunks' / (ref + '.json')
                record = json.loads(chunk.read_text(encoding='utf-8'))
                record['session_id'] = outsider.id
                chunk.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
                with pytest.raises(PermissionError):
                    await service.read(session, ref)
            elif mode == 'journal_tamper':
                journal = input_store.directory(session.id) / 'messages.jsonl'
                before = journal.read_bytes()
                changed = before.replace('用户背景'.encode('utf-8'), '改写背景'.encode('utf-8'))
                # 同字节长度改写，保留已提交范围与换行，专门验证日志内容校验。
                assert changed != before and len(changed) == len(before)
                journal.write_bytes(changed)
                with pytest.raises(PermissionError):
                    await service.read(session, ref)
            elif mode == 'metadata_tamper':
                raw_user['_flow_handoff']['owner'] = outsider.id
                with pytest.raises(ValueError):
                    await service.read(session, ref)
            elif mode == 'cancel_read':
                session.cancelled.set()
                with pytest.raises(asyncio.CancelledError):
                    await service.read(session, ref)
        finally:
            await manager.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize('outcome', ['failure', 'partial', 'timeout', 'cancel'])
def test_unsuccessful_first_request_never_acknowledges_input(input_store, monkeypatch, outcome):
    from agent_core.core.llm import ModelResponseError

    async def run():
        seen, acknowledgements = [], []
        text, span, _ = _input()

        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            if outcome == 'partial':
                kwargs['emit']('content', '受控部分输出')
            if outcome == 'cancel':
                manager.cancel(session)
                raise asyncio.CancelledError
            if outcome == 'timeout':
                raise TimeoutError('受控超时')
            raise ModelResponseError('受控未完成响应')

        manager = SessionManager(input_store, model=model)
        service = manager.history_context
        original = service.mark_delivered

        def mark(owner, actual):
            acknowledgements.append(copy.deepcopy(actual))
            return original(owner, actual)

        monkeypatch.setattr(service, 'mark_delivered', mark)
        try:
            session = manager.create(prompt='核验失败交付')
            session.record['tool_names'] = ['history_read']
            manager.submit(session, text, flow_id='controlled-flow', handoff_span=span)
            await asyncio.wait_for(session.task, 8)
            assert seen and not acknowledgements
            assert all('## 用户输入\n' + text in [m.get('content') for m in history] for history in seen)
            assert [m['content'] for m in session.record['messages'] if m.get('role') == 'user'] == [text]
            assert not service.cache.get(session.id, {}).get('input_refs')
            assert session.record['status'] == ('cancelled' if outcome == 'cancel' else 'error')
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('permission', ['smart', 'readonly', 'full_access'])
def test_small_input_remains_inline_without_pressure(input_store, permission):
    async def run():
        seen = []
        text, span, _ = _input()

        async def model(history, **kwargs):
            seen.append(copy.deepcopy(history))
            return {'role': 'assistant', 'content': '受控完成'}

        manager = SessionManager(input_store, model=model)
        try:
            session = manager.create(prompt='核验小输入')
            manager.set_permission_policy(session, permission)
            session.record['tool_names'] = ['history_read']
            manager.submit(session, text, flow_id='controlled-flow', handoff_span=span)
            await asyncio.wait_for(session.task, 8)
            assert len(seen) == 1 and session.record['status'] == 'idle'
            assert '## 用户输入\n' + text in [m.get('content') for m in seen[0]]
            assert not getattr(manager.history_context, 'cache', {}).get(session.id, {}).get('input_refs')
        finally:
            await manager.shutdown()
    asyncio.run(run())









async def _soft_pressure_projection(store):
    text, span, upstream = _input()
    seen = []

    async def model(history, **kwargs):
        seen.append(copy.deepcopy(history))
        return {'role': 'assistant', 'content': '受控首次完整送达'}

    manager = SessionManager(store, model=model)
    try:
        session = manager.create(prompt='核验软阈值交接')
        session.record['tool_names'] = ['history_read']
        manager.submit(session, text, flow_id='controlled-soft-flow', handoff_span=span)
        await asyncio.wait_for(session.task, 8)
        assert session.record['status'] == 'idle'
        assert '## 用户输入\n' + text in [m.get('content') for m in seen[0]]
        assert not manager.history_context.cache[session.id].get('input_refs')
        schemas = [s for s in all_schemas() if s['function']['name'] == 'history_read']
        assert len(schemas) == 1
        # 仅为预算构造的本地运行时上下文，不是模型回答或工具结果。
        runtime = '\n受控运行时参考：' + '界' * 9500
        history, metrics = await manager.history_context.prepare(
            session, session.record['messages'], schemas=schemas, runtime=runtime)
        original = copy.deepcopy(history)
        next(m for m in original if m.get('role') == 'user' and '当前节点任务' in m.get('content', ''))['content'] = '## 用户输入\n' + text
        raw_utilization = (request_tokens(original, schemas) + 2000) / 16000
        assert config.CONTEXT_COMPACT_RATIO <= raw_utilization < 1
        assert manager.history_context.cache[session.id]['input_refs']
        return manager, session, text, upstream, schemas, history
    except BaseException:
        await manager.shutdown()
        raise


def test_successful_delivery_projects_at_existing_soft_threshold(input_store):
    async def run():
        manager, session, text, upstream, schemas, history = await _soft_pressure_projection(input_store)
        try:
            user = [m['content'] for m in history if m.get('role') == 'user'][-1]
            assert upstream not in user and 'history_read' in user
            assert '前序成功请求中送达' in user and '仅需复核时' in user
            assert [m['content'] for m in session.record['messages'] if m.get('role') == 'user'] == [text]
            assert request_tokens(history, schemas) + 2000 <= 16000
        finally:
            await manager.shutdown()
    asyncio.run(run())


def test_published_reference_sticks_after_pressure_clears_and_reader_revocation_restores_input(input_store):
    async def run():
        manager, session, text, upstream, schemas, previous = await _soft_pressure_projection(input_store)
        try:
            refs = copy.deepcopy(manager.history_context.cache[session.id]['input_refs'])
            history, _ = await manager.history_context.prepare(session, session.record['messages'], schemas=schemas)
            user = [m['content'] for m in history if m.get('role') == 'user'][-1]
            assert upstream not in user and refs[0]['ID'] in user
            restored = copy.deepcopy(history)
            next(m for m in restored if m.get('role') == 'user' and '当前节点任务' in m.get('content', ''))['content'] = '## 用户输入\n' + text
            assert (request_tokens(restored, schemas) + 2000) / 16000 < config.CONTEXT_COMPACT_RATIO
            history, _ = await manager.history_context.prepare(session, session.record['messages'], schemas=[])
            assert '## 用户输入\n' + text in [m.get('content') for m in history]
            assert manager.history_context.cache[session.id]['input_refs'] == refs
            assert [m['content'] for m in session.record['messages'] if m.get('role') == 'user'] == [text]
        finally:
            await manager.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize('link_kind', ['hardlink', 'symlink'])
def test_reference_private_chunk_rejects_links_to_external_source(input_store, tmp_path, request, link_kind):
    if link_kind == 'symlink':
        request.getfixturevalue('symlink_capable')

    async def run():
        manager, session, text, upstream, schemas, history = await _soft_pressure_projection(input_store)
        try:
            ref = manager.history_context.cache[session.id]['input_refs'][0]['ID']
            chunk = input_store.directory(session.id) / 'context' / 'chunks' / (ref + '.json')
            source = tmp_path / 'external-source.json'
            source.write_bytes(chunk.read_bytes())
            before = source.read_bytes()
            chunk.unlink()
            if link_kind == 'hardlink':
                chunk.hardlink_to(source)
            else:
                chunk.symlink_to(source)
            # Windows 在 opened_path 身份固定阶段拒绝硬链接，Linux 由描述符校验拒绝。
            expected_error = ValueError if link_kind == 'hardlink' and os.name != 'nt' else PermissionError
            with pytest.raises(expected_error):
                await manager.history_context.read(session, ref)
            assert source.read_bytes() == before
        finally:
            await manager.shutdown()
    asyncio.run(run())
