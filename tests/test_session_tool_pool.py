"""控制工具不能等待被自身占满的默认线程池。"""
import asyncio
import concurrent.futures
import json
import threading

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


@pytest.mark.parametrize('name', ['send_session_message', 'create_session'])
def test_control_tool_completes_with_one_available_worker(tmp_path, monkeypatch, name):
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_SESSION_BUDGETS', False)

    async def run():
        async def model(*args, **kwargs):
            return {'role': 'assistant', 'content': 'done'}

        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        parent = manager.create()
        child = manager.create()
        child.record['delegated_from'] = parent.id
        await manager.save(child)
        await manager.flush()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=2))
        release = threading.Event()
        entered = threading.Event()

        def occupy():
            entered.set()
            release.wait(10)

        occupied = loop.run_in_executor(None, occupy)
        while not entered.is_set():
            await asyncio.sleep(0)
        arguments = ({'session_id': child.id, 'message': 'hello'} if name == 'send_session_message'
                     else {'task': 'inspect', 'tool_names': [], 'max_rounds': 1})
        task = asyncio.create_task(manager._tool(parent, {'id': 'call', 'function': {
            'name': name, 'arguments': json.dumps(arguments)}}))
        try:
            done, _ = await asyncio.wait({task}, timeout=1)
            completed_without_releasing_worker = bool(done)
        finally:
            # 无论断言成败均解开旧实现的死锁，以免测试本身挂住。
            release.set()
            await occupied
            await asyncio.wait_for(task, 5)
            await manager.shutdown()
            store.close()
        assert completed_without_releasing_worker, '控制工具嵌套等待已耗尽的默认线程池'

    asyncio.run(run())
