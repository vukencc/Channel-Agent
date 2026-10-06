"""A completed command job remains active until its final audit is written."""

import asyncio
import json
import threading
import time

import pytest

from ai_agent_startup import config
from ai_agent_startup.core import command_jobs
from ai_agent_startup.core.sessions import SessionManager
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.tools import command
from ai_agent_startup.tools.sandbox import ToolContext, tool_context


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', True)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


def test_completed_command_job_waits_for_final_audit_before_session_archive(store, monkeypatch):
    audit_entered = threading.Event()
    release_audit = threading.Event()
    original_audit = command_jobs.audit

    def held_audit(event, **fields):
        if event == 'command_job_finished':
            audit_entered.set()
            if not release_audit.wait(5):
                raise TimeoutError('test did not release final audit')
        return original_audit(event, **fields)

    def fake_execute(command_text, reason, argv, isolated, *, timeout=None, on_output=None):
        if on_output is not None:
            on_output(b'done')
        return '[退出码] 0\ndone'

    monkeypatch.setattr(command_jobs, 'audit', held_audit)
    monkeypatch.setattr(command_jobs, 'isolated_command', lambda argv, **kwargs: argv)
    monkeypatch.setattr(command, '_execute', fake_execute)

    async def run():
        manager = SessionManager(store)
        try:
            session = manager.create()
            await manager.flush()
            workspace = store.workspace(session.id)
            context = ToolContext(workspace, store.directory(session.id) / 'audit.jsonl',
                                  lambda *_: True, session.cancelled,
                                  job_manager=manager.command_jobs, permission_policy='standard')
            with tool_context(context):
                identifier = manager.command_jobs.start('printf done', timeout=2)

            assert await asyncio.to_thread(audit_entered.wait, 3), 'final audit was not reached'
            deadline = time.monotonic() + 3
            while manager.command_jobs.status(session.id, identifier)['status'] != 'completed':
                assert time.monotonic() < deadline, 'job did not persist completed status'
                await asyncio.sleep(.01)

            future = manager.command_jobs.futures[identifier]
            assert not future.done()
            assert manager.command_jobs.logs(session.id, identifier) == 'done'
            with pytest.raises(ValueError, match='长命令'):
                manager.check_session_deletion(session)
            with pytest.raises(ValueError, match='长命令'):
                await manager.delete_session(session)
            assert store.directory(session.id).exists()

            release_audit.set()
            await asyncio.to_thread(future.result, 3)
            archived = await manager.delete_session(session)
            assert archived == store.root / '.trash' / session.id
            audit_rows = [json.loads(line) for line in (archived / 'audit.jsonl').read_text().splitlines()]
            assert any(row.get('event') == 'command_job_finished' and row.get('job_id') == identifier
                       and row.get('status') == 'completed' for row in audit_rows)
            assert not store.directory(session.id).exists()
            await asyncio.sleep(0)
            assert not store.directory(session.id).exists()
        finally:
            release_audit.set()
            await manager.shutdown()

    asyncio.run(run())
