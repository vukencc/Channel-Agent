import asyncio
import time

from pydantic import BaseModel

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import TOOL_REGISTRY
from ai_agent_startup.tools.base import Tool


class Args(BaseModel):
    pass


def test_read_tools_run_in_parallel_and_excess_calls_are_paired(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MAX_TOOL_CALLS_PER_ROUND', 2, raising=False)
    calls = []
    def read():
        calls.append(time.monotonic())
        time.sleep(.15)
        return 'ok'
    tool = Tool('probe_read', Args, read)
    tool.concurrency = 'read'
    monkeypatch.setitem(TOOL_REGISTRY, tool.name, tool)
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        async def model(history, **kwargs):
            if history[-1]['role'] == 'user':
                return {'role': 'assistant', 'tool_calls': [{'id': str(i), 'function': {'name': tool.name, 'arguments': '{}'}} for i in range(3)]}
            assert [m['tool_call_id'] for m in history if m['role'] == 'tool'] == ['0', '1', '2']
            assert '上限' in history[-1]['content']
            return {'role': 'assistant', 'content': 'done'}
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, 'read')
        await session.task
        assert session.record['status'] == 'idle'
        assert len(calls) == 2
        assert abs(calls[0] - calls[1]) < .1
        await manager.shutdown()
        store.close()
    asyncio.run(run())
