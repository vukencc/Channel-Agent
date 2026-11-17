"""真实会话日志与受控离线模型；不执行线上模型或录制回放。"""
import asyncio
import copy
import threading

import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


ANSWER = '受控纯文本最终回答'


@pytest.fixture
def terminal_store(tmp_path, monkeypatch, sandbox_env):
    for key in ('ENABLE_STRUCTURED_CONTEXT', 'WORKSPACE_MEMORY_ENABLED', 'ENABLE_TASK_PLANS',
                'ENABLE_AGENT_TASKS', 'ENABLE_COMMAND_JOBS', 'MEMORY_AUTO_EXTRACT',
                'RAG_ASSESS', 'CONTEXT_SUMMARY', 'AUTO_CONTINUE_TASKS'):
        monkeypatch.setattr(config, key, False)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    yield store
    store.close()


def _has_answer(record):
    return any(m.get('role') == 'assistant' and m.get('content') == ANSWER
               for m in record['messages'][:record.get('_count', len(record['messages']))])


def _stage(record):
    if not _has_answer(record):
        return ''
    if record['status'] == 'running' and not record.get('run_history'):
        return 'A'
    if record['status'] == 'idle' and record.get('run_history'):
        return 'B'
    return 'other'


async def _model(history, **kwargs):
    return {'role': 'assistant', 'content': ANSWER}


def test_two_durable_snapshots_start_without_event_loop_rescheduling(terminal_store, monkeypatch):
    async def run():
        manager = SessionManager(terminal_store, model=_model)
        session = manager.create(prompt='输出纯文本')
        await manager.flush()
        loop = asyncio.get_running_loop()
        original = terminal_store.save
        b_started, release_b = threading.Event(), threading.Event()
        observed, durable = {}, []
        path = terminal_store.directory(session.id) / 'session.json'

        def hold_loop_after_a():
            try:
                observed['b_started_without_loop'] = b_started.wait(1)
                observed['busy'] = session.busy
                observed['a'] = copy.deepcopy(terminal_store.read_record(path))
            finally:
                release_b.set()

        def save(record):
            stage = _stage(record)
            if stage == 'B':
                b_started.set()
                if not release_b.wait(3):
                    raise TimeoutError('受控 B 写盘门未释放')
            original(record)
            if stage in ('A', 'B'):
                durable.append((stage, copy.deepcopy(terminal_store.read_record(path))))
            if stage == 'A':
                loop.call_soon_threadsafe(hold_loop_after_a)

        monkeypatch.setattr(terminal_store, 'save', save)
        try:
            manager.submit(session, '执行受控纯文本请求')
            await asyncio.wait_for(session.task, 5)
            assert observed['b_started_without_loop'], 'B 必须在 A 成功后直接进入同一写盘任务，不能等待事件循环重新调度'
            assert observed['busy'] is True
            assert observed['a']['status'] == 'running'
            assert _has_answer(observed['a']) and not observed['a'].get('run_history')
            assert [stage for stage, _ in durable] == ['A', 'B']
            final = terminal_store.read_record(path)
            assert final['status'] == 'idle' and len(final['run_history']) == 1
            assert final['run_history'][0]['status'] == 'idle'
            assert final['run_history'][0]['turn_id'] == session.record['last_run']['turn_id']
            assert session.record['status'] == 'idle' and not session.busy
        finally:
            release_b.set()
            await manager.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize('failed_stage', ['A', 'B'])
def test_failed_snapshot_never_publishes_success_and_retries_error_state(terminal_store, monkeypatch, failed_stage):
    async def run():
        manager = SessionManager(terminal_store, model=_model)
        session = manager.create(prompt='核验写盘失败')
        await manager.flush()
        original = terminal_store.save
        attempts, durable = [], []
        failed = False
        path = terminal_store.directory(session.id) / 'session.json'

        def save(record):
            nonlocal failed
            stage = _stage(record)
            attempts.append(stage)
            if stage == failed_stage and not failed:
                failed = True
                raise OSError('受控快照写盘失败 ' + stage)
            original(record)
            if _has_answer(record):
                durable.append(copy.deepcopy(terminal_store.read_record(path)))

        monkeypatch.setattr(terminal_store, 'save', save)
        try:
            manager.submit(session, '返回纯文本')
            await asyncio.wait_for(session.task, 5)
            assert failed
            if failed_stage == 'A':
                assert 'B' not in attempts
            else:
                assert attempts.index('A') < attempts.index('B')
                assert durable[0]['status'] == 'running' and not durable[0].get('run_history')
            final = terminal_store.read_record(path)
            assert final['status'] == session.record['status'] == 'error'
            assert _has_answer(final) and len(final['run_history']) == 1
            assert final['run_history'][0]['status'] == 'error'
            assert not any(row['status'] == 'idle' for row in durable)
        finally:
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('cancel_at', ['A', 'B'])
@pytest.mark.parametrize('cancel_method', ['manager', 'task'])
def test_cancelled_transition_drains_and_persists_cancelled_once(terminal_store, monkeypatch, cancel_at, cancel_method):
    async def run():
        manager = SessionManager(terminal_store, model=_model)
        session = manager.create(prompt='核验取消写盘')
        await manager.flush()
        original = terminal_store.save
        entered, release = threading.Event(), threading.Event()
        states = []
        path = terminal_store.directory(session.id) / 'session.json'

        def save(record):
            stage = _stage(record)
            if stage == cancel_at:
                if stage == 'A':
                    original(record)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('受控取消门未释放')
                if stage == 'A':
                    states.append(record['status'])
                    return
            original(record)
            if _has_answer(record):
                states.append(record['status'])

        monkeypatch.setattr(terminal_store, 'save', save)
        try:
            manager.submit(session, '返回纯文本')
            assert await asyncio.to_thread(entered.wait, 2)
            assert session.busy
            if cancel_method == 'manager':
                manager.cancel(session)
            else:
                session.task.cancel()
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(session.task, 5)
            final = terminal_store.read_record(path)
            assert _has_answer(final)
            assert final['status'] == session.record['status'] == 'cancelled'
            assert len(final['run_history']) == 1 and final['run_history'][0]['status'] == 'cancelled'
            if cancel_at == 'A':
                assert 'idle' not in states
            else:
                assert states.index('idle') < len(states) - 1 and states[-1] == 'cancelled'
            assert not session.busy
        finally:
            release.set()
            await manager.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('update_at', ['A', 'B', 'B_unsaved'])
def test_concurrent_ceiling_save_and_persistent_inbox_survive_transition(terminal_store, monkeypatch, update_at):
    async def run():
        manager = SessionManager(terminal_store, model=_model)
        session = manager.create(prompt='纯文本根会话')
        manager.set_tool_names(session, ['read_file'])
        sender = manager.create(prompt='发信子会话', parent=session)
        session.record['inherited_tool_names'] = ['read_file']
        await manager.save(session)
        await manager.flush()
        original = terminal_store.save
        original_dependent = terminal_store.save_dependent_async
        paired = []
        def dependent(record, *args, **kwargs):
            paired.append(record['id'])
            return original_dependent(record, *args, **kwargs)
        monkeypatch.setattr(terminal_store, 'save_dependent_async', dependent)
        entered, release = threading.Event(), threading.Event()
        held = False
        path = terminal_store.directory(session.id) / 'session.json'

        def save(record):
            nonlocal held
            stage = _stage(record) if record['id'] == session.id else ''
            if stage == ('B' if update_at == 'B_unsaved' else update_at) and not held:
                held = True
                if stage == 'A':
                    original(record)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('受控并发保存门未释放')
                if stage == 'A':
                    return
            original(record)

        monkeypatch.setattr(terminal_store, 'save', save)
        try:
            manager.submit(session, '返回纯文本')
            assert await asyncio.to_thread(entered.wait, 2)
            assert session.busy
            # 使用真实归档逻辑也会保存的权限上限字段，显式提交使保存代次改变。
            session.record['inherited_tool_names'] = []
            session.record['title'] = '执行期间更新的标题'
            pending_save = manager.save(session) if update_at != 'B_unsaved' else None
            letter = manager.communication.send(sender.id, session.id, '终态转换期间的新信件')
            release.set()
            await asyncio.wait_for(session.task, 5)
            if pending_save is not None:
                await pending_save
            await manager.flush()
            assert paired == [session.id]
            final = terminal_store.read_record(path)
            assert final['status'] == 'idle' and len(final['run_history']) == 1
            assert final['inherited_tool_names'] == session.record['inherited_tool_names'] == []
            assert final['title'] == '执行期间更新的标题'
            assert manager._effective_session_tools(session) == []
            page = manager.communication.read(session.id)
            assert any(item['seq'] == letter['seq'] and item['content'] == '终态转换期间的新信件' for item in page['messages'])
            assert final.get('inbox_cursor', 0) < letter['seq']
        finally:
            release.set()
            await manager.shutdown()
    asyncio.run(run())

@pytest.mark.parametrize('ineligible', ['rag_assess', 'handoff', 'delegated'])
def test_ineligible_plain_final_paths_keep_original_two_save_order(terminal_store, monkeypatch, ineligible):
    async def run():
        import json
        expected = json.dumps({'evidence': ANSWER}, ensure_ascii=False) if ineligible == 'handoff' else ANSWER
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': expected}
        manager = SessionManager(terminal_store, model=model)
        parent = manager.create(prompt='父会话') if ineligible == 'delegated' else None
        session = manager.create(prompt='核验原保存次序', parent=parent)
        await manager.flush()
        if ineligible == 'rag_assess':
            monkeypatch.setattr(config, 'RAG_ASSESS', True)
        original = terminal_store.save
        writes = []

        def reject_pair(*args, **kwargs):
            pytest.fail('不适用路径不能进入依赖快照优化')

        def save(record):
            original(record)
            if record['id'] == session.id and any(m.get('content') == expected for m in record['messages'][:record.get('_count', len(record['messages']))]):
                writes.append((record['status'], len(record.get('run_history', []))))

        monkeypatch.setattr(terminal_store, 'save_dependent_async', reject_pair)
        monkeypatch.setattr(terminal_store, 'save', save)
        try:
            kwargs = {'handoff_contract': {'fields': {'evidence': {'type': 'string'}}}} if ineligible == 'handoff' else {}
            manager.submit(session, '返回结果', **kwargs)
            await asyncio.wait_for(session.task, 5)
            assert session.record['status'] == 'idle', session.record.get('error')
            assert writes == [('running', 0), ('idle', 1)]
        finally:
            await manager.shutdown()
    asyncio.run(run())
