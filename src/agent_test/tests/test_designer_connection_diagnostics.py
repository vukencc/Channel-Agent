"""受控离线 HTTPX/SSE：真实 SDK 与 call_model，不执行真实供应商请求。"""

import asyncio
import json

import httpx
import openai
import pytest

from agent_core import config
from agent_core.core import llm
from agent_core.core.flow_designer import design_flow
from agent_core.core.model_settings import active_profile, active_reasoning_effort
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore


PRIVATE = 'controlled-private-secret-marker'
MESSAGES = [{'role': 'user', 'content': '设计一个流程'}]


def _frame(content=None, *, metadata=False):
    choices = [] if metadata else [{'index': 0, 'finish_reason': None,
                                   'delta': {'content': content}}]
    return ('data: ' + json.dumps({'id': PRIVATE, 'object': 'chat.completion.chunk',
            'created': 0, 'model': 'controlled-model', 'choices': choices}) + '\n\n').encode()


class ControlledSSE:
    """在 HTTP 握手后按指定边界断流，保留 SDK 的 SSE 解码路径。"""

    def __init__(self, frames):
        self.frames = frames
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.started.set()
        for frame in self.frames:
            if frame == 'block':
                await asyncio.Event().wait()
            elif isinstance(frame, BaseException):
                raise frame
            else:
                yield frame

    async def aclose(self):
        self.closed = True


@pytest.fixture
def transport_backend(request):
    if request.param == 'legacy-httpx':
        return httpx
    from openai import _base_client
    backend = getattr(_base_client, 'httpx2', None)
    if backend is None:
        pytest.skip('当前 SDK 未提供 httpx2 后端')
    return backend


@pytest.fixture
def controlled_sse(transport_backend):
    return type('ControlledBackendSSE', (ControlledSSE, transport_backend.AsyncByteStream), {})


@pytest.fixture
def controlled_designer(tmp_path, sandbox_env, monkeypatch, transport_backend):
    """每个请求使用独立离线客户端，全部状态与审计保存在临时目录。"""
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', 5)
    monkeypatch.setattr(config, 'MAX_RETRIES', 1)
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
    store = SessionStore(tmp_path / 'state', sandbox_env)
    manager = SessionManager(store, model=llm.call_model)
    profile = manager.model_registry.add(name='controlled', model='controlled-model',
        base_url=f'https://example.invalid/{PRIVATE}/v1', api_key=PRIVATE)['id']
    streams, requests, contexts = [], [], []

    async def handle(request):
        requests.append(json.loads(request.content))
        contexts.append((active_profile.get(), active_reasoning_effort.get()))
        return transport_backend.Response(200, headers={'content-type': 'text/event-stream'},
                              stream=streams.pop(0))

    def client(endpoint):
        return openai.AsyncOpenAI(api_key=PRIVATE, base_url=endpoint['base_url'],
            max_retries=0, http_client=transport_backend.AsyncClient(
                transport=transport_backend.MockTransport(handle)))

    monkeypatch.setattr(llm, 'endpoint_client', client)
    yield manager, profile, streams, requests, contexts
    store.close()


@pytest.mark.parametrize('transport_backend', ['legacy-httpx', 'sdk-httpx2'], indirect=True)
@pytest.mark.parametrize('policy', ['readonly', 'smart', 'full_access'])
@pytest.mark.parametrize('phase', ['connect', 'content', 'metadata'])
def test_designer_classifies_sse_disconnect_without_replay_or_private_echo(
        controlled_designer, controlled_sse, transport_backend, phase, policy, monkeypatch, capsys):
    manager, profile, streams, requests, contexts = controlled_designer
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    monkeypatch.setattr(config, 'TOOL_PERMISSION_POLICY', policy)
    manager.permission_override = policy
    prefix = [] if phase == 'connect' else [
        _frame(metadata=True) if phase == 'metadata' else
        _frame(json.dumps({'reply': PRIVATE, 'template': {'format': 'agent-flow',
            'version': 1, 'title': PRIVATE, 'nodes': [
                {'id': 'a', 'title': 'A', 'prompt': PRIVATE}], 'edges': []}}))]
    attempts = 2 if phase == 'connect' else 1
    failed_streams = [controlled_sse([*prefix,
        transport_backend.ReadError(f'private URL https://invalid/{PRIVATE}')]) for _ in range(attempts)]
    streams.extend(failed_streams)
    metrics = []
    original_model = manager.model

    async def observed_model(history, *, session_id, emit):
        def observe(kind, value):
            if kind == 'metrics':
                metrics.append(value)
            emit(kind, value)
        return await original_model(history, session_id=session_id, emit=observe)

    monkeypatch.setattr(manager, 'model', observed_model)

    with pytest.raises(ValueError) as caught:
        asyncio.run(design_flow(manager, MESSAGES, model_profile=profile, reasoning_effort='xhigh'))

    message = str(caught.value)
    if phase == 'connect':
        assert '首段响应前连接失败' in message
    else:
        assert '生成过程中连接中断' in message
        assert '未导入模板' in message
    assert PRIVATE not in message
    assert PRIVATE not in capsys.readouterr().out
    assert caught.value.__suppress_context__
    assert len(requests) == attempts
    assert contexts == [(profile, 'xhigh')] * attempts
    assert all(request['model'] == 'controlled-model' for request in requests)
    assert all(request['reasoning_effort'] == 'xhigh' for request in requests)
    assert all('tools' not in request for request in requests)
    assert all(stream.closed for stream in failed_streams)
    assert len(metrics) == 1
    assert metrics[0]['outcome'] == 'connection_error'
    assert metrics[0]['failure_phase'] == ('connect' if phase == 'connect' else 'stream')
    assert metrics[0]['transport_error_type'] == 'ReadError'
    assert metrics[0]['request_attempts'] == attempts
    assert PRIVATE not in json.dumps(metrics)
    assert config.TOOL_PERMISSION_POLICY == manager.permission_override == policy
    assert not manager.sessions
    assert not (manager.store.root / 'flows').exists()


@pytest.mark.parametrize('transport_backend', ['legacy-httpx', 'sdk-httpx2'], indirect=True)
@pytest.mark.parametrize('after_content', [False, True])
def test_designer_cancellation_closes_sse_without_retry_or_import(
        controlled_designer, controlled_sse, monkeypatch, after_content):
    manager, profile, streams, requests, _ = controlled_designer
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    stream = controlled_sse([*([_frame(PRIVATE)] if after_content else []), 'block'])
    streams.append(stream)

    async def run():
        task = asyncio.create_task(design_flow(manager, MESSAGES, model_profile=profile))
        await asyncio.wait_for(stream.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert stream.closed
    assert len(requests) == 1
    assert not (manager.store.root / 'flows').exists()
    assert not manager.sessions


@pytest.mark.parametrize('transport_backend', ['legacy-httpx', 'sdk-httpx2'], indirect=True)
@pytest.mark.parametrize('after_content', [False, True])
def test_designer_timeout_remains_timeout_without_retry_or_import(
        controlled_designer, controlled_sse, transport_backend, monkeypatch, after_content):
    manager, profile, streams, requests, _ = controlled_designer
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .2)
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    # MockTransport 不会计算 socket idle timeout。显式注入真实后端 ReadTimeout，
    # 防止把已移除的设计总时限当作网络空闲超时；首帧前仍保留两次重试。
    attempts = 1 if after_content else 2
    failed_streams = [controlled_sse([*([_frame(PRIVATE)] if after_content else []),
        transport_backend.ReadTimeout(f'private idle timeout {PRIVATE}')]) for _ in range(attempts)]
    streams.extend(failed_streams)

    with pytest.raises(ValueError, match='超时') as caught:
        # 三秒仅保护测试悬挂，并容纳生产重试的一秒退避；不充当被测模型时限。
        asyncio.run(asyncio.wait_for(design_flow(manager, MESSAGES, model_profile=profile), 3))

    assert '连接失败' not in str(caught.value)
    assert PRIVATE not in str(caught.value)
    assert caught.value.__suppress_context__
    assert all(stream.closed for stream in failed_streams)
    assert len(requests) == attempts
    assert not (manager.store.root / 'flows').exists()
    assert not manager.sessions
