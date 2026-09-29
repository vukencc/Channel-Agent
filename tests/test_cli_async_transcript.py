"""Interactive rendering and draft preservation under long session history."""

import asyncio
import threading
import time

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from ai_agent_startup.core.cli import AgentCLI
from ai_agent_startup.core.storage import LazyRecord, SessionStore
from ai_agent_startup.core.transcript import Transcript


def test_busy_enter_keeps_draft_and_switch_restores_each_draft(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            first = cli.current
            second = cli.manager.create('second').id
            cli.active.record['status'] = 'running'
            cli.active.forking = True
            cli.input.text = 'unfinished first draft'
            cli.handle(cli.input.text)
            assert cli.input.text == 'unfinished first draft'
            cli.select(second)
            assert cli.input.text == ''
            cli.input.text = 'second draft'
            cli.select(first)
            assert cli.input.text == 'unfinished first draft'
            cli.select(second)
            assert cli.input.text == 'second draft'
    finally:
        store.close()


def test_async_render_loads_lazy_history_off_event_loop_and_keeps_latest_view(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        record = store.create('long', 'system')
        record['messages'] += [{'role': 'user', 'content': f'old-{index}'} for index in range(5000)]
        record['messages'].append({'role': 'assistant', 'content': 'last-marker'})
        store.save(record)
        loop_thread = threading.get_ident()
        materialized_on = []
        original_materialize = LazyRecord.materialize
        original_sync = Transcript.sync

        def checked_materialize(self):
            materialized_on.append(threading.get_ident())
            assert threading.get_ident() != loop_thread
            return original_materialize(self)

        def delayed_sync(self, messages):
            time.sleep(.06)
            return original_sync(self, messages)

        monkeypatch.setattr(LazyRecord, 'materialize', checked_materialize)
        monkeypatch.setattr(Transcript, 'sync', delayed_sync)
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                start = time.monotonic()
                assert '5002 条' in cli.status_text()[0][1]
                cli.handle('/top')
                assert time.monotonic() - start < .04
                await cli.drain_render()
                assert materialized_on and all(thread != loop_thread for thread in materialized_on)
                assert 'old-0' in cli.chat.text
                cli.handle('/bottom')
                await cli.drain_render()
                assert 'last-marker' in cli.chat.text
                assert 'old-0' in '\n'.join(cli.transcript.lines)
                await cli.manager.shutdown()
        finally:
            store.close()

    asyncio.run(run())


def test_status_names_queue_thinking_answer_tool_confirmation_and_error(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            session = cli.active
            for status, phase, expected in (
                ('queued', '', '排队'),
                ('running', '模型正在思考', '思考'),
                ('running', '模型正在回答', '回答'),
                ('running', '执行工具：read_file', '执行工具'),
                ('confirming', '', '待确认'),
                ('stopping', '', '停止'),
                ('error', '', '错误'),
            ):
                session.record['status'] = status
                session.phase = phase
                assert expected in cli.status_text()[0][1]
    finally:
        store.close()


def test_switch_during_render_discards_stale_page_and_coalesces_workers(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        first = store.create('first', 'system')
        first['messages'].append({'role': 'user', 'content': 'first marker'})
        store.save(first)
        second = store.create('second', 'system')
        second['messages'].append({'role': 'user', 'content': 'second marker'})
        store.save(second)
        active = 0
        peak = 0
        original_sync = Transcript.sync

        def slow_sync(self, messages):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                time.sleep(.04)
                return original_sync(self, messages)
            finally:
                active -= 1

        monkeypatch.setattr(Transcript, 'sync', slow_sync)
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                cli.select(first['id'])
                cli.select(second['id'])
                await cli.drain_render()
                assert peak == 1
                assert 'second marker' in cli.chat.text
                assert 'first marker' not in cli.chat.text
                await cli.manager.shutdown()
        finally:
            store.close()

    asyncio.run(run())


def test_stream_refresh_formats_existing_message_only_once(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                await cli.drain_render()
                session = cli.active
                session.record['messages'].append({'role': 'user', 'content': 'oldmarker'})
                original = Transcript.text_lines
                formatted = []

                def count_lines(cls, value):
                    if value == 'oldmarker':
                        formatted.append(value)
                    return original(value)

                monkeypatch.setattr(Transcript, 'text_lines', classmethod(count_lines))
                cli.render(force=True)
                await cli.drain_render()
                assert len(formatted) == 1
                session.partial = 'new streaming token'
                cli.render(force=True)
                await cli.drain_render()
                assert len(formatted) == 1
                assert 'new streaming token' in cli.chat.text
                await cli.manager.shutdown()
        finally:
            store.close()

    asyncio.run(run())


def test_loading_history_rejects_send_and_rename_without_blocking_switch(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        record = store.create('stored', 'system')
        record['messages'].append({'role': 'user', 'content': 'stored marker'})
        store.save(record)
        entered = threading.Event()
        release = threading.Event()
        main_thread = threading.get_ident()
        original = LazyRecord.materialize

        def gated_materialize(self):
            assert threading.get_ident() != main_thread
            entered.set()
            assert release.wait(2)
            return original(self)

        monkeypatch.setattr(LazyRecord, 'materialize', gated_materialize)
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                await asyncio.wait_for(asyncio.to_thread(entered.wait), 2)
                cli.input.text = 'unsent draft'
                start = time.monotonic()
                assert cli.handle('unsent draft') is False
                assert cli.handle('/rename changed') is False
                assert time.monotonic() - start < .05
                assert cli.input.text == 'unsent draft'
                assert cli.active.record['title'] == 'stored'
                assert cli.handle('/new while loading') is True
                assert cli.active.record['title'] == 'while loading'
                assert cli.handle('/switch ' + record['id'][:8]) is True
                assert cli.input.text == 'unsent draft'
                release.set()
                await cli.drain_render()
                await cli.manager.shutdown()
        finally:
            release.set()
            store.close()

    asyncio.run(run())


def test_switch_hides_previous_session_body_until_new_page_ready(tmp_path, monkeypatch):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        first = store.create('first', 'system')
        first['messages'].append({'role': 'user', 'content': 'old session body'})
        store.save(first)
        second = store.create('second', 'system')
        second['messages'].append({'role': 'user', 'content': 'new session body'})
        store.save(second)
        original_sync = Transcript.sync

        def slow_sync(self, messages):
            time.sleep(.04)
            return original_sync(self, messages)

        monkeypatch.setattr(Transcript, 'sync', slow_sync)
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                await cli.drain_render()
                assert 'new session body' in cli.chat.text
                cli.select(first['id'])
                assert 'new session body' not in cli.chat.text
                assert '加载' in cli.chat.text
                await cli.drain_render()
                assert 'old session body' in cli.chat.text
                await cli.manager.shutdown()
        finally:
            store.close()

    asyncio.run(run())


def test_pipe_enter_keeps_busy_session_draft(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state', tmp_path / 'work')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                session = cli.active
                session.forking = True
                session.record['status'] = 'running'
                running = asyncio.create_task(cli.run())
                try:
                    await asyncio.sleep(.05)
                    pipe.send_text('busy draft\r')
                    await asyncio.sleep(.12)
                    assert cli.input.text == 'busy draft'
                    assert '正在运行' in cli.notice
                finally:
                    pipe.send_bytes(b'\x11')
                    await asyncio.wait_for(running, 2)
        finally:
            store.close()

    asyncio.run(run())


def test_status_styles_distinguish_work_and_error(tmp_path):
    store = SessionStore(tmp_path / 'state', tmp_path / 'work')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            session = cli.active
            styles = []
            for status, phase in (('queued', ''), ('running', '模型正在回答'),
                                  ('confirming', ''), ('error', '')):
                session.record['status'] = status
                session.phase = phase
                styles.append(cli.status_text()[0][0])
            assert len(set(styles)) == len(styles)
    finally:
        store.close()
