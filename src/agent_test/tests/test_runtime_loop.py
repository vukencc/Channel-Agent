"""Frontend 自有事件循环；受控真实 socket 唤醒，不运行模型或业务回放。"""
import asyncio
import importlib
import socket
import sys
import threading
import time

import pytest


def _runtime():
    return importlib.import_module('agent_frontend.runtime_loop')


def _timer_probe(loop, *, iterations=24):
    incoming, outgoing = socket.socketpair()
    incoming.setblocking(False)
    stopped = threading.Event()

    def wake_io():
        while not stopped.wait(.001):
            try:
                outgoing.send(b'x')
            except OSError:
                break

    worker = threading.Thread(target=wake_io, daemon=True)

    async def receive():
        while True:
            await loop.sock_recv(incoming, 4096)

    async def measure():
        receiver = asyncio.create_task(receive())
        elapsed = []
        try:
            for _ in range(iterations):
                start = time.perf_counter_ns()
                await asyncio.sleep(.01)
                elapsed.append(time.perf_counter_ns() - start)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        assert min(elapsed) >= 10_000_000, f'10ms 定时器提前触发；最短 {min(elapsed)}ns'
        return elapsed

    worker.start()
    try:
        return loop.run_until_complete(measure())
    finally:
        stopped.set()
        worker.join(timeout=1)
        incoming.close()
        outgoing.close()
        assert not worker.is_alive()


def test_runtime_loop_timers_do_not_fire_before_requested_delay_with_real_io():
    loop = _runtime().new_runtime_loop()
    try:
        assert _timer_probe(loop)
    finally:
        loop.close()
    assert loop.is_closed()


def test_runtime_loop_cancellation_deadline_and_timeout_are_not_early():
    async def run():
        loop = asyncio.get_running_loop()
        never = asyncio.Event()
        task = asyncio.create_task(never.wait())
        started = time.perf_counter_ns()
        loop.call_later(.02, task.cancel)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.perf_counter_ns() - started >= 20_000_000
        started = time.perf_counter_ns()
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(.02):
                await never.wait()
        assert time.perf_counter_ns() - started >= 20_000_000
    _runtime().run_runtime(run())


def test_runtime_runner_closes_owned_loop_without_changing_global_policy():
    runtime = _runtime()
    policy = asyncio.get_event_loop_policy()
    current = policy._local.__dict__.copy() if hasattr(policy, '_local') else None
    seen = []
    async def run():
        seen.append(asyncio.get_running_loop())
        return '受控完成'
    assert runtime.run_runtime(run()) == '受控完成'
    assert seen[0].is_closed()
    assert asyncio.get_event_loop_policy() is policy
    if current is not None:
        assert policy._local.__dict__ == current


def test_runtime_runner_closes_after_exception():
    seen = []
    async def run():
        seen.append(asyncio.get_running_loop())
        raise ValueError('受控失败')
    with pytest.raises(ValueError, match='受控失败'):
        _runtime().run_runtime(run())
    assert seen[0].is_closed()

@pytest.mark.parametrize('scenario', ['linux', 'precise_windows', 'custom_policy', 'custom_factory', 'coarse_windows'])
def test_factory_selection_with_controlled_platform_doubles(monkeypatch, scenario):
    from types import SimpleNamespace
    runtime = _runtime()

    class NormalLoop:
        _clock_resolution = .015625
        def time(self):
            return 100.0

    class StandardPolicy:
        _loop_factory = NormalLoop
        def new_event_loop(self):
            return self._loop_factory()

    class CustomPolicy(StandardPolicy):
        pass

    policy = CustomPolicy() if scenario == 'custom_policy' else StandardPolicy()
    if scenario == 'custom_factory':
        policy._loop_factory = lambda: NormalLoop()
    original_factory = policy._loop_factory
    fake_asyncio = SimpleNamespace(WindowsProactorEventLoopPolicy=StandardPolicy,
                                   ProactorEventLoop=NormalLoop,
                                   get_event_loop_policy=lambda: policy,
                                   new_event_loop=policy.new_event_loop)
    precise = scenario == 'precise_windows'
    def clock_info(name):
        return SimpleNamespace(resolution=1e-7 if name == 'perf_counter' or precise else .015625)
    monkeypatch.setattr(runtime, 'asyncio', fake_asyncio)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='linux' if scenario == 'linux' else 'win32'))
    monkeypatch.setattr(runtime, 'time', SimpleNamespace(get_clock_info=clock_info, perf_counter=lambda: 200.0))
    loop = runtime.new_runtime_loop()
    if scenario == 'coarse_windows':
        assert type(loop) is not NormalLoop and isinstance(loop, NormalLoop)
        assert loop._clock_resolution == 1e-7 and loop.time() == 200.0
    else:
        assert type(loop) is NormalLoop
        assert loop._clock_resolution == .015625 and loop.time() == 100.0
    assert policy._loop_factory is original_factory


def test_runtime_runner_rejects_nested_active_loop_without_leaking_coroutine():
    async def active():
        coroutine = asyncio.sleep(0)
        try:
            with pytest.raises(RuntimeError, match='running event loop'):
                _runtime().run_runtime(coroutine)
        finally:
            coroutine.close()
    asyncio.run(active())


def test_desktop_entry_uses_owned_runtime_and_cleans_up(tmp_path, monkeypatch):
    pytest.importorskip('PySide6')
    from agent_frontend.desktop import runtime as desktop
    seen = []
    async def serve(self):
        seen.append(asyncio.get_running_loop())
    monkeypatch.setattr(desktop.RuntimeBridge, '_serve', serve)
    bridge = desktop.RuntimeBridge(tmp_path / 'state', workspace_root=tmp_path / 'workspace')
    bridge.is_running = True
    bridge._run()
    assert len(seen) == 1 and seen[0].is_closed()
    assert bridge._loop is None and bridge.is_running is False
    if _runtime().use_high_resolution_clock():
        assert type(seen[0]).__name__ == 'HighResolutionLoop'


def test_cli_entry_runs_controlled_frontend_on_owned_loop(tmp_path, monkeypatch):
    from agent_frontend.cli import app as cli
    seen, stores = [], []

    class ControlledCLI:
        """仅验证入口生命周期，不读取终端、不执行模型或生成业务结果。"""
        def __init__(self, store):
            stores.append(store)
        async def run(self):
            seen.append(asyncio.get_running_loop())

    monkeypatch.setattr(cli, 'AgentCLI', ControlledCLI)
    monkeypatch.setattr(cli, 'configure_debug_tools', lambda: None)
    monkeypatch.setattr(cli.config, 'validate_runtime_config', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['ai-agent-startup', '--state-dir', str(tmp_path / 'cli-state')])
    cli.main()
    assert len(seen) == 1 and seen[0].is_closed()
    assert stores[0].writer._shutdown
    if _runtime().use_high_resolution_clock():
        assert type(seen[0]).__name__ == 'HighResolutionLoop'


@pytest.mark.parametrize('mode', ['standard', 'legacy_owned', 'modern_factory', 'startup_failure'])
def test_web_entry_preserves_server_options_and_startup_failure(tmp_path, monkeypatch, mode):
    from types import SimpleNamespace
    from agent_core import config
    from agent_frontend.web import server
    runtime = _runtime()
    seen, calls = [], []
    app = object()

    class Config:
        def __init__(self, value, **options):
            assert value is app
            self.options = options
    if mode == 'modern_factory':
        Config.get_loop_factory = lambda self: None

    class Server:
        started = False
        def __init__(self, config):
            calls.append(config.options)
        async def serve(self):
            seen.append(asyncio.get_running_loop())
            self.started = mode != 'startup_failure'

    def uvicorn_run(value, **options):
        assert value is app
        calls.append(options)

    monkeypatch.setitem(sys.modules, 'uvicorn', SimpleNamespace(Config=Config, Server=Server, run=uvicorn_run))
    monkeypatch.setitem(sys.modules, 'agent_frontend.web.app', SimpleNamespace(create_app=lambda **kwargs: app))
    monkeypatch.setattr(runtime, 'use_high_resolution_clock', lambda: mode != 'standard')
    monkeypatch.setattr(config, 'validate_runtime_config', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['ai-agent-web', '--host', '127.0.0.1', '--port', '8765', '--state-dir', str(tmp_path / 'web-state')])
    if mode == 'startup_failure':
        with pytest.raises(SystemExit) as failure:
            server.main()
        assert failure.value.code == 3
    else:
        server.main()
    expected = dict(host='127.0.0.1', port=8765, workers=1, access_log=False,
                    proxy_headers=False, timeout_graceful_shutdown=30)
    if mode == 'modern_factory':
        expected['loop'] = 'agent_frontend.runtime_loop:new_runtime_loop'
    assert calls == [expected]
    if mode in ('legacy_owned', 'startup_failure'):
        assert len(seen) == 1 and seen[0].is_closed()
    else:
        assert not seen
