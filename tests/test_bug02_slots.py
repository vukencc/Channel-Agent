import asyncio

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_waiting_tools_do_not_block_fifth_model(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_CONCURRENT_AGENTS', 4)
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        ready, release = asyncio.Event(), asyncio.Event()
        waiting = 0
        async def model(history, **kwargs):
            if history[-1]['content'] == 'wait':
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call', 'function': {'name': 'probe', 'arguments': '{}'}}]}
            return {'role': 'assistant', 'content': 'done'}
        manager = SessionManager(store, model=model)
        async def tool(*args):
            nonlocal waiting
            waiting += 1
            if waiting == 4:
                ready.set()
            await release.wait()
            return 'ok'
        monkeypatch.setattr(manager, '_tool', tool)
        try:
            for _ in range(4):
                manager.submit(manager.create(), 'wait')
            await asyncio.wait_for(ready.wait(), 1)
            fifth = manager.create()
            manager.submit(fifth, 'free')
            await asyncio.wait_for(asyncio.shield(fifth.task), .3)
            assert fifth.record['status'] == 'idle'
        finally:
            release.set()
            await manager.shutdown()
            store.close()
    asyncio.run(run())
