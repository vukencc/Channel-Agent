import asyncio
from types import SimpleNamespace

from ai_agent_startup.core import llm


def test_stream_callback_closes_connection_and_uses_session_id(monkeypatch, capsys):
    class Stream:
        closed = False
        def __aiter__(self):
            async def iterator():
                yield SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", delta=SimpleNamespace(
                    content='hello', tool_calls=None, reasoning_content=None))])
            return iterator()
        async def close(self):
            self.closed = True
    stream = Stream()
    async def open_stream(history, session_id):
        assert session_id == 'session-a'
        return stream
    monkeypatch.setattr(llm, '_open_stream', open_stream)
    events = []
    result = asyncio.run(llm.call_model([], session_id='session-a', emit=lambda *event: events.append(event)))
    assert result['content'] == 'hello'
    assert events[0] == ('content', 'hello')
    assert events[-1][0] == 'metrics'
    assert events[-1][1]['outcome'] == 'ok'
    assert stream.closed
    assert not capsys.readouterr().out


def test_model_requests_use_distinct_session_headers(monkeypatch):
    captured = []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='score'))])
    monkeypatch.setattr(llm, 'get_client', lambda: Client())
    async def run():
        await llm._open_stream([], 'agent-a')
        await llm._open_stream([], 'agent-b')
        await llm.complete('assessment', session_id='agent-a')
    asyncio.run(run())
    assert [row['extra_headers']['x-opencode-session'] for row in captured] == ['agent-a', 'agent-b', 'agent-a']


class FakeStream:
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay, self.closed = chunks, delay, False
    def __aiter__(self):
        async def run():
            for chunk in self.chunks:
                await asyncio.sleep(self.delay)
                yield chunk
        return run()
    async def close(self):
        self.closed = True


def chunk(content=None, reasoning=None, calls=None, finish=None):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, delta=SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=calls))])


def tool_delta(index=0, identifier=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=identifier,
                           function=SimpleNamespace(name=name, arguments=arguments))


def install_stream(monkeypatch, stream):
    async def open_stream(*args):
        return stream
    monkeypatch.setattr(llm, '_open_stream', open_stream)


def test_reasoning_and_fragmented_tool_calls_survive_round_trip(monkeypatch):
    stream = FakeStream([chunk(reasoning='first '), chunk(reasoning='second'),
        chunk(calls=[tool_delta(1, 'b', 'read_file', '{"path":')]),
        chunk(calls=[tool_delta(0, 'a', 'list_files', '{}')]),
        chunk(calls=[tool_delta(1, arguments='"a.txt"}')], finish='tool_calls')])
    install_stream(monkeypatch, stream)
    result = asyncio.run(llm.call_model([], emit=lambda *a: None))
    assert result['reasoning_content'] == 'first second'
    assert [c['id'] for c in result['tool_calls']] == ['a', 'b']
    assert result['tool_calls'][1]['function']['arguments'] == '{"path":"a.txt"}'
    assert stream.closed


def test_incomplete_or_invalid_stream_never_returns_executable_calls(monkeypatch):
    import pytest
    for finish, args in [(None, '{}'), ('length', '{}'), ('tool_calls', '{'), ('tool_calls', '[]')]:
        stream = FakeStream([chunk(calls=[tool_delta(0, 'a', 'create_file', args)], finish=finish)])
        install_stream(monkeypatch, stream)
        with pytest.raises(RuntimeError):
            asyncio.run(llm.call_model([], emit=lambda *a: None))
        assert stream.closed


def test_total_deadline_stops_a_live_but_endless_stream(monkeypatch):
    from ai_agent_startup import config
    import pytest
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .05)
    stream = FakeStream([chunk(reasoning='thinking')] * 100, delay=.01)
    install_stream(monkeypatch, stream)
    events = []
    with pytest.raises(TimeoutError, match='总时限'):
        asyncio.run(llm.call_model([], emit=lambda *a: events.append(a)))
    assert stream.closed
    assert events[-1][1]['outcome'] == 'timeout'
    assert events[-1][1]['total_s'] < .3


def test_total_deadline_includes_open_stream_retries(monkeypatch):
    from ai_agent_startup import config
    import httpx
    import openai
    import pytest
    monkeypatch.setattr(config, 'MODEL_CALL_TIMEOUT', .05)
    async def failure():
        raise openai.APIConnectionError(request=httpx.Request('POST', 'https://example.test'))
    async def open_stream(*args):
        return await llm._retry(failure, 'test', attempts=5)
    monkeypatch.setattr(llm, '_open_stream', open_stream)
    with pytest.raises(TimeoutError):
        asyncio.run(llm.call_model([], emit=lambda *a: None))


def test_reasoning_configuration_sent_to_stream_and_assessment(monkeypatch):
    from ai_agent_startup import config
    monkeypatch.setattr(config, 'REASONING_EFFORT', 'low')
    monkeypatch.setattr(config, 'THINKING_MODE', 'auto')
    assert llm.model_options() == {'reasoning_effort': 'low'}
    monkeypatch.setattr(config, 'THINKING_MODE', 'disabled')
    assert llm.model_options() == {'extra_body': {'thinking': {'type': 'disabled'}}}


def test_slow_client_initialization_does_not_freeze_event_loop(monkeypatch):
    import time
    class Client:
        chat = None
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
        def with_options(self, **kwargs):
            return self
        async def create(self, **kwargs):
            return FakeStream([])
    def slow_client():
        time.sleep(.12)
        return Client()
    monkeypatch.setattr(llm, 'get_client', slow_client)
    async def run():
        request = asyncio.create_task(llm._open_stream([]))
        ticks = []
        while not request.done():
            start = time.monotonic()
            await asyncio.sleep(.01)
            ticks.append(time.monotonic() - start)
        await request
        assert len(ticks) >= 5
        assert max(ticks) < .08
    asyncio.run(run())


def test_output_budget_stops_oversized_tool_arguments_without_execution(monkeypatch):
    import pytest
    from ai_agent_startup import config
    monkeypatch.setattr(config, 'MODEL_OUTPUT_CHARS', 100)
    stream = FakeStream([chunk(calls=[tool_delta(0, 'a', 'create_file', '{"content":"' + 'x'*200)])])
    install_stream(monkeypatch, stream)
    with pytest.raises(llm.ModelResponseError, match='字符上限'):
        asyncio.run(llm.call_model([], emit=lambda *a: None))
    assert stream.closed
