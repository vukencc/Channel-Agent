"""并发收信的因果事件必须位于持久发送之后。"""
import asyncio
import threading

from ai_agent_startup import config
from ai_agent_startup.core import communication
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore


def test_read_cannot_publish_before_sent_when_audit_is_delayed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DEBUG', True)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    entered, release = threading.Event(), threading.Event()
    original = communication.audit

    def slow_audit(event, **fields):
        if event == 'session_message_sent':
            entered.set()
            assert release.wait(3)
        return original(event, **fields)
    monkeypatch.setattr(communication, 'audit', slow_audit)

    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        manager = SessionManager(store)
        sending = None
        try:
            parent, child = manager.create(), manager.create()
            child.record['delegated_from'] = parent.id
            await manager.flush()
            sending = asyncio.create_task(asyncio.to_thread(
                manager.communication.send, parent.id, child.id, 'private concurrent message'))
            assert await asyncio.to_thread(entered.wait, 3)
            received = await asyncio.to_thread(manager.communication.read, child.id)
            assert len(received['messages']) == 1
            release.set()
            await sending
            rows = manager.debug.snapshot()['timeline']
            sent = next(row for row in rows if row['kind'] == 'message.sent')
            read = next(row for row in rows if row['kind'] == 'message.read')
            assert sent['seq'] < read['seq']
            assert sent['elapsed_ms'] <= read['elapsed_ms']
            assert 'private concurrent message' not in str(rows)
        finally:
            release.set()
            if sending is not None:
                await asyncio.gather(sending, return_exceptions=True)
            await manager.shutdown()
            store.close()
    asyncio.run(run())
