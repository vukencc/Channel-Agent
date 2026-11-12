"""受控 HTTPX/SSE 服务夹具；不连接真实模型，不执行生成工具。"""
import asyncio
import json
import threading
import time

import httpx
import pytest

from agent_core import config
from agent_core.core import llm
from agent_core.core.budgets import BudgetLedger
from agent_core.core.flow_designer import design_flow
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


def _template():
    nodes = []
    for index in range(5):
        tools = [] if index == 0 else [{
            'name': f'controlled_tool_{index}', 'description': '读取受控输入',
            'parameters': {'type': 'object', 'properties': {}},
            'code': 'def main(arguments):\n    return arguments\n',
        }]
        nodes.append({'id': f'n{index}', 'title': f'角色{index}', 'prompt': f'完整角色{index}',
                      'reasoning_effort': 'xhigh', 'custom_tools': tools})
    return {'format': 'agent-flow', 'version': 1, 'title': '五节点四动态工具',
            'permission_policy': 'readonly', 'nodes': nodes,
            'edges': [{'source': f'n{i}', 'target': f'n{i + 1}'} for i in range(4)]}


class ControlledSSE(httpx.AsyncByteStream):
    def __init__(self, *, delay=.015, frames=12, failure=None, first_delay=0):
        self.delay, self.frames, self.failure = delay, frames, failure
        self.first_delay = first_delay
        self.closed = False
        self.started = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.sleep(self.first_delay)
        for _ in range(self.frames):
            await asyncio.sleep(self.delay)
            yield self._frame({'reasoning_content': '继续完整设计'})
        if self.failure:
            raise self.failure
        content = json.dumps({'reply': '设计完成', 'template': _template()}, ensure_ascii=False)
        yield self._frame({'content': content}, finish='stop')
        yield b'data: [DONE]\n\n'

    @staticmethod
    def _frame(delta, finish=None):
        payload = {'id': 'controlled-sse', 'object': 'chat.completion.chunk', 'created': 0,
                   'model': 'controlled-model', 'choices': [
                       {'index': 0, 'delta': delta, 'finish_reason': finish}]}
        return ('data: ' + json.dumps(payload) + '\n\n').encode()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def controlled_designer(tmp_path, monkeypatch, sandbox_env):
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .05)
    monkeypatch.setattr(config, 'SESSION_TIMEOUT', .5)
    for name in ('SESSION_COST_LIMIT', 'DAILY_COST_LIMIT', 'MODEL_REQUESTS_PER_MINUTE'):
        monkeypatch.setattr(config, name, 0)
    store = SessionStore(tmp_path / 'state', sandbox_env)
    manager = SessionManager(store, model=llm.call_model)
    manager.budget_ledger = BudgetLedger(store.root)
    profile = manager.model_registry.add(name='受控模型', model='controlled-model',
                                         base_url='https://controlled.invalid/v1', api_key='fixture-key')['id']
    requests, streams, clients = [], [], []

    def install(factory=ControlledSSE):
        def handle(request):
            requests.append(json.loads(request.content))
            stream = factory()
            streams.append(stream)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)

        def client(endpoint):
            sdk = llm.AsyncOpenAI(api_key='fixture-key', base_url='https://controlled.invalid/v1',
                                  max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
            clients.append(sdk)
            return sdk
        monkeypatch.setattr(llm, 'endpoint_client', client)

    install()
    yield manager, profile, requests, streams, clients, install
    store.close()


@pytest.mark.parametrize('policy', ['readonly', 'smart', 'full_access'])
@pytest.mark.parametrize('first_delay', [0, .08], ids=['streaming', 'slow-first-frame'])
def test_designer_live_sse_survives_classic_deadline_without_simplifying(
        controlled_designer, monkeypatch, policy, first_delay):
    manager, profile, requests, streams, clients, install = controlled_designer
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    manager.permission_override = policy
    install(lambda: ControlledSSE(first_delay=first_delay))
    messages = [{'role': 'user', 'content': '保留完整五节点、四动态工具及全部业务规则'}]
    started = time.monotonic()
    result = asyncio.run(design_flow(manager, messages, model_profile=profile, reasoning_effort='xhigh'))
    assert time.monotonic() - started > config.MODEL_CALL_TIMEOUT
    assert config.MODEL_CALL_TIMEOUT == .05
    assert requests[0]['messages'][1:] == messages
    assert requests[0]['model'] == 'controlled-model'
    assert requests[0]['reasoning_effort'] == 'xhigh'
    assert 'tools' not in requests[0]
    assert len(result['template']['nodes']) == 5
    assert sum(len(n['custom_tools']) for n in result['template']['nodes']) == 4
    assert all(n['reasoning_effort'] == 'xhigh' for n in result['template']['nodes'])
    assert result['template']['permission_policy'] == 'readonly'
    assert config.TOOL_PERMISSION_POLICY == manager.permission_override == policy
    assert [n['prompt'] for n in result['template']['nodes']] == [f'完整角色{i}' for i in range(5)]
    assert streams[0].closed and clients[0].is_closed()
    assert not (manager.store.root / 'flows').exists()


def test_classic_live_sse_still_times_out_concurrently_and_after_design(controlled_designer):
    from agent_core.core.model_settings import model_profile
    from agent_core.core.tool_settings import model_tools

    manager, profile, requests, streams, clients, _ = controlled_designer

    async def classic():
        with model_profile(profile, manager.model_registry.resolve(profile)), model_tools([]):
            with pytest.raises(TimeoutError, match='总时限'):
                await llm.call_model([{'role': 'user', 'content': '普通请求'}], emit=lambda *a: None)

    async def run():
        design = asyncio.create_task(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                                 model_profile=profile, reasoning_effort='xhigh'))
        await classic()
        assert not design.done()
        assert (await design)['template']['title'] == '五节点四动态工具'
        await classic()

    asyncio.run(run())
    assert len(requests) == 3
    assert all(stream.closed for stream in streams)
    assert all(client.is_closed() for client in clients)
    assert config.MODEL_CALL_TIMEOUT == .05


def test_designer_stop_closes_live_sdk_stream_and_drains_slot(controlled_designer):
    manager, profile, requests, streams, clients, install = controlled_designer
    install(lambda: ControlledSSE(frames=1000))

    async def run():
        manager.slots = asyncio.Semaphore(1)
        task = asyncio.create_task(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                               model_profile=profile, reasoning_effort='xhigh'))
        async with asyncio.timeout(2):
            while not streams:
                await asyncio.sleep(.001)
            await streams[0].started.wait()
            await asyncio.sleep(.08)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert manager.slots._value == 1
        assert manager.budget_ledger.state['pending'] == {}

    asyncio.run(run())
    assert len(requests) == 1
    assert streams[0].closed and clients[0].is_closed()
    assert not (manager.store.root / 'flows').exists()


def test_designer_slot_queue_has_no_total_deadline_and_remains_cancellable(controlled_designer):
    manager, profile, requests, streams, clients, _ = controlled_designer

    async def run():
        manager.slots = asyncio.Semaphore(1)
        await manager.slots.acquire()
        task = asyncio.create_task(design_flow(manager, [{'role': 'user', 'content': '排队完整设计'}],
                                               model_profile=profile, reasoning_effort='xhigh'))
        try:
            await asyncio.sleep(.08)
            assert not task.done()
            assert not requests and not clients
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not manager.slots._waiters
        finally:
            manager.slots.release()
        assert manager.slots._value == 1

    asyncio.run(run())
    assert not (manager.store.root / 'flows').exists()


def test_designer_cancel_waits_for_client_initialization_thread(controlled_designer, monkeypatch):
    manager, profile, requests, streams, clients, _ = controlled_designer
    original = llm.endpoint_client
    entered, release = threading.Event(), threading.Event()

    def blocked(endpoint):
        entered.set()
        if not release.wait(2):
            raise AssertionError('受控客户端初始化未获释放')
        return original(endpoint)

    monkeypatch.setattr(llm, 'endpoint_client', blocked)

    async def run():
        manager.slots = asyncio.Semaphore(1)
        task = asyncio.create_task(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                               model_profile=profile, reasoning_effort='xhigh'))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(.001)
                task.cancel()
                await asyncio.sleep(.02)
                assert not task.done()
                assert manager.slots.locked()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert manager.slots._value == 1

    asyncio.run(run())
    assert not requests and not streams
    assert len(clients) == 1 and clients[0].is_closed()
    assert not (manager.store.root / 'flows').exists()


def test_designer_idle_transport_timeout_still_fails_without_partial_import(controlled_designer):
    manager, profile, requests, streams, clients, install = controlled_designer
    # MockTransport 不执行 socket 计时；明确注入底层真实 HTTPX 超时类型，SDK 原样消费 SSE。
    install(lambda: ControlledSSE(frames=5, failure=httpx.ReadTimeout('controlled idle timeout')))
    with pytest.raises(ValueError, match='超时'):
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                model_profile=profile, reasoning_effort='xhigh'))
    assert len(requests) == 1
    assert streams[0].closed and clients[0].is_closed()
    assert manager.budget_ledger.state['pending'] == {}
    assert not (manager.store.root / 'flows').exists()


def test_designer_output_character_cap_is_not_disabled(controlled_designer, monkeypatch):
    manager, profile, requests, streams, clients, install = controlled_designer
    monkeypatch.setattr(config, 'MODEL_OUTPUT_CHARS', 20)
    install(lambda: ControlledSSE(frames=12))
    with pytest.raises(ValueError, match='未返回完整可用内容'):
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                model_profile=profile, reasoning_effort='xhigh'))
    assert streams[0].closed and clients[0].is_closed()
    assert manager.budget_ledger.state['pending'] == {}
    assert not (manager.store.root / 'flows').exists()


def test_designer_budget_still_blocks_unknown_price_before_http_request(controlled_designer, monkeypatch):
    manager, profile, requests, streams, clients, _ = controlled_designer
    monkeypatch.setattr(config, 'SESSION_COST_LIMIT', .01)
    with pytest.raises(ValueError, match='预算'):
        asyncio.run(design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                model_profile=profile, reasoning_effort='xhigh'))
    assert not requests and not streams
    assert clients and all(client.is_closed() for client in clients)
    assert manager.budget_ledger.state['pending'] == {}
    assert not (manager.store.root / 'flows').exists()


@pytest.mark.parametrize('backend_name', ['sdk-httpx2', 'legacy-httpx'])
def test_designer_real_local_socket_idle_timeout_closes_without_partial_import(
        controlled_designer, monkeypatch, backend_name):
    """真实 localhost TCP 空闲计时；服务内容受控，不调用真实供应商。"""
    from openai import _base_client

    backend = httpx if backend_name == 'legacy-httpx' else getattr(_base_client, 'httpx2', None)
    if backend is None:
        pytest.skip('当前 SDK 未提供 httpx2 后端')
    manager, profile, _, _, _, _ = controlled_designer
    monkeypatch.setattr(config, 'SESSION_TIMEOUT', .15)
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    clients, requests, metrics = [], [], []
    original_model = manager.model

    async def observed_model(history, *, session_id, emit):
        def observe(kind, value):
            if kind == 'metrics':
                metrics.append(value)
            emit(kind, value)
        return await original_model(history, session_id=session_id, emit=observe)

    monkeypatch.setattr(manager, 'model', observed_model)

    async def run():
        disconnected = asyncio.Event()
        handlers = set()
        first_sent = []

        async def handle(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next(int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                              if line.lower().startswith(b'content-length:'))
                requests.append(json.loads(await reader.readexactly(length)))
                # 完整 JSON 也不能因连接中断而被当作已完整结束的模板。
                partial = json.dumps({'reply': 'controlled-partial', 'template': _template()})
                frame = ControlledSSE._frame({'content': partial})
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                             b'Transfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n')
                writer.write(f'{len(frame):x}\r\n'.encode() + frame + b'\r\n')
                await writer.drain()
                first_sent.append(time.monotonic())
                # 不发送结束帧或任何新字节；真实 HTTP 客户端负责计算读取空闲时限。
                assert await reader.read() == b''
                disconnected.set()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

        def client(endpoint):
            sdk = llm.AsyncOpenAI(api_key='local-fixture-key',
                base_url=f'http://127.0.0.1:{port}/v1', max_retries=0,
                http_client=backend.AsyncClient(trust_env=False))
            clients.append(sdk)
            return sdk

        monkeypatch.setattr(llm, 'endpoint_client', client)
        try:
            async with asyncio.timeout(3):
                with pytest.raises(ValueError, match='超时') as caught:
                    await design_flow(manager, [{'role': 'user', 'content': '完整设计'}],
                                      model_profile=profile, reasoning_effort='xhigh')
                assert 'controlled-partial' not in str(caught.value)
                await disconnected.wait()
            assert len(first_sent) == 1
            assert time.monotonic() - first_sent[0] >= config.SESSION_TIMEOUT
        finally:
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*handlers), 2)

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0]['reasoning_effort'] == 'xhigh'
    assert clients and all(client.is_closed() for client in clients)
    assert len(metrics) == 1 and metrics[0]['outcome'] == 'timeout'
    assert metrics[0]['request_attempts'] == 1
    assert manager.budget_ledger.state['pending'] == {}
    assert not manager.sessions
    assert not (manager.store.root / 'flows').exists()
