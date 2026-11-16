"""受控线程门控复现工具超时与排空；不调用真实模型。"""
import asyncio
from contextlib import contextmanager
import threading

import pytest
from pydantic import BaseModel

from agent_core import config
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools.base import TOOL_REGISTRY, Tool
from agent_core.tools.sandbox import SandboxError


class EmptyArgs(BaseModel):
    pass


def call():
    return {'id': 'probe', 'type': 'function',
            'function': {'name': 'timeout_drain_probe', 'arguments': '{}'}}


@pytest.mark.parametrize('mode', ['guard_cancelled', 'guard_identity', 'worker_error', 'worker_sandbox',
                                  'external_cancel', 'external_cancel_error', 'read'])
@pytest.mark.parametrize('command_jobs', [False, True])
def test_timeout_drains_writes_and_preserves_unknown_evidence(tmp_path, sandbox_env, monkeypatch, mode, command_jobs):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', command_jobs)
    started = threading.Event()
    release = threading.Event()
    executed = []

    def tool():
        executed.append(True)
        started.set()
        assert release.wait(5)
        if mode in {'worker_error', 'external_cancel_error'}:
            raise OSError('controlled audit failure')
        if mode == 'worker_sandbox':
            raise SandboxError('工作区操作已取消')
        return 'late success'

    monkeypatch.setitem(TOOL_REGISTRY, 'timeout_drain_probe', Tool(
        'timeout_drain_probe', EmptyArgs, tool, timeout_s=.02,
        concurrency='read' if mode == 'read' else 'serial'))

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        manager = SessionManager(store)
        session = manager.create()
        evidence = {}
        if mode in {'guard_cancelled', 'guard_identity'}:
            guard_owner = manager.command_jobs if manager.command_jobs is not None else manager.workspaces
            original = guard_owner.guard
            @contextmanager
            def delayed(root, cancelled):
                started.set()
                assert release.wait(5)
                if mode == 'guard_identity':
                    raise SandboxError('工作区目录已被替换')
                with original(root, cancelled):
                    yield
            monkeypatch.setattr(guard_owner, 'guard', delayed)
        try:
            task = asyncio.create_task(manager._tool(session, call(), evidence=evidence))
            session.task = task
            assert await asyncio.to_thread(started.wait, 3)
            if mode != 'read':
                assert await asyncio.to_thread(session.cancelled.wait, 3)
                assert not task.done() and session.tool_workers and session.busy
                with pytest.raises(ValueError, match='正在运行'):
                    manager.submit(session, '不可重入')
            if mode in {'external_cancel', 'external_cancel_error'}:
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done() and session.tool_workers
                task.cancel()
                await asyncio.sleep(.01)
                assert not task.done() and session.tool_workers
            if mode == 'read':
                result = await asyncio.wait_for(task, 3)
                assert '超时' in result and session.tool_workers
            release.set()
            if mode == 'worker_error':
                with pytest.raises(OSError, match='controlled audit failure'):
                    await task
            elif mode == 'worker_sandbox':
                with pytest.raises(SandboxError, match='工作区操作已取消'):
                    await task
            elif mode == 'guard_identity':
                with pytest.raises(SandboxError, match='工作区目录已被替换'):
                    await task
            elif mode in {'external_cancel', 'external_cancel_error'}:
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
                if mode == 'external_cancel_error':
                    assert isinstance(caught.value.__cause__, OSError)
            elif mode != 'read':
                assert '超时' in await task
            assert evidence['source'] == 'result_unknown'
            if mode == 'guard_cancelled':
                assert not executed
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())
