"""突发通信不得在一个模型轮次中无界灌入上下文。"""
import asyncio
import json

from ai_agent_startup import config
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_one_message_per_model_round_leaves_remaining_messages_unread(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            parent = manager.create()
            child = manager.create()
            child.record['delegated_from'] = parent.id
            await manager.save(child)
            await manager.flush()
            for text in ('one', 'two', 'three'):
                manager.communication.send(child.id, parent.id, text)
            await manager._drain_inbox(parent)
            assert parent.record['inbox_cursor'] == 1
            assert manager.communication.pending_count(parent) == 2
            assert len([message for message in parent.record['messages'] if '_agent_message' in message]) == 1
            await manager._drain_inbox(parent)
            assert parent.record['inbox_cursor'] == 2
            assert manager.communication.pending_count(parent) == 1
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())


def test_read_page_fits_tool_output_budget_and_does_not_skip_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 6000)
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        manager = SessionManager(store)
        parent, child = manager.create(), manager.create()
        child.record['delegated_from'] = parent.id
        manager.save(child)
        for _ in range(6):
            manager.communication.send(child.id, parent.id, '进度' * 1500)
        page = manager.communication.read(parent.id, limit=100, max_chars=config.TOOL_MAX_OUTPUT)
        assert len(json.dumps(page, ensure_ascii=False)) <= config.TOOL_MAX_OUTPUT
        assert page['next_seq'] == 1
        next_page = manager.communication.read(parent.id, page['next_seq'], 100, max_chars=config.TOOL_MAX_OUTPUT)
        assert next_page['messages'][0]['seq'] == 2
    finally:
        store.close()


def test_small_tool_output_limit_does_not_block_internal_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'TOOL_MAX_OUTPUT', 1000)

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        try:
            parent, child = manager.create(), manager.create()
            child.record['delegated_from'] = parent.id
            await manager.save(child)
            await manager.flush()
            manager.communication.send(child.id, parent.id, 'x' * 2000)
            # CLI 只为初始化未读计数访问内部信箱，不受工具响应长度影响。
            assert manager.communication.read(parent.id, 0, 1)['next_seq'] == 1
            await manager._drain_inbox(parent)
            assert parent.record['inbox_cursor'] == 1
            assert 'x' * 2000 in parent.record['messages'][-1]['content']
        finally:
            await manager.shutdown()
            store.close()

    asyncio.run(run())
