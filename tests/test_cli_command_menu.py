"""Command discovery, keyboard selection, and explicit session deletion consent."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ai_agent_startup.core.cli import AgentCLI
from ai_agent_startup.core.storage import SessionStore


@pytest.fixture
def cli(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    with create_pipe_input() as pipe:
        client = AgentCLI(store, input=pipe, output=DummyOutput())
        yield client
    store.close()


def test_completion_has_command_policy_and_session_arguments(cli):
    from ai_agent_startup.core.cli_commands import CommandCompleter
    from prompt_toolkit.document import Document
    completer = CommandCompleter(cli)
    def options(value):
        return [row.text for row in completer.get_completions(Document(value), None)]
    assert '/permissions' in options('/per')
    assert 'smart' in options('/policy sm')
    assert 'full_access' in options('/policy full')
    assert cli.current in options('/switch ')
    assert not options('//policy')


def test_palette_searches_description_and_preserves_draft(cli):
    cli.input.text = 'keep my unsent draft'
    cli.open_palette()
    cli.palette.query.text = '重命名'
    assert [row.value for row in cli.palette.matches()] == ['/rename']
    cli.palette.choose()
    assert cli.input.text == '/rename '
    assert cli.handle('/rename changed')
    assert cli.active.record['title'] == 'changed'
    assert cli._command_draft == (cli.current, 'keep my unsent draft')


def test_sessions_picker_restores_hidden_session(cli):
    first = cli.current
    cli.active.record['title'] = 'hidden original'
    cli.handle('/new second')
    cli.hidden.add(first)
    cli.handle('/resume')
    cli.palette.query.text = 'hidden original'
    assert first in [row.value for row in cli.palette.matches()]
    cli.palette.choose()
    assert cli.current == first and first not in cli.hidden


def test_delete_rejection_and_confirmation_are_bound_to_target(cli):
    first = cli.current
    cli.handle('/new other')
    second = cli.current
    cli.manager.delete_session = AsyncMock()
    assert cli.handle('/delete ' + first[:8])
    assert cli.current == first
    assert '回收' in cli.confirmation_text()
    assert '工作区' in cli.confirmation_text()
    cli.select(second)
    assert not cli.handle('/yes')
    cli.manager.delete_session.assert_not_called()
    cli.select(first)
    assert cli.handle('/no')
    assert cli.pending_delete is None


def test_last_session_delete_creates_one_replacement(cli):
    first = cli.current
    async def remove(session):
        assert session.id == first
        cli.manager.sessions.pop(first)
        return cli.store.root / '.trash' / first
    cli.manager.delete_session = AsyncMock(side_effect=remove)
    cli.active.record['permission_policy'] = 'full_access'
    assert cli.handle('/delete')
    cli.manager.delete_session.assert_not_called()
    assert cli.handle('/yes')
    assert len(cli.manager.sessions) == 1 and cli.current != first
    assert first not in cli._drafts
    assert cli.manager.delete_session.await_count == 1


def test_real_keyboard_tab_palette_and_escape_keep_draft(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                submitted = []
                cli.manager.submit = lambda session, text: submitted.append(text)
                running = asyncio.create_task(cli.run())
                try:
                    await asyncio.sleep(.05)
                    pipe.send_text('/pol')
                    await asyncio.sleep(.08)
                    pipe.send_bytes(b'\t')
                    await asyncio.sleep(.08)
                    assert cli.input.text == '/policy'
                    assert cli.app.layout.has_focus(cli.input)
                    pipe.send_bytes(b'\x15')  # Ctrl+U clears line.
                    pipe.send_text('unsent message')
                    pipe.send_bytes(b'\x10')  # Ctrl+P opens palette.
                    await asyncio.sleep(.08)
                    pipe.send_text('重命名')
                    await asyncio.sleep(.08)
                    pipe.send_bytes(b'\x1b')
                    await asyncio.sleep(.15)
                    assert not cli.palette.visible
                    assert cli.input.text == 'unsent message'
                    pipe.send_bytes(b'\x10')
                    await asyncio.sleep(.08)
                    pipe.send_text('重命名\r')
                    await asyncio.sleep(.08)
                    assert cli.input.text == '/rename '
                    pipe.send_text('changed\r')
                    await asyncio.sleep(.08)
                    assert cli.active.record['title'] == 'changed'
                    assert cli.input.text == 'unsent message'
                finally:
                    pipe.send_bytes(b'\x11')
                    await asyncio.wait_for(running, 3)
        finally:
            store.close()
    asyncio.run(run())


def test_palette_arrow_selection_and_prompt_submission_not_swallowed(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                prompts = []
                cli.manager.submit = lambda session, text: prompts.append(text)
                running = asyncio.create_task(cli.run())
                try:
                    await asyncio.sleep(.05)
                    pipe.send_bytes(b'\x10')
                    await asyncio.sleep(.06)
                    pipe.send_text('历史')
                    await asyncio.sleep(.06)
                    before = cli.palette.index
                    pipe.send_bytes(b'\x1b[B')
                    await asyncio.sleep(.06)
                    assert cli.palette.index != before
                    pipe.send_bytes(b'\x1b[A')
                    await asyncio.sleep(.06)
                    assert cli.palette.index == before
                    pipe.send_bytes(b'\x1b')
                    await asyncio.sleep(.12)
                    pipe.send_bytes(b'\x1b[200~hello\nworld\x1b[201~')
                    pipe.send_text('\r')
                    await asyncio.sleep(.1)
                    assert prompts == ['hello\nworld']
                    pipe.send_text('//slash\r')
                    await asyncio.sleep(.08)
                    assert prompts == ['hello\nworld', '/slash']
                finally:
                    pipe.send_bytes(b'\x11')
                    await asyncio.wait_for(running, 3)
        finally:
            store.close()
    asyncio.run(run())


def test_policy_aliases_and_selector_dont_expose_model_secrets(cli, monkeypatch):
    from ai_agent_startup import config
    monkeypatch.setattr(config, 'MODEL_PROFILES', {'private': {'api_key': 'very-secret-key'}})
    assert cli.handle('/policy full access')
    assert cli.active.record['permission_policy'] == 'full_access'
    assert 'Full Access' in cli.status_text()[0][1]
    assert cli.handle('/policy full')
    assert cli.handle('/policy smart')
    assert 'Smart' in cli.status_text()[0][1]
    assert cli.handle('/models')
    assert 'very-secret-key' not in str(cli.palette.rows())
    cli.palette.query.text = 'private'
    cli.palette.choose()
    assert cli.active.record['model_profile'] == 'private'
    assert cli.handle('/permissions')
    cli.palette.query.text = 'Read Only'
    cli.palette.choose()
    assert cli.active.record['permission_policy'] == 'readonly'


def test_async_delete_waits_ui_write_and_does_not_wait_for_itself(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                await cli.drain_render()
                identifier = cli.current
                gate = asyncio.Event()
                order = []
                async def writer():
                    await gate.wait()
                    order.append('write')
                job = asyncio.create_task(writer())
                cli._ui_jobs.add(job)
                job.add_done_callback(cli._ui_jobs.discard)
                async def delete(session):
                    order.append('delete')
                    cli.manager.sessions.pop(session.id)
                    return store.root / '.trash' / session.id
                cli.manager.delete_session = AsyncMock(side_effect=delete)
                assert cli.handle('/delete')
                assert cli.handle('/yes')
                await asyncio.sleep(.02)
                cli.manager.delete_session.assert_not_called()
                # Additional deletions cannot form a mutual _ui_jobs wait.
                cli.handle('/new another')
                assert not cli.handle('/delete')
                gate.set()
                await asyncio.wait_for(asyncio.gather(*list(cli._ui_jobs)), 2)
                assert order == ['write', 'delete']
                assert identifier not in cli.manager.sessions
                await cli.drain_render()
                await cli.manager.shutdown()
        finally:
            store.close()
    asyncio.run(run())


def test_actual_session_delete_preserves_workspace_and_export(cli):
    identifier = cli.current
    workspace = cli.store.workspace(identifier)
    (workspace / 'retained.txt').write_text('keep')
    cli.handle('/export json')
    exports = list((cli.store.root / 'exports').glob('*.json'))
    assert cli.handle('/delete')
    assert cli.handle('/yes')
    assert (cli.store.root / '.trash' / identifier / 'session.json').exists()
    assert (workspace / 'retained.txt').read_text() == 'keep'
    assert all(path.exists() for path in exports)
    assert identifier not in cli.manager.sessions


def test_confirmed_deletion_finishes_when_quit_follows_immediately(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                identifier = cli.current
                async def pending_ui_write():
                    await asyncio.sleep(.15)
                job = asyncio.create_task(pending_ui_write())
                cli._ui_jobs.add(job)
                job.add_done_callback(cli._ui_jobs.discard)
                running = asyncio.create_task(cli.run())
                await asyncio.sleep(.05)
                pipe.send_text('/delete\r/yes\r')
                pipe.send_bytes(b'\x11')
                await asyncio.wait_for(running, 3)
                assert (store.root / '.trash' / identifier / 'session.json').exists()
                assert identifier not in cli.manager.sessions
        finally:
            store.close()
    asyncio.run(run())


def test_busy_policy_changes_and_delete_are_rejected(cli):
    cli.active.forking = True
    cli.active.record['status'] = 'running'
    assert not cli.handle('/policy smart')
    assert not cli.handle('/delete')
    assert cli.pending_delete is None
    assert cli.handle('/permissions')
    cli.palette.query.text = 'Full Access'
    cli.palette.choose()
    assert cli.active.record.get('permission_policy') is None
    cli.active.forking = False


def test_session_switch_closes_palette_and_keeps_parameter_command_draft(cli):
    first = cli.current
    second = cli.manager.create('second').id
    cli.input.text = 'retained draft'
    cli.prepare_command('/rename ')
    cli.open_palette()
    cli.select(second)
    assert not cli.palette.visible
    cli.select(first)
    assert cli.input.text == 'retained draft'
