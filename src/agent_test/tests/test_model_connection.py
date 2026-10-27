"""Connection failures around streamed model replies and session checkpoints."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent_core import config
from agent_core.core import llm
from agent_core.core.sessions import SessionManager
from agent_core.core.storage import SessionStore
from agent_core.tools import TOOL_REGISTRY


class Stream:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = next(self.frames, None)
        if frame is None:
            raise StopAsyncIteration
        if isinstance(frame, BaseException):
            raise frame
        return frame

    async def close(self):
        self.closed = True


class BlockingStream:
    def __init__(self):
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError('unreachable')

    async def close(self):
        self.closed = True


def _chunk(*, content=None, tool_args=None, finish=None):
    calls = None
    if tool_args is not None:
        calls = [SimpleNamespace(index=0, id='pending-write', function=SimpleNamespace(
            name='create_file', arguments=tool_args))]
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        delta=SimpleNamespace(content=content, reasoning_content=None, tool_calls=calls))])


@pytest.fixture
def fake_transport(monkeypatch):
    """Route every SDK request to queued streams without network access."""
    monkeypatch.setattr(config, 'MAX_RETRIES', 2)
    monkeypatch.setattr(config, 'MODEL_FALLBACKS', [])
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', 5)
    queued = []
    requests = []

    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def with_options(self, **kwargs):
            return self

        async def create(self, **kwargs):
            requests.append(kwargs)
            return queued.pop(0)

    monkeypatch.setattr(llm, 'endpoint_client', lambda endpoint: Client())
    return queued, requests


def test_read_error_before_first_frame_retries_and_closes_failed_stream(fake_transport):
    queued, requests = fake_transport
    failed = Stream([httpx.ReadError('connection dropped')])
    succeeded = Stream([_chunk(content='恢复成功', finish='stop')])
    queued.extend([failed, succeeded])
    events = []

    answer = asyncio.run(llm.call_model([{'role': 'user', 'content': 'hello'}],
        emit=lambda *event: events.append(event)))

    assert answer['content'] == '恢复成功'
    assert len(requests) == 2
    assert failed.closed and succeeded.closed
    assert [event for event in events if event[0] == 'content'] == [('content', '恢复成功')]
    metrics = events[-1][1]
    assert metrics['outcome'] == 'ok'
    assert metrics['request_attempts'] == 2


def test_cancel_before_first_frame_closes_stream_without_retry(fake_transport):
    queued, requests = fake_transport
    blocked = BlockingStream()
    queued.append(blocked)

    async def run():
        task = asyncio.create_task(llm.call_model([{'role': 'user', 'content': 'hello'}],
            emit=lambda *_: None))
        await asyncio.wait_for(blocked.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert len(requests) == 1
    assert blocked.closed


def test_timeout_before_first_frame_uses_total_deadline_and_closes_stream(
        fake_transport, monkeypatch):
    queued, requests = fake_transport
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .05)
    blocked = BlockingStream()
    queued.append(blocked)
    events = []
    started = time.monotonic()

    with pytest.raises(TimeoutError, match='总时限'):
        asyncio.run(llm.call_model([{'role': 'user', 'content': 'hello'}],
            emit=lambda *event: events.append(event)))

    assert time.monotonic() - started < .3
    assert len(requests) == 1
    assert blocked.closed
    assert events[-1][1]['outcome'] == 'timeout'


def test_priming_retry_settles_each_sdk_request_once(fake_transport, monkeypatch):
    queued, requests = fake_transport
    failed = Stream([httpx.ReadError('connection dropped')])
    succeeded = Stream([_chunk(content='完成', finish='stop')])
    queued.extend([failed, succeeded])
    reserved = []
    settled = []

    async def reserve(amount):
        ticket = object()
        reserved.append(ticket)
        return ticket

    async def settle(ticket, actual):
        settled.append((ticket, actual))

    monkeypatch.setattr(llm, 'reserve_request', reserve)
    monkeypatch.setattr(llm, 'settle_request', settle)

    answer = asyncio.run(llm.call_model([{'role': 'user', 'content': 'hello'}],
        emit=lambda *_: None))

    assert answer['content'] == '完成'
    assert len(requests) == len(reserved) == len(settled) == 2
    assert [ticket for ticket, _ in settled] == reserved
    assert all(ticket is not None for ticket in reserved)
    assert failed.closed and succeeded.closed


def test_read_error_after_first_frame_never_retries_or_returns_partial_tool(fake_transport):
    queued, requests = fake_transport
    started = Stream([_chunk(content='部分正文', tool_args='{"path":"a.txt"'),
        httpx.ReadError('stream dropped after output')])
    queued.append(started)
    events = []

    with pytest.raises(llm.ModelConnectionError):
        asyncio.run(llm.call_model([{'role': 'user', 'content': 'write'}],
            emit=lambda *event: events.append(event)))

    assert len(requests) == 1
    assert started.closed
    assert ('content', '部分正文') in events
    assert all(kind != 'tool_calls' for kind, _ in events)
    assert events[-1][1]['request_attempts'] == 1
    assert events[-1][1]['outcome'] == 'connection_error'


def test_metadata_frame_counts_as_started_stream_and_disables_retry(fake_transport):
    queued, requests = fake_transport
    stream = Stream([SimpleNamespace(choices=[], usage=None),
        httpx.ReadError('connection dropped after metadata')])
    queued.append(stream)
    events = []

    with pytest.raises(llm.ModelConnectionError):
        asyncio.run(llm.call_model([{'role': 'user', 'content': 'hello'}],
            emit=lambda *event: events.append(event)))

    assert len(requests) == 1
    assert stream.closed
    assert events[-1][1]['request_attempts'] == 1
    assert events[-1][1]['outcome'] == 'connection_error'


def test_exhausted_api_connection_errors_keep_transport_class_and_safe_message(fake_transport, caplog, capsys):
    queued, requests = fake_transport
    secret = 'secret-token-should-never-appear'
    request = httpx.Request('POST', f'https://example.test/{secret}/chat/completions')

    def failure():
        error = openai.APIConnectionError(request=request)
        error.__cause__ = httpx.ReadError(f'disconnected from {request.url}', request=request)
        return error

    streams = [Stream([failure()]), Stream([failure()])]
    queued.extend(streams)
    events = []

    with pytest.raises(llm.ModelConnectionError) as caught:
        asyncio.run(llm.call_model([{'role': 'user', 'content': 'hello'}],
            emit=lambda *event: events.append(event)))

    metrics = events[-1][1]
    assert len(requests) == 2
    assert all(stream.closed for stream in streams)
    assert metrics['outcome'] == 'connection_error'
    assert metrics['error_type'] == 'APIConnectionError'
    assert metrics['transport_error_type'] == 'ReadError'
    assert metrics['request_attempts'] == 2
    assert secret not in str(caught.value)
    assert secret not in caplog.text
    assert secret not in capsys.readouterr().out
    assert secret not in json.dumps(metrics)


def test_connection_failure_checkpoints_real_tool_and_continue_does_not_replay(
        tmp_path, sandbox_env, monkeypatch):
    monkeypatch.setattr(config, 'ENABLE_TASK_PLANS', False)
    monkeypatch.setattr(config, 'ENABLE_AGENT_TASKS', False)
    monkeypatch.setattr(config, 'ENABLE_COMMAND_JOBS', False)
    monkeypatch.setattr(config, 'ENABLE_STRUCTURED_CONTEXT', False)
    monkeypatch.setattr(config, 'MEMORY_AUTO_EXTRACT', False)
    monkeypatch.setattr(config, 'RAG_ASSESS', False)

    create_tool = TOOL_REGISTRY['create_file']
    original_create = create_tool.fn
    executions = []

    def counted_create(**kwargs):
        executions.append(kwargs['path'])
        return original_create(**kwargs)

    monkeypatch.setattr(create_tool, 'fn', counted_create)

    async def run():
        store = SessionStore(tmp_path / 'state', sandbox_env)
        calls = []

        async def model(history, *, session_id, emit):
            calls.append(history)
            if len(calls) == 1:
                return {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'create-once', 'type': 'function', 'function': {
                        'name': 'create_file', 'arguments': json.dumps({
                            'path': 'notes/once.txt', 'content': 'saved', 'reason': 'user request'})}}]}
            if len(calls) == 2:
                emit('content', '已保存，继续核对')
                raise llm.ModelConnectionError('模型连接中断，请稍后继续。')
            assert any(message.get('role') == 'user' and message.get('content') == '继续'
                       for message in history)
            assert sum(message.get('tool_call_id') == 'create-once' for message in history) == 1
            return {'role': 'assistant', 'content': '核对完成'}

        manager = SessionManager(store, model=model, confirmation_handler=lambda *_: True)
        session = manager.create()
        workspace = store.workspace(session.id)
        manager.submit(session, '建立 notes/once.txt')
        await session.task

        assert (workspace / 'notes/once.txt').read_text(encoding='utf-8') == 'saved'
        assert executions == ['notes/once.txt']
        assert len(calls) == 2
        assert session.record['status'] == 'checkpoint'
        assert session.record['last_run']['stop_reason'] == 'model_connection'
        assert session.record['error'] == ''
        assert any(message.get('tool_call_id') == 'create-once' and '[完成]' in message['content']
                   for message in session.record['messages'])
        assert any(message.get('content') == '已保存，继续核对'
                   for message in session.record['messages'])
        persisted = store.load_all()[0]
        assert persisted['status'] == 'checkpoint'
        assert persisted['last_run']['stop_reason'] == 'model_connection'

        manager.submit(session, '继续')
        await session.task
        assert session.record['status'] == 'idle'
        assert session.record['messages'][-1]['content'] == '核对完成'
        assert executions == ['notes/once.txt']
        assert len(calls) == 3
        await manager.shutdown()
        store.close()

    asyncio.run(run())
