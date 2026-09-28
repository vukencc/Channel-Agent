import asyncio

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_recent_runs_have_distinct_trace_ids_and_bounded_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUN_HISTORY_LIMIT', 2, raising=False)
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        async def model(*args, **kwargs):
            return {'role': 'assistant', 'content': 'ok'}
        manager = SessionManager(store, model=model)
        session = manager.create()
        for text in ['one', 'two', 'three']:
            manager.submit(session, text)
            await session.task
        history = store.load_all()[0]['run_history']
        assert len(history) == 2
        assert len({row['turn_id'] for row in history}) == 2
        assert all(row['status'] == 'idle' for row in history)
        await manager.shutdown()
        store.close()
    asyncio.run(run())
