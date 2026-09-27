import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from core.cli import AgentCLI
from core.storage import SessionStore


def test_cli_commands_export_memory_and_restore(tmp_path):
    store = SessionStore(tmp_path / 'state')
    try:
        with create_pipe_input() as pipe:
            cli = AgentCLI(store, input=pipe, output=DummyOutput())
            first = cli.current
            cli.handle('/remember 使用中文')
            cli.handle('/new 第二会话')
            assert cli.current != first
            assert not store.memory(cli.current)
            cli.handle('/switch ' + first[:8])
            cli.handle('/memory')
            assert '使用中文' in cli.notice
            cli.handle('/export json')
            assert list((store.root / 'exports').glob('*.json'))
            cli.handle('/forget')
            cli.handle('/no')
            assert '使用中文' in store.memory(first)
            cli.handle('/forget')
            cli.handle('/yes')
            assert not store.memory(first)
    finally:
        store.close()


def test_fullscreen_cli_accepts_keyboard_and_exits(tmp_path):
    async def run():
        store = SessionStore(tmp_path / 'state')
        try:
            with create_pipe_input() as pipe:
                cli = AgentCLI(store, input=pipe, output=DummyOutput())
                async def keys():
                    await asyncio.sleep(0.1)
                    pipe.send_text('/new 键盘会话\r')
                    await asyncio.sleep(0.1)
                    pipe.send_bytes(b'\x11')
                sender = asyncio.create_task(keys())
                await asyncio.wait_for(cli.run(), 3)
                await sender
                assert any(s.record['title'] == '键盘会话' for s in cli.manager.sessions.values())
        finally:
            store.close()
    asyncio.run(run())
