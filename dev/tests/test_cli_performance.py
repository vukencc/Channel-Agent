"""UI work is bounded; complete transcript remains navigable without truncation."""
import asyncio
import time

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from core.cli import AgentCLI
from core.sessions import SessionManager
from core.storage import SessionStore
from core.transcript import Transcript


def test_full_history_and_large_message_are_accessible_by_paging():
    transcript = Transcript()
    messages = [{'role': 'user', 'content': f'old-{i}'} for i in range(120)]
    messages.append({'role': 'assistant', 'content': 'line\n' * 9000 + 'tail-marker-after-20000'})
    transcript.sync(messages)
    transcript.top()
    first, _ = transcript.page()
    assert 'old-0' in first
    transcript.bottom()
    last, _ = transcript.page()
    assert 'tail-marker-after-20000' in last
    assert len(last.splitlines()) <= transcript.PAGE_LINES
    assert 'old-0' in '\n'.join(transcript.lines)


def test_stream_notifications_coalesce_and_background_does_not_rebuild(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'crud_tests')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                active = cli.active
                active.record['messages'] += [{'role': 'assistant', 'content': 'large line\n' * 1000} for _ in range(60)]
                cli.render(force=True)
                changes = []
                original = cli.chat.buffer.set_document
                def measured(*args, **kwargs):
                    changes.append(1)
                    return original(*args, **kwargs)
                cli.chat.buffer.set_document = measured
                for _ in range(1000):
                    cli.manager.emit(active, 'content', 'token\n')
                await asyncio.sleep(.15)
                assert len(changes) <= 2
                assert cli.transcript.count == len(active.record['messages'])
                other = cli.manager.create('background')
                changes.clear()
                for _ in range(1000):
                    cli.manager.emit(other, 'content', 'background')
                await asyncio.sleep(.15)
                assert not changes
                cli.handle('/top')
                before = cli.chat.text
                cli.manager.emit(active, 'content', 'new tail')
                await asyncio.sleep(.15)
                assert cli.chat.text == before
                assert not cli.transcript.follow
                await cli.manager.shutdown()
                if cli._refresh_handle:
                    cli._refresh_handle.cancel()
        finally:
            store.close()
    asyncio.run(run())


def test_slow_save_does_not_block_loop_and_latest_checkpoint_wins(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'crud_tests')
        original = store.save
        def slow_save(record):
            time.sleep(.12)
            original(record)
        monkeypatch.setattr(store, 'save', slow_save)
        try:
            manager = SessionManager(store)
            session = manager.create()
            ticks = []
            async def heartbeat():
                for _ in range(10):
                    ticks.append(time.monotonic())
                    await asyncio.sleep(.01)
            session.record['title'] = 'latest'
            manager.save(session)
            await heartbeat()
            assert len(ticks) == 10
            assert max(b-a for a,b in zip(ticks, ticks[1:])) < .08
            await manager.flush()
            assert store.load_all()[0]['title'] == 'latest'
        finally:
            store.close()
    asyncio.run(run())


def test_workspace_migration_preserves_originals_and_uses_configured_root(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'crud_tests')
    try:
        record = store.create('old', 'system')
        old = store.directory(record['id']) / 'workspace'
        old.mkdir()
        (old / 'note.txt').write_text('existing')
        new = store.workspace(record['id'])
        assert new == tmp_path / 'crud_tests' / record['id']
        assert (old / 'note.txt').read_text() == (new / 'note.txt').read_text() == 'existing'
        (new / 'note.txt').write_text('updated')
        assert (store.workspace(record['id']) / 'note.txt').read_text() == 'updated'
    finally:
        store.close()


def test_workspace_conflict_never_overwrites_files(tmp_path):
    import pytest
    store = SessionStore(tmp_path / 'state', tmp_path / 'crud_tests')
    try:
        record = store.create('old', 'system')
        old = store.directory(record['id']) / 'workspace'
        old.mkdir()
        (old / 'note.txt').write_text('old')
        new = store.workspace_path(record['id'])
        new.mkdir(parents=True)
        (new / 'note.txt').write_text('new')
        with pytest.raises(ValueError, match='未覆盖'):
            store.workspace(record['id'])
        assert (old / 'note.txt').read_text() == 'old'
        assert (new / 'note.txt').read_text() == 'new'
    finally:
        store.close()


def test_cached_state_directory_does_not_redirect_tool_output(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'SANDBOX_DIR', tmp_path / 'crud_tests')
    store = SessionStore(tmp_path / '.cache' / 'state')
    try:
        record = store.create('cached state', 'system')
        root = store.workspace(record['id'])
        (root / 'result.txt').write_text('output')
        assert root == tmp_path / 'crud_tests' / record['id']
        assert not (store.directory(record['id']) / 'workspace').exists()
    finally:
        store.close()


def test_cancel_during_checkpoint_preserves_valid_tool_history(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'crud_tests')
        original = store.save
        def delayed(record):
            time.sleep(.05)
            original(record)
        monkeypatch.setattr(store, 'save', delayed)
        ready = asyncio.Event()
        async def model(history, **kwargs):
            ready.set()
            return {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'pending', 'function': {'name': 'create_file', 'arguments': '{"path":"never.txt"}'}}]}
        try:
            manager = SessionManager(store, model=model)
            session = manager.create()
            manager.submit(session, 'test')
            await ready.wait()
            manager.cancel(session)
            await session.task
            assert session.record['status'] == 'cancelled'
            assert session.record['messages'][-1]['role'] == 'tool'
            assert session.record['messages'][-1]['tool_call_id'] == 'pending'
            assert not (store.workspace_path(session.id) / 'never.txt').exists()
            assert store.load_all()[0]['messages'][-1]['tool_call_id'] == 'pending'
        finally:
            store.close()
    asyncio.run(run())
