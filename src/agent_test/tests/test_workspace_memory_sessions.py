"""真实会话/Flow 服务与离线受控模型回调；不是 provider 实测或录制回放。"""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import threading

import pytest

from agent_core import config
from agent_core.core.flows import FlowService
from agent_core.core.projects import ProjectService
from agent_core.core.llm import ModelConnectionError
from agent_core.core.model_settings import active_endpoint
from agent_core.core.budgets import active_budget
from agent_core.core.runtime_models import RuntimeModelSnapshot
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.core.workspace_memory import WorkspaceMemoryCommitUncertain, WorkspaceMemoryStore


FACT = '项目使用橙色工单编号'
LESSON = '发布前核对工单编号'


@pytest.fixture(autouse=True)
def shared_memory_config(monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'WORKSPACE_MEMORY_ENABLED', True, raising=False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'full_access')


@asynccontextmanager
async def runtime(root, model, monkeypatch, *, summary=None):
    store = SessionStore(root / 'state', workspace_root=root / 'private')
    manager = SessionManager(store, model=model)
    prompts = []
    async def extract(prompt, *, session_id):
        prompts.append((prompt, session_id))
        if summary is not None:
            return await summary(prompt, session_id=session_id)
        return json.dumps({'project': [FACT], 'progress': '本轮自报完成，尚未业务验收',
                           'lessons': [LESSON]}, ensure_ascii=False)
    monkeypatch.setattr(manager, '_complete', extract)
    try:
        yield manager, prompts
    finally:
        await manager.shutdown()
        store.close()


def memory_path(project):
    return project / '.channelagent' / 'memory.json'


async def submit(manager, session, text='当前工作请求'):
    manager.submit(session, text)
    await asyncio.wait_for(asyncio.shield(session.task), 5)


async def answer(history, **kwargs):
    return {'role': 'assistant', 'content': '本轮可见回复', 'reasoning_content': '内部思考不得共享'}


def test_irrelevant_conversation_does_not_create_shared_record(tmp_path, monkeypatch):
    async def summarize(*args, **kwargs):
        return 'null'
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch, summary=summarize) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await submit(manager, session, '这是与项目无关的私人闲聊')
            assert session.record['status'] == 'idle'
            assert session.record['last_run']['workspace_memory']['status'] == 'skipped_irrelevant'
            assert len(prompts) == 1
            assert not list(project.iterdir())
    asyncio.run(run())


def test_generated_summary_cannot_persist_known_config_secret(tmp_path, monkeypatch):
    secret = 'fixture-output-secret-8946'
    monkeypatch.setattr(config, 'API_KEY', secret)
    async def summarize(*args, **kwargs):
        return json.dumps({'project': [secret], 'progress': '任务已结束', 'lessons': [secret]})
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch, summary=summarize) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            text = memory_path(project).read_text(encoding='utf-8')
            assert secret not in text
            assert '已隐藏配置密钥' in text
    asyncio.run(run())


def test_summary_retains_selected_endpoint_and_budget_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', 10)
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        scopes = []
        async def model(history, **kwargs):
            scopes.append((active_endpoint.get(), active_budget.get()))
            return await answer(history)
        async def summarize(prompt, **kwargs):
            scopes.append((active_endpoint.get(), active_budget.get()))
            return json.dumps({'project': [FACT], 'progress': '自报完成', 'lessons': []})
        async with runtime(tmp_path, model, monkeypatch, summary=summarize) as (manager, prompts):
            endpoint = RuntimeModelSnapshot(id='offline-selected', name='离线模型',
                model='offline-selected-model', base_url='https://offline.invalid/v1',
                api_key='offline-controlled-key')
            monkeypatch.setattr(manager.model_registry, 'resolve', lambda profile: endpoint)
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            assert session.record['status'] == 'idle', session.record.get('error')
            assert len(scopes) == 2
            assert scopes[0] == scopes[1]
            assert scopes[1][0] == endpoint
            assert scopes[1][1] == (manager.budget_ledger, session.id)
    asyncio.run(run())


def test_hanging_auxiliary_is_bounded_and_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'AUX_TIMEOUT', .03)
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def summarize(prompt, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async with runtime(tmp_path, answer, monkeypatch, summary=summarize) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            manager.submit(session, '当前工作请求')
            await asyncio.wait_for(asyncio.shield(session.task), 1)
            assert started.is_set() and cancelled.is_set()
            assert session.record['status'] == 'idle'
            data = json.loads(memory_path(project).read_text(encoding='utf-8'))
            assert not data['lessons'] and not data['project']
            assert '失败' in data['progress'][0]['summary']
    asyncio.run(run())


@pytest.mark.parametrize('structured', [False, True])
def test_shared_reference_counts_toward_existing_context_window(tmp_path, monkeypatch, structured):
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', structured)
    monkeypatch.setattr(config, 'CONTEXT_SUMMARY_BACKGROUND', False)
    async def summarize(prompt, **kwargs):
        return json.dumps({'project': [str(i) + '项目事实' * 100 for i in range(8)],
                           'progress': '本轮自报完成', 'lessons': []})
    async def run():
        project, empty = tmp_path / 'project', tmp_path / 'empty'
        project.mkdir()
        empty.mkdir()
        calls = []
        async def model(history, **kwargs):
            calls.append(history)
            return await answer(history)
        async with runtime(tmp_path, model, monkeypatch, summary=summarize) as (manager, prompts):
            seed = manager.create(prompt='预算测试', workspace_path=str(project))
            await submit(manager, seed)
            assert memory_path(project).exists()
            monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'readonly')
            baseline = manager.create(prompt='预算测试', workspace_path=str(empty))
            await submit(manager, baseline, '同一请求')
            assert baseline.record['status'] == 'idle', baseline.record.get('error')
            metrics = baseline.record['last_run']['context']
            size = metrics.get('total_chars', metrics['sent_chars']) + metrics.get('reserved_chars', 0)
            monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', size + 100)
            # 同一窗口可容纳无共享参考的请求；共享资料必须挤占既有预算。
            control = manager.create(prompt='预算测试', workspace_path=str(empty))
            await submit(manager, control, '同一请求')
            assert control.record['status'] == 'idle', control.record.get('error')
            before = len(calls)
            reader = manager.create(prompt='预算测试', workspace_path=str(project))
            await submit(manager, reader, '同一请求')
            assert len(calls) == before
            assert reader.record['status'] == 'checkpoint'
            assert reader.record['last_run']['stop_reason'] == 'context_budget'
            assert len(prompts) == 1
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['summary', 'confirmation', 'completed'])
def test_cancel_during_metadata_phase_or_after_completed_turn(tmp_path, monkeypatch, phase):
    if phase == 'confirmation':
        monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'standard')
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        started, stopped = asyncio.Event(), asyncio.Event()
        async def summarize(prompt, **kwargs):
            started.set()
            if phase == 'summary':
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()
            return json.dumps({'project': [FACT], 'progress': '自报完成', 'lessons': []})
        async with runtime(tmp_path, answer, monkeypatch, summary=summarize) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            manager.submit(session, '当前工作请求')
            if phase == 'completed':
                await asyncio.wait_for(asyncio.shield(session.task), 5)
                before = memory_path(project).read_bytes()
                manager.cancel(session)
                assert session.record['status'] == 'idle'
                assert memory_path(project).read_bytes() == before
                assert session.record['last_run']['workspace_memory']['status'] == 'saved'
                return
            if phase == 'summary':
                await asyncio.wait_for(started.wait(), 2)
            else:
                async def confirming():
                    while not session.confirmation:
                        await asyncio.sleep(.005)
                await asyncio.wait_for(confirming(), 2)
            assert not session.task.done()
            manager.cancel(session)
            await asyncio.wait_for(asyncio.shield(session.task), 5)
            assert session.record['status'] == 'cancelled'
            assert session.record['last_run']['status'] == 'cancelled'
            assert session.confirmation is None and session.decision is None
            persisted = json.loads((manager.store.directory(session.id) / 'session.json').read_text(encoding='utf-8'))
            assert persisted['status'] == 'cancelled'
            assert not list(project.iterdir())
            assert any(m.get('content') == '本轮可见回复' for m in session.record['messages'])
            if phase == 'summary':
                assert stopped.is_set()
            else:
                assert not prompts
    asyncio.run(run())


@pytest.mark.parametrize('policy', ['full_access', 'smart'])
def test_new_session_and_independent_state_inherit_actual_workspace(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        seen = []
        async def model(history, **kwargs):
            seen.append(json.dumps(history, ensure_ascii=False))
            return await answer(history)
        async with runtime(tmp_path / 'one', model, monkeypatch) as (manager, prompts):
            first = manager.create(workspace_path=str(project))
            await submit(manager, first)
            payload = json.loads(memory_path(project).read_text(encoding='utf-8'))
            assert payload['version'] == payload['revision'] == 1
            assert FACT in payload['project']
            assert first.record['last_run']['workspace_memory']['status'] == 'saved'
            second = manager.create(workspace_path=str(project))
            await submit(manager, second, '继续工作')
            assert FACT in seen[-1]
            assert '不可信' in seen[-1]
            other = tmp_path / 'other'
            other.mkdir()
            isolated = manager.create(workspace_path=str(other))
            await submit(manager, isolated)
            assert FACT not in seen[-1]
        async with runtime(tmp_path / 'two', model, monkeypatch) as (manager, prompts):
            restored = manager.create(workspace_path=str(project))
            await submit(manager, restored)
            assert FACT in seen[-1]
            assert not manager.store.memory(restored.id).strip()
    asyncio.run(run())


@pytest.mark.parametrize('policy', ['standard', 'trusted'])
def test_legacy_permission_denial_preserves_workspace(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            manager.submit(session, '当前工作请求')
            async def wait_confirmation():
                while not session.confirmation:
                    if session.task.done():
                        pytest.fail('历史权限策略未请求共享记忆确认')
                    await asyncio.sleep(.005)
            await asyncio.wait_for(wait_confirmation(), 2)
            manager.decide(session, False)
            await asyncio.wait_for(asyncio.shield(session.task), 5)
            assert session.record['status'] == 'idle'
            assert session.record['last_run']['workspace_memory']['status'] in {'skipped', 'denied'}
            assert not list(project.iterdir())
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['error', 'checkpoint'])
def test_non_success_records_status_without_inventing_summary(tmp_path, monkeypatch, failure):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async def model(history, **kwargs):
            if failure == 'checkpoint':
                raise ModelConnectionError('controlled connection failure')
            raise RuntimeError('controlled execution failure')
        async with runtime(tmp_path, model, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            assert session.record['status'] == failure
            assert not prompts
            data = json.loads(memory_path(project).read_text(encoding='utf-8'))
            assert not data['project'] and not data['lessons']
            assert data['progress'][0]['outcome'] == failure
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['model', 'queued_save'])
def test_cancelled_or_uncommitted_turn_never_writes_shared_memory(tmp_path, monkeypatch, phase):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        started = asyncio.Event()
        async def model(history, **kwargs):
            started.set()
            await asyncio.Event().wait()
        async with runtime(tmp_path, model, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await manager.flush()
            if phase == 'queued_save':
                original = manager.store.atomic_write
                def fail_queued(path, data):
                    if Path(path).name == 'session.json' and json.loads(data).get('status') == 'queued':
                        raise OSError('controlled queued persistence failure')
                    return original(path, data)
                monkeypatch.setattr(manager.store, 'atomic_write', fail_queued)
            manager.submit(session, '本轮请求')
            if phase == 'model':
                await asyncio.wait_for(started.wait(), 2)
                manager.cancel(session)
            await asyncio.wait_for(asyncio.shield(session.task), 5)
            assert session.record['status'] == ('cancelled' if phase == 'model' else 'error')
            assert not prompts
            assert not list(project.iterdir())
            if phase == 'queued_save':
                assert not started.is_set()
    asyncio.run(run())


def test_summary_input_excludes_private_history_reasoning_and_config_secret(tmp_path, monkeypatch):
    secret = 'fixture-config-secret-9182'
    monkeypatch.setattr(config, 'API_KEY', secret)
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            manager.store.remember(session.id, '私有记忆不得共享')
            session.record['messages'].extend([
                {'role': 'user', 'content': '旧用户历史不得共享'},
                {'role': 'assistant', 'content': '旧助手历史不得共享'},
            ])
            await submit(manager, session, f'当前工作请求 {secret}')
            assert len(prompts) == 1
            prompt, session_id = prompts[0]
            assert session_id == session.id
            assert '当前工作请求' in prompt and '本轮可见回复' in prompt
            for value in (secret, '私有记忆不得共享', '旧用户历史不得共享',
                          '旧助手历史不得共享', '内部思考不得共享'):
                assert value not in prompt
                assert value not in memory_path(project).read_text(encoding='utf-8')
            assert not list(project.rglob('messages.jsonl'))
            assert not list(project.rglob('memory.md'))
            assert not list(project.rglob('inbox*'))
            assert not list(project.rglob('audit.jsonl'))
    asyncio.run(run())


@pytest.mark.parametrize('existing', [False, True])
def test_readonly_reads_existing_without_metadata_or_aux_side_effects(tmp_path, monkeypatch, existing):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        seen = []
        async def model(history, **kwargs):
            seen.append(json.dumps(history, ensure_ascii=False))
            return await answer(history)
        async with runtime(tmp_path, model, monkeypatch) as (manager, prompts):
            if existing:
                await submit(manager, manager.create(workspace_path=str(project)))
            before = {str(p.relative_to(project)): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', 'readonly')
            prompts.clear()
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            assert session.record['status'] == 'idle'
            assert session.record['last_run']['workspace_memory']['status'] == 'skipped_readonly'
            assert not prompts
            assert (FACT in seen[-1]) == existing
            assert {str(p.relative_to(project)): p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
            assert not list(project.rglob('.write-lock'))
            if not existing:
                assert not list(project.iterdir())
    asyncio.run(run())


@pytest.mark.parametrize('unsafe', ['corrupt', 'symlink'])
def test_invalid_memory_blocks_model_without_overwriting(tmp_path, monkeypatch, symlink_capable, unsafe):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        target = memory_path(project)
        target.parent.mkdir()
        if unsafe == 'corrupt':
            target.write_text('{broken', encoding='utf-8')
        else:
            external = tmp_path / 'outside'
            external.write_text('external-preserved', encoding='utf-8')
            target.symlink_to(external)
        original = target.read_bytes()
        calls = []
        async def model(history, **kwargs):
            calls.append(1)
            return await answer(history)
        async with runtime(tmp_path, model, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            assert not calls and not prompts
            assert session.record['status'] in {'checkpoint', 'error'}
            assert target.read_bytes() == original
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['invalid_json', 'timeout'])
def test_summary_failure_records_only_explicit_fallback_progress(tmp_path, monkeypatch, failure):
    async def summarize(prompt, **kwargs):
        if failure == 'timeout':
            raise TimeoutError('controlled auxiliary timeout')
        return 'summary is not JSON'
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch, summary=summarize) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await submit(manager, session)
            data = json.loads(memory_path(project).read_text(encoding='utf-8'))
            assert not data['project'] and not data['lessons']
            assert len(data['progress']) == 1
            assert '摘要' in data['progress'][0]['summary']
            assert '失败' in data['progress'][0]['summary']
            assert session.record['status'] == 'idle'
            assert len(prompts) == 1
    asyncio.run(run())


def test_terminal_session_save_failure_never_publishes_memory(tmp_path, monkeypatch):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await manager.flush()
            original = manager.store.atomic_write
            def fail_terminal(path, data):
                if Path(path).name == 'session.json' and json.loads(data).get('status') == 'idle':
                    raise OSError('controlled terminal save failure')
                return original(path, data)
            monkeypatch.setattr(manager.store, 'atomic_write', fail_terminal)
            await submit(manager, session)
            assert session.record['status'] == 'error'
            assert not memory_path(project).exists()
            assert not prompts
    asyncio.run(run())


def test_cancel_during_first_terminal_save_never_publishes_memory(tmp_path, monkeypatch):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        entered, release = threading.Event(), threading.Event()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await manager.flush()
            original = manager.store.atomic_write
            blocked = False
            def block_terminal(path, data):
                nonlocal blocked
                if not blocked and Path(path).name == 'session.json' and json.loads(data).get('status') == 'idle':
                    blocked = True
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('测试终态保存屏障未释放')
                return original(path, data)
            monkeypatch.setattr(manager.store, 'atomic_write', block_terminal)
            try:
                manager.submit(session, '当前工作请求')
                assert await asyncio.to_thread(entered.wait, 2)
                assert not session.task.done()
                manager.cancel(session)
                release.set()
                await asyncio.wait_for(asyncio.shield(session.task), 5)
                assert session.record['status'] == 'cancelled'
                assert session.record['last_run']['status'] == 'cancelled'
                persisted = json.loads((manager.store.directory(session.id) / 'session.json').read_text(encoding='utf-8'))
                assert persisted['status'] == 'cancelled'
                assert not prompts
                assert not list(project.iterdir())
            finally:
                release.set()
    asyncio.run(run())


def test_cancel_during_second_terminal_save_retains_committed_memory_without_replay(tmp_path, monkeypatch):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        entered, release = threading.Event(), threading.Event()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await manager.flush()
            original = manager.store.atomic_write
            idle_saves = 0
            def block_second_terminal(path, data):
                nonlocal idle_saves
                if Path(path).name == 'session.json' and json.loads(data).get('status') == 'idle':
                    idle_saves += 1
                    if idle_saves == 2:
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('测试第二次终态保存屏障未释放')
                return original(path, data)
            monkeypatch.setattr(manager.store, 'atomic_write', block_second_terminal)
            try:
                manager.submit(session, '当前工作请求')
                assert await asyncio.to_thread(entered.wait, 2)
                assert not session.task.done()
                assert len(prompts) == 1
                committed = memory_path(project).read_bytes()
                assert FACT in json.loads(committed)['project']
                manager.cancel(session)
                release.set()
                await asyncio.wait_for(asyncio.shield(session.task), 5)
                assert session.record['status'] == 'cancelled'
                assert session.record['last_run']['status'] == 'cancelled'
                persisted = json.loads((manager.store.directory(session.id) / 'session.json').read_text(encoding='utf-8'))
                assert persisted['status'] == 'cancelled'
                assert persisted['last_run']['status'] == 'cancelled'
                assert memory_path(project).read_bytes() == committed
                assert len(prompts) == 1
            finally:
                release.set()
    asyncio.run(run())


def test_cancel_during_error_save_retry_preserves_persistence_failure(tmp_path, monkeypatch):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        entered, release = threading.Event(), threading.Event()
        async with runtime(tmp_path, answer, monkeypatch) as (manager, prompts):
            session = manager.create(workspace_path=str(project))
            await manager.flush()
            original = manager.store.atomic_write
            failed = False
            def fail_idle_then_block_error(path, data):
                nonlocal failed
                if Path(path).name == 'session.json':
                    status = json.loads(data).get('status')
                    if status == 'idle' and not failed:
                        failed = True
                        raise OSError('controlled terminal persistence failure')
                    if status == 'error' and failed:
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError('测试错误终态保存屏障未释放')
                return original(path, data)
            monkeypatch.setattr(manager.store, 'atomic_write', fail_idle_then_block_error)
            try:
                manager.submit(session, '当前工作请求')
                assert await asyncio.to_thread(entered.wait, 2)
                assert not session.task.done()
                assert session.record['status'] == 'error'
                manager.cancel(session)
                release.set()
                await asyncio.wait_for(asyncio.shield(session.task), 5)
                assert session.record['status'] == 'error'
                assert session.record['last_run']['status'] == 'error'
                assert session.record['last_run']['stop_reason'] == 'persistence_failed'
                persisted = json.loads((manager.store.directory(session.id) / 'session.json').read_text(encoding='utf-8'))
                assert persisted['status'] == 'error'
                assert persisted['last_run']['status'] == 'error'
                assert persisted['last_run']['stop_reason'] == 'persistence_failed'
                assert not prompts
                assert not list(project.iterdir())
            finally:
                release.set()
    asyncio.run(run())


@pytest.mark.parametrize('fail_write', [False, True, 'uncertain'])
def test_flow_downstream_waits_for_memory_commit_and_stops_on_failure(tmp_path, monkeypatch, fail_write):
    async def run():
        project = tmp_path / 'project'
        project.mkdir()
        calls = []
        summarizing, release_summary = asyncio.Event(), asyncio.Event()
        async def model(history, **kwargs):
            calls.append(json.dumps(history, ensure_ascii=False))
            return await answer(history)
        async def summarize(prompt, **kwargs):
            summarizing.set()
            await release_summary.wait()
            return json.dumps({'project': [FACT], 'progress': '本轮自报完成，尚未业务验收',
                               'lessons': [LESSON]}, ensure_ascii=False)
        if fail_write:
            original_append = WorkspaceMemoryStore.append
            def fail(*args, **kwargs):
                if fail_write == 'uncertain':
                    original_append(*args, **kwargs)
                    raise WorkspaceMemoryCommitUncertain('controlled completion audit failure')
                raise OSError('controlled shared snapshot failure')
            monkeypatch.setattr(WorkspaceMemoryStore, 'append', fail)
        async with runtime(tmp_path, model, monkeypatch, summary=summarize) as (manager, prompts):
            service = FlowService(manager)
            projects = ProjectService(manager, service)
            identifier = projects.create('项目', str(project))['flow_id']
            flow = service.get(identifier)
            flow['nodes'] = [{'id': 'a', 'title': '分析', 'prompt': '分析'},
                             {'id': 'b', 'title': '执行', 'prompt': '执行'}]
            flow['edges'] = [{'source': 'a', 'target': 'b'}]
            service.save(flow)
            try:
                service.start(identifier, input_text='本轮输入')
                await asyncio.wait_for(summarizing.wait(), 2)
                assert len(calls) == 1
                assert not service.tasks[identifier].done()
                release_summary.set()
                await asyncio.wait_for(asyncio.shield(service.tasks[identifier]), 5)
                result = service.get(identifier)['run']
                if fail_write:
                    assert result['status'] != 'completed'
                    assert len(calls) == 1
                    session = manager.sessions[result['nodes']['a']['session_id']]
                    assert session.record['status'] in {'checkpoint', 'error'}
                    assert session.record['last_run']['workspace_memory']['status'] in {'failed', 'uncertain'}
                    if fail_write == 'uncertain':
                        assert session.record['last_run']['workspace_memory']['status'] == 'uncertain'
                    assert any(m.get('content') == '本轮可见回复' for m in session.record['messages'])
                else:
                    assert result['status'] == 'completed'
                    assert len(calls) == 2
                    assert FACT in calls[1]
                    assert json.loads(memory_path(project).read_text(encoding='utf-8'))['revision'] == 2
            finally:
                release_summary.set()
                await service.shutdown()
    asyncio.run(run())
