import asyncio

import pytest

from ai_agent_startup import config
from ai_agent_startup.core.context import build_model_history
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_schema_is_charged_to_context_budget(monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1200)
    with pytest.raises(ValueError, match='schema'):
        build_model_history([{'role': 'system', 'content': 's'}], schemas=[{'description': 'x' * 1300}])


def test_oversize_turn_is_exportable_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MODEL_INPUT_CHARS', 1000)
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        async def model(*args, **kwargs):
            pytest.fail('超限请求不能发出')
        manager = SessionManager(store, model=model)
        session = manager.create()
        manager.submit(session, 'x' * 1500)
        await session.task
        assert session.record['status'] == 'checkpoint'
        assert 'x' * 1500 in store.export(session.record).read_text()
        await manager.shutdown()
        store.close()
    asyncio.run(run())


def test_memory_limit_rejects_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'MEMORY_MAX_CHARS', 30, raising=False)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        record = store.create('test', 's')
        store.remember(record['id'], 'short')
        before = store.memory(record['id'])
        with pytest.raises(ValueError, match='MEMORY_MAX_CHARS'):
            store.remember(record['id'], 'x' * 40)
        assert store.memory(record['id']) == before
    finally:
        store.close()
