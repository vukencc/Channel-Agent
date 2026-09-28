import asyncio
import copy

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_SESSION_BRANCHES', True, raising=False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def test_branch_keeps_original_history_workspace_and_memory_unchanged(store):
    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        parent.record['messages'] += [{'role': 'user', 'content': '旧问题'}, {'role': 'assistant', 'content': '旧答案'}]
        await manager.save(parent)
        store.remember(parent.id, '原记忆')
        (store.workspace(parent.id) / 'original.txt').write_text('原工作文件')
        before = copy.deepcopy(parent.record)
        child = await manager.fork_session(parent)
        assert child.id != parent.id
        assert child.record['branch']['parent_id'] == parent.id
        assert parent.record == before
        assert child.record['messages'][1:] == parent.record['messages'][1:]
        assert '工作区为空' in child.record['messages'][0]['content']
        assert not (store.workspace_path(child.id) / 'original.txt').exists()
        assert '原记忆' in store.memory(child.id, 'session')
        store.clear_memory(child.id, 'session')
        assert '原记忆' in store.memory(parent.id)
        assert (store.workspace_path(parent.id) / 'original.txt').read_text() == '原工作文件'
        await manager.shutdown()
    asyncio.run(run())


def test_branch_cannot_cut_between_call_and_result(store):
    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        parent.record['messages'] += [{'role': 'user', 'content': '读取'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read', 'function': {'name': 'list_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'read', 'content': '原结果'},
            {'role': 'assistant', 'content': '结束'}]
        await manager.save(parent)
        with pytest.raises(ValueError, match='配对'):
            await manager.fork_session(parent, through=2)
        assert len(store.list_metadata()) == 1
        child = await manager.fork_session(parent, through=3)
        assert child.record['messages'][-1]['tool_call_id'] == 'read'
        await manager.shutdown()
    asyncio.run(run())


def test_edit_resend_creates_new_session_and_preserves_original(store):
    async def model(history, **kwargs):
        return {'role': 'assistant', 'content': history[-1]['content']}
    async def run():
        manager = SessionManager(store, model=model)
        parent = manager.create()
        parent.record['messages'] += [{'role': 'user', 'content': '旧问题'}, {'role': 'assistant', 'content': '旧答案'}]
        await manager.save(parent)
        child = await manager.resend(parent, 1, '新问题')
        await child.task
        assert parent.record['messages'][-1]['content'] == '旧答案'
        assert child.record['messages'][-2:][0]['content'] == '新问题'
        assert child.record['messages'][-1]['content'] == '新问题'
        await manager.shutdown()
    asyncio.run(run())


def test_parallel_forks_persist_independently_and_cli_shows_parent(store):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from ai_agent_startup.core.cli import AgentCLI
    async def run():
        manager = SessionManager(store)
        parents = [manager.create('first'), manager.create('second')]
        await manager.flush()
        children = await asyncio.gather(*(manager.fork_session(parent) for parent in parents))
        assert len({child.id for child in children}) == 2
        restored = {record['id']: record for record in store.load_all()}
        assert [restored[child.id]['branch']['parent_id'] for child in children] == [parent.id for parent in parents]
        await manager.shutdown()
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            cli.select(children[0].id)
            assert '↳' + parents[0].id[:6] in ''.join(row[1] for row in cli.session_list())
            await cli.manager.shutdown()
    asyncio.run(run())


def test_fork_does_not_require_memory_management_or_inherit_shared_alias(store, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_MEMORY_MANAGEMENT', False)
    monkeypatch.setattr(config, 'MEMORY_SHARED', True)
    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        await manager.flush()
        store.remember(parent.id, '共享原记忆')
        child = await manager.fork_session(parent)
        store.clear_memory(child.id, 'session')
        assert '共享原记忆' in store.memory(parent.id)
        await manager.shutdown()
    asyncio.run(run())


def test_shutdown_drains_inflight_fork_before_closing_store(store, monkeypatch):
    import threading
    from ai_agent_startup.core import branches
    entered, release = threading.Event(), threading.Event()
    original = branches.fork_record
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(branches, 'fork_record', slow)
    async def run():
        manager = SessionManager(store)
        parent = manager.create()
        task = asyncio.create_task(manager.fork_session(parent))
        assert await asyncio.to_thread(entered.wait, 1)
        shutdown = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(.01)
        assert not shutdown.done()
        release.set()
        child = await task
        await shutdown
        assert child.id in manager.sessions and not manager.fork_tasks
        assert store.read_record(store.directory(child.id) / 'session.json')['branch']['parent_id'] == parent.id
    try:
        asyncio.run(run())
    finally:
        release.set()


def test_cli_branch_and_resend_commands_use_new_sessions(store):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from ai_agent_startup.core.cli import AgentCLI
    with create_pipe_input() as pipe:
        cli = AgentCLI(store, input=pipe, output=DummyOutput())
        parent = cli.active
        parent.record['messages'].extend([{'role': 'user', 'content': '原问题'}, {'role': 'assistant', 'content': '原答案'}])
        cli.manager.save(parent)
        cli.handle('/branch')
        assert cli.current != parent.id
        assert cli.active.record['branch']['parent_id'] == parent.id
        async def model(history, **kwargs):
            return {'role': 'assistant', 'content': '重发测试桩'}
        cli.manager.model = model
        cli.select(parent.id)
        cli.handle('/resend 1 新问题')
        assert cli.current != parent.id
        assert cli.active.record['messages'][-2]['content'] == '新问题'
        assert parent.record['messages'][-1]['content'] == '原答案'
