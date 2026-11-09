"""经典会话必须先提交用户输入，再启动模型或工具。"""

import asyncio
import errno
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


WAIT_SECONDS = 5
TURN_INPUT = '第一轮：请保留这条输入'
IndexGates = Callable[
    [Path, list[str], tuple[str, ...]], dict[str, tuple[asyncio.Event, threading.Event]],
]


@pytest.fixture
def store(tmp_path: Path, sandbox_env: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionStore]:
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'MODEL_STABLE_PREFIX', False)
    session_store = SessionStore(tmp_path / 'state', workspace_root=sandbox_env)
    try:
        yield session_store
    finally:
        session_store.close()


@pytest.fixture
def hold_index(store: SessionStore, monkeypatch: pytest.MonkeyPatch) -> IndexGates:
    def install(index: Path, published: list[str], statuses: tuple[str, ...]):
        loop = asyncio.get_running_loop()
        gates = {status: (asyncio.Event(), threading.Event()) for status in statuses}
        atomic_write = store.atomic_write

        def write(path: Path, data: str) -> None:
            if path != index:
                atomic_write(path, data)
                return
            status = json.loads(data)['status']
            if status in gates:
                at_index, release = gates[status]
                loop.call_soon_threadsafe(at_index.set)
                if not release.wait(WAIT_SECONDS):
                    raise TimeoutError('测试未释放会话索引写入')
            atomic_write(path, data)
            published.append(status)

        monkeypatch.setattr(store, 'atomic_write', write)
        return gates

    return install


def test_model_starts_after_queued_index_and_journal_commit(store: SessionStore, hold_index: IndexGates) -> None:
    async def run() -> None:
        published: list[str] = []
        seen = []

        async def model(history: list[dict], **kwargs) -> dict:
            seen.append((store.read_record(index), published.copy(), session.record['status'], history[-1]))
            return {'role': 'assistant', 'content': '已收到'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
        index = store.directory(session.id) / 'session.json'
        at_index, release = hold_index(index, published, ('queued',))['queued']
        try:
            manager.submit(session, TURN_INPUT)
            await asyncio.wait_for(at_index.wait(), WAIT_SECONDS)
            assert not seen
            assert session.busy
            assert session.record['status'] == 'queued'
            assert store.read_record(index)['status'] == 'idle'
            release.set()
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            assert len(seen) == 1
            saved, pre_model_writes, active_status, input_message = seen[0]
            assert pre_model_writes == ['queued']
            assert saved['status'] == 'queued'
            assert saved['last_run']['turn_id'] == session.record['last_run']['turn_id']
            assert saved['messages'][-1] == {'role': 'user', 'content': TURN_INPUT}
            assert input_message == saved['messages'][-1]
            assert active_status == 'running'
            assert store.read_record(index)['status'] == 'idle'
        finally:
            release.set()
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)

    asyncio.run(run())


def test_stop_during_reply_commit_persists_cancelled_turn(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        reply = {'role': 'assistant', 'content': '已收到'}

        async def model(history: list[dict], **kwargs) -> dict:
            return reply

        manager = SessionManager(store, model=model)
        session = manager.create()
        await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
        index = store.directory(session.id) / 'session.json'
        loop = asyncio.get_running_loop()
        at_reply_index = asyncio.Event()
        release = threading.Event()
        atomic_write = store.atomic_write

        def write(path: Path, data: str) -> None:
            if path == index and json.loads(data)['message_count'] == 3 and not release.is_set():
                loop.call_soon_threadsafe(at_reply_index.set)
                if not release.wait(WAIT_SECONDS):
                    raise TimeoutError('测试未释放回复写入')
            atomic_write(path, data)

        monkeypatch.setattr(store, 'atomic_write', write)
        try:
            manager.submit(session, TURN_INPUT)
            await asyncio.wait_for(at_reply_index.wait(), WAIT_SECONDS)
            assert session.busy
            assert store.read_record(index)['status'] == 'queued'
            manager.cancel(session)
            assert session.record['status'] == 'stopping'
            release.set()
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            saved = store.read_record(index)
            assert (saved['status'], saved['last_run']['status']) == ('cancelled', 'cancelled')
            assert saved['messages'][-1] == reply
            assert not session.busy
        finally:
            release.set()
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)

    asyncio.run(run())


@pytest.mark.parametrize('failure_at', ['journal', 'index'])
def test_initial_queued_write_failure_stops_turn_before_model_and_tools(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch, failure_at: str,
) -> None:
    async def run() -> None:
        calls: list[str] = []
        notifications: list[tuple[str, str]] = []

        async def model(history: list[dict], **kwargs) -> dict:
            calls.append(history[-1]['content'])
            if history[-1] == {'role': 'user', 'content': TURN_INPUT}:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'would-write', 'function': {'name': 'create_file', 'arguments': json.dumps({
                        'path': 'unexpected.txt', 'content': '出现了副作用'})},
                }]}
            return {'role': 'assistant', 'content': '已收到'}

        manager = SessionManager(store, model=model, permission_override='full_access')
        session = manager.create()
        await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
        index = store.directory(session.id) / 'session.json'
        manager.notify = lambda: notifications.append((session.record['status'], session.record['error']))
        failed = threading.Event()

        def fail_first_queued(status: str) -> None:
            if status == 'queued' and not failed.is_set():
                failed.set()
                raise OSError(errno.ENOSPC, '测试注入的写入失败')

        if failure_at == 'journal':
            save = store.save

            def write_journal(record: dict) -> None:
                fail_first_queued(record['status'])
                save(record)

            monkeypatch.setattr(store, 'save', write_journal)
        else:
            atomic_write = store.atomic_write

            def write_index(path: Path, data: str) -> None:
                if path == index:
                    fail_first_queued(json.loads(data)['status'])
                atomic_write(path, data)

            monkeypatch.setattr(store, 'atomic_write', write_index)

        try:
            manager.submit(session, TURN_INPUT)
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            assert failed.is_set()
            unexpected_file = store.workspace_path(session.id) / 'unexpected.txt'
            assert (calls, unexpected_file.exists()) == ([], False)
            assert session.record['status'] == 'error'
            assert '保存' in session.record['error']
            assert ('error', session.record['error']) in notifications
            assert store.read_record(index)['status'] == 'error'

            # 失败只影响当前轮，任务结束后可立即在同一会话提交。
            manager.submit(session, '第二轮')
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            assert calls == ['第二轮']
            saved = store.read_record(index)
            assert (saved['status'], saved['error']) == ('idle', '')
            assert saved['messages'][-2:] == [
                {'role': 'user', 'content': '第二轮'}, {'role': 'assistant', 'content': '已收到'},
            ]
        finally:
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)

    asyncio.run(run())


def test_cancel_waiting_for_queued_commit_then_submit_next_turn(
    store: SessionStore, hold_index: IndexGates,
) -> None:
    async def run() -> None:
        published: list[str] = []
        calls: list[tuple[str, list[str]]] = []

        async def model(history: list[dict], **kwargs) -> dict:
            calls.append((history[-1]['content'], published.copy()))
            return {'role': 'assistant', 'content': '已收到'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
        index = store.directory(session.id) / 'session.json'
        at_index, release = hold_index(index, published, ('queued',))['queued']
        try:
            manager.submit(session, TURN_INPUT)
            await asyncio.wait_for(at_index.wait(), WAIT_SECONDS)
            manager.cancel(session)
            after_cancel = asyncio.Event()
            asyncio.get_running_loop().call_soon(after_cancel.set)
            await asyncio.wait_for(after_cancel.wait(), WAIT_SECONDS)
            assert session.busy
            assert session.record['status'] == 'stopping'
            with pytest.raises(ValueError, match='正在运行'):
                manager.submit(session, '写入尚未排空')
            assert not published

            release.set()
            result, = await asyncio.wait_for(asyncio.gather(session.task, return_exceptions=True), WAIT_SECONDS)
            assert result is None or isinstance(result, asyncio.CancelledError)
            assert session.record['status'] == 'cancelled'
            assert store.read_record(index)['status'] == 'cancelled'
            assert published == ['queued', 'cancelled']
            assert not calls

            manager.submit(session, '取消后的下一轮')
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            assert calls == [('取消后的下一轮', ['queued', 'cancelled', 'queued'])]
            saved = store.read_record(index)
            assert saved['status'] == 'idle'
            assert [message['content'] for message in saved['messages'] if message['role'] == 'user'] == [
                TURN_INPUT, '取消后的下一轮',
            ]
        finally:
            release.set()
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)

    asyncio.run(run())


def test_task_cancelled_before_first_tick_drains_terminal_save(
    store: SessionStore, hold_index: IndexGates,
) -> None:
    async def run() -> None:
        published: list[str] = []
        calls: list[str] = []

        async def model(history: list[dict], **kwargs) -> dict:
            calls.append(history[-1]['content'])
            return {'role': 'assistant', 'content': '已收到'}

        manager = SessionManager(store, model=model)
        session = manager.create()
        await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
        index = store.directory(session.id) / 'session.json'
        gates = hold_index(index, published, ('queued', 'cancelled'))
        at_queued, release_queued = gates['queued']
        at_cancelled, release_cancelled = gates['cancelled']
        try:
            manager.submit(session, TURN_INPUT)
            original_task = session.task
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(original_task, 0)
            assert original_task.cancelled()
            await asyncio.wait_for(at_queued.wait(), WAIT_SECONDS)
            assert session.busy
            with pytest.raises(ValueError, match='正在运行'):
                manager.submit(session, '写入尚未排空')
            release_queued.set()
            await asyncio.wait_for(at_cancelled.wait(), WAIT_SECONDS)
            assert session.busy
            assert store.read_record(index)['status'] == 'queued'
            with pytest.raises(ValueError, match='正在运行'):
                manager.submit(session, '取消状态尚未写入')
            release_cancelled.set()
            await asyncio.wait_for(manager.flush(), WAIT_SECONDS)
            assert not session.busy
            assert session.record['status'] == store.read_record(index)['status'] == 'cancelled'
            assert published == ['queued', 'cancelled']
            assert not calls

            manager.submit(session, '取消后的下一轮')
            await asyncio.wait_for(session.task, WAIT_SECONDS)
            assert calls == ['取消后的下一轮']
        finally:
            release_queued.set()
            release_cancelled.set()
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)

    asyncio.run(run())


def test_reopen_committed_queued_turn_as_interrupted_without_replay(tmp_path: Path, sandbox_env: Path) -> None:
    state = tmp_path / 'state'
    # 在受控模型入口直接退出子进程，模拟没有机会执行关闭处理的应用。
    script = '''
import asyncio
import os
import sys
from pathlib import Path

from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore

async def model(history, **kwargs):
    os._exit(31)

async def run():
    store = SessionStore(Path(sys.argv[1]), workspace_root=Path(sys.argv[2]))
    manager = SessionManager(store, model=model)
    session = manager.create()
    manager.submit(session, sys.argv[3])
    await session.task

asyncio.run(run())
'''
    process = subprocess.run(
        [sys.executable, '-c', script, str(state), str(sandbox_env), TURN_INPUT],
        env={**os.environ, 'PYTHON_DOTENV_DISABLED': '1', 'COMMAND_EXECUTION_ENVIRONMENT': 'sandbox'},
        capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 31, process.stderr
    index, = state.glob('*/session.json')
    record = SessionStore.read_record(index)
    assert record['status'] == 'queued'
    assert record['messages'][-1] == {'role': 'user', 'content': TURN_INPUT}

    calls: list[str] = []

    async def model(history: list[dict], **kwargs) -> dict:
        calls.append(history[-1]['content'])
        return {'role': 'assistant', 'content': '不应调用'}

    async def reopen(reopened: SessionStore) -> None:
        manager = SessionManager(reopened, model=model)
        session = manager.sessions[record['id']]
        try:
            assert session.record['messages'] == record['messages']
            assert session.record['status'] == 'interrupted'
            assert session.record['error']
            assert session.task is None
            assert not session.busy
            assert reopened.read_record(index)['status'] == 'interrupted'
        finally:
            await asyncio.wait_for(manager.shutdown(), WAIT_SECONDS)
        assert not calls

    reopened = SessionStore(state, workspace_root=sandbox_env)
    try:
        asyncio.run(reopen(reopened))
    finally:
        reopened.close()
