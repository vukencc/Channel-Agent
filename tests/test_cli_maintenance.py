"""Local CLI maintenance uses explicit consent and optional tool detail display."""
import asyncio
from contextlib import asynccontextmanager
import copy
import json

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ai_agent_startup import config
from ai_agent_startup.core.cli import AgentCLI
from ai_agent_startup.core.storage import SessionStore
from ai_agent_startup.core.transcript import Transcript


@pytest.fixture
def store(tmp_path, monkeypatch):
    project, raw, cache, logs = (tmp_path / name for name in ('project', 'raw', 'cache', 'logs'))
    for path in (project, raw, cache, logs):
        path.mkdir()
    monkeypatch.setattr(config, 'PROJECT_ROOT', project)
    monkeypatch.setattr(config, 'DOC_DIR', raw)
    monkeypatch.setattr(config, 'RAG_CACHE_DIR', cache)
    monkeypatch.setattr(config, 'AUDIT_LOG', logs / 'audit.jsonl')
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)
    value = SessionStore(tmp_path / 'state', tmp_path / 'workspace')
    yield value
    value.close()


async def _drain(cli):
    while jobs := list(cli._ui_jobs):
        await asyncio.wait_for(asyncio.gather(*jobs), 3)
        await asyncio.sleep(0)
    await cli.manager.flush()
    await cli.drain_render()


@asynccontextmanager
async def _client(store):
    with create_pipe_input() as pipe:
        cli = AgentCLI(store, input=pipe, output=DummyOutput())
        try:
            await _drain(cli)
            yield cli
        finally:
            for session in cli.manager.sessions.values():
                session.forking = False
                session.confirmation = None
            await _drain(cli)
            cli._render_closed = True
            if cli._refresh_handle:
                cli._refresh_handle.cancel()
                cli._refresh_handle = None
            await cli.drain_render()
            await cli.manager.shutdown()


def _messages():
    return [
        {'role': 'user', 'content': 'keep user narrative'},
        {'role': 'assistant', 'content': 'keep explanation\n```python\nprint("keep code")\n```',
         'tool_calls': [{'id': 'detail-call', 'function': {'name': 'read_file',
             'arguments': json.dumps({'path': 'ARGS_DETAIL_MARKER'})}}]},
        {'role': 'tool', 'tool_call_id': 'detail-call', 'content': 'RESULT_DETAIL_MARKER'},
        {'role': 'assistant', 'content': 'keep final answer'},
    ]


def test_cli_hides_tool_payloads_by_default_and_details_toggle_preserves_history(store):
    async def run():
        async with _client(store) as cli:
            session = cli.active
            session.record['messages'].extend(_messages())
            original = copy.deepcopy(session.record['messages'])
            await cli.manager.save(session)
            cli.render(force=True)
            await cli.drain_render()
            assert 'keep user narrative' in cli.chat.text
            assert 'keep explanation' in cli.chat.text
            assert 'print("keep code")' in cli.chat.text
            assert 'keep final answer' in cli.chat.text
            assert 'ARGS_DETAIL_MARKER' not in cli.chat.text
            assert 'RESULT_DETAIL_MARKER' not in cli.chat.text
            assert cli.handle('/details')
            assert cli.handle('/details on')
            await _drain(cli)
            assert 'ARGS_DETAIL_MARKER' in cli.chat.text
            assert 'RESULT_DETAIL_MARKER' in cli.chat.text
            assert cli.handle('/details off')
            await _drain(cli)
            assert 'ARGS_DETAIL_MARKER' not in cli.chat.text
            assert 'RESULT_DETAIL_MARKER' not in cli.chat.text
            assert session.record['messages'] == original
            assert store.read_record(store.directory(session.id) / 'session.json')['messages'] == original
    asyncio.run(run())


def test_transcript_default_keeps_legacy_detail_behavior_and_can_explicitly_hide():
    messages = [{'role': 'system', 'content': 'system'}, *_messages()]
    legacy = Transcript()
    legacy.sync(messages)
    text, _ = legacy.page()
    assert 'ARGS_DETAIL_MARKER' in text and 'RESULT_DETAIL_MARKER' in text
    compact = Transcript(show_details=False)
    compact.sync(messages)
    text, _ = compact.page()
    assert 'keep explanation' in text and 'print("keep code")' in text
    assert 'ARGS_DETAIL_MARKER' not in text and 'RESULT_DETAIL_MARKER' not in text


def test_cleanup_without_arguments_previews_without_changing_files(store):
    async def run():
        async with _client(store) as cli:
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep cache')
            config.AUDIT_LOG.write_bytes(b'keep log')
            identifier = cli.current
            assert cli.handle('/cleanup')
            await _drain(cli)
            assert cache.read_bytes() == b'keep cache'
            assert config.AUDIT_LOG.read_bytes() == b'keep log'
            assert set(cli.manager.sessions) == {identifier}
            assert not cli.handle('/yes')
    asyncio.run(run())


def test_cleanup_cache_waits_for_consent_and_no_cancels(store):
    async def run():
        async with _client(store) as cli:
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep cache')
            assert cli.handle('/cleanup cache')
            await _drain(cli)
            assert cache.read_bytes() == b'keep cache'
            assert cli.handle('/no')
            await _drain(cli)
            assert cache.read_bytes() == b'keep cache'
            assert not cli.handle('/yes')
    asyncio.run(run())


def test_cleanup_confirmation_is_bound_to_current_session(store):
    async def run():
        async with _client(store) as cli:
            owner = cli.current
            other = cli.manager.create('other')
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep cache')
            assert cli.handle('/cleanup cache')
            await _drain(cli)
            cli.select(other.id)
            await _drain(cli)
            assert not cli.handle('/yes')
            assert cache.read_bytes() == b'keep cache'
            cli.select(owner)
            await _drain(cli)
            assert cli.handle('/yes')
            await _drain(cli)
            assert not cache.exists()
            assert set(cli.manager.sessions) == {owner, other.id}
    asyncio.run(run())


@pytest.mark.parametrize('selection', ['cache', 'logs', 'all'])
def test_confirmed_cleanup_only_changes_selected_resources(store, selection):
    async def run():
        async with _client(store) as cli:
            old_id = cli.current
            workspace = cli.manager.workspace(cli.active)
            (workspace / 'keep.txt').write_text('keep project')
            cache = config.RAG_CACHE_DIR / 'cached.bin'
            cache.write_bytes(b'cached')
            config.AUDIT_LOG.write_bytes(b'OLD_LOG_CONTENT')
            assert cli.handle('/cleanup ' + selection)
            await _drain(cli)
            assert cache.read_bytes() == b'cached'
            assert config.AUDIT_LOG.read_bytes() == b'OLD_LOG_CONTENT'
            assert old_id in cli.manager.sessions
            assert cli.handle('/yes')
            await _drain(cli)
            assert cache.exists() is (selection == 'logs')
            assert (b'OLD_LOG_CONTENT' in config.AUDIT_LOG.read_bytes()) is (selection == 'cache')
            if selection == 'all':
                assert old_id not in cli.manager.sessions
                assert (store.root / '.trash' / old_id / 'session.json').is_file()
                assert cli.current in cli.manager.sessions and cli.current != old_id
            else:
                assert old_id in cli.manager.sessions
            assert (workspace / 'keep.txt').read_text() == 'keep project'
    asyncio.run(run())


def test_cleanup_sessions_archives_old_records_and_creates_blank_replacement(store):
    async def run():
        async with _client(store) as cli:
            first = cli.current
            second = cli.manager.create('second').id
            workspace = cli.manager.workspace(cli.manager.sessions[first])
            (workspace / 'keep.txt').write_text('keep project')
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep cache')
            assert cli.handle('/cleanup sessions')
            await _drain(cli)
            assert {first, second} <= set(cli.manager.sessions)
            assert cli.handle('/yes')
            await _drain(cli)
            assert len(cli.manager.sessions) == 1
            assert cli.current not in {first, second}
            assert cli.current in cli.manager.sessions
            assert len(cli.active.record['messages']) == 1
            assert all((store.root / '.trash' / identifier / 'session.json').is_file()
                       for identifier in (first, second))
            assert (workspace / 'keep.txt').read_text() == 'keep project'
            assert cache.read_bytes() == b'keep cache'
    asyncio.run(run())


@pytest.mark.parametrize('command', ['/cleanup unknown', '/cleanup cache logs', '/details unknown'])
def test_invalid_maintenance_or_detail_arguments_are_rejected(store, command):
    async def run():
        async with _client(store) as cli:
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep')
            assert not cli.handle(command)
            await _drain(cli)
            assert cache.read_bytes() == b'keep'
            assert not cli.handle('/yes')
    asyncio.run(run())


@pytest.mark.parametrize('pending', ['busy', 'confirmation', 'forget', 'delete'])
def test_cleanup_blocks_busy_agents_and_existing_confirmation(store, pending):
    async def run():
        async with _client(store) as cli:
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep')
            if pending == 'busy':
                cli.active.forking = True
            elif pending == 'confirmation':
                cli.active.confirmation = {'id': 'existing', 'prompt': 'existing confirmation'}
            elif pending == 'forget':
                cli.pending_forget = cli.current
            else:
                cli.pending_delete = cli.current
            assert not cli.handle('/cleanup cache')
            await _drain(cli)
            assert cache.read_bytes() == b'keep'
            cli.active.forking = False
            cli.active.confirmation = None
            cli.pending_forget = None
            cli.pending_delete = None
    asyncio.run(run())


def test_second_cleanup_request_cannot_replace_pending_selection(store):
    async def run():
        async with _client(store) as cli:
            cache = config.RAG_CACHE_DIR / 'keep.bin'
            cache.write_bytes(b'keep')
            config.AUDIT_LOG.write_bytes(b'OLD_LOG_CONTENT')
            assert cli.handle('/cleanup cache')
            await _drain(cli)
            assert not cli.handle('/cleanup logs')
            with pytest.raises(ValueError, match='清理或删除'):
                cli.request_delete(cli.current)
            assert cli.handle('/yes')
            await _drain(cli)
            assert not cache.exists()
            assert b'OLD_LOG_CONTENT' in config.AUDIT_LOG.read_bytes()
    asyncio.run(run())


def test_logs_cleanup_can_prepare_confirmation_when_cache_preview_has_error(store, monkeypatch):
    async def run():
        async with _client(store) as cli:
            unsafe_cache = config.PROJECT_ROOT / 'src'
            unsafe_cache.mkdir()
            sentinel = unsafe_cache / 'keep.py'
            sentinel.write_text('project source')
            monkeypatch.setattr(config, 'RAG_CACHE_DIR', unsafe_cache)
            config.AUDIT_LOG.write_bytes(b'OLD_LOG_CONTENT')
            assert cli.handle('/cleanup logs')
            await _drain(cli)
            assert cli.pending_cleanup is not None
            assert cli.pending_cleanup[0] == cli.current
            assert cli.pending_cleanup[1]['logs'] is True
            assert config.AUDIT_LOG.read_bytes() == b'OLD_LOG_CONTENT'
            assert cli.handle('/yes')
            await _drain(cli)
            assert b'OLD_LOG_CONTENT' not in config.AUDIT_LOG.read_bytes()
            assert sentinel.read_text() == 'project source'
    asyncio.run(run())


@pytest.mark.parametrize('first', ['delete', 'cleanup'])
def test_lifecycle_commands_reject_overlap_before_ui_drain(store, first):
    async def run():
        async with _client(store) as cli:
            session = cli.active
            gate = asyncio.Event()
            blocker = asyncio.create_task(gate.wait())
            cli._ui_jobs.add(blocker)
            blocker.add_done_callback(cli._ui_jobs.discard)
            try:
                if first == 'delete':
                    cli.delete_command(session)
                else:
                    cli.pending_cleanup = (session.id, {'cache': False, 'logs': True, 'sessions': False})
                    cli.cleanup_command()
                assert cli._lifecycle_busy
                with pytest.raises(ValueError, match='清理或删除'):
                    cli.cleanup_command()
                with pytest.raises(ValueError, match='清理或删除'):
                    cli.delete_command(session)
                with pytest.raises(ValueError, match='清理或删除'):
                    cli.request_delete(session.id)
                assert not cli.handle('/cleanup logs')
                assert not cli.handle('/delete ' + session.id)
            finally:
                gate.set()
            await _drain(cli)
            assert not cli._lifecycle_busy
    asyncio.run(run())
