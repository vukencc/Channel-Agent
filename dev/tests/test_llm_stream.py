import asyncio
from types import SimpleNamespace

from core import llm


def test_stream_callback_closes_connection_and_uses_session_id(monkeypatch, capsys):
    class Stream:
        closed = False
        def __aiter__(self):
            async def iterator():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
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
    assert events == [('content', 'hello')]
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
